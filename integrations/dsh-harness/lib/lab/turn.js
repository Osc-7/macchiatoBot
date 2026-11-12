/**
 * 一个 DSH 回合 = 一条到 daemon 的连接 + 一次 `run_turn_stream`。
 *
 * DSH 的一个回合里可能有多步（step）：mbot 每调用一次模型就是一步。
 * 步骤边界用 trace 里的 `llm_request` 标记，所以这里把事件流摊平成一个
 * 事件迭代器，由 step() 决定什么时候"停在这一步"。
 */
import { labConfig, mbotIdentity } from "./config.js";
import { extractBackgroundJob, trackJob } from "./job-watch.js";
import { cannedEvents, toLabEvents } from "./events.js";
import { messageText, turnText } from "./text.js";
import { MbotError, MbotLink, abortError } from "./wire.js";
import { resolveSessionWorkspace } from "./workspace-model.js";

/** agent → 当前回合的 LabTurn（跨 step 复用同一条连接）。 */
const ACTIVE = new WeakMap();

export class LabTurn {
	constructor(agent, turn, signal, decision) {
		this.agent = agent;
		this.turn = turn;
		this.signal = signal;
		this.decision = decision;
		this.config = labConfig();
		this.clientId = "";
		this.sessionId = "";
		this.link = null;
		this.iterator = null;
		this.usageBefore = null;
		/** 正在执行、还没拿到结果的工具数量（决定卡死判定用哪个超时） */
		this.pendingTools = 0;
		this.finished = false;
		this.closed = false;
		/* DSH 侧 assistant 消息的 provider/model 标签 */
		this.source = { provider: "mbot", model: "mbot" };
	}

	/** 给外部（审批等）用的日志出口，走和内部一样的格式。 */
	log(message) {
		this.#log(String(message));
	}

	/** 取本回合的驱动器；同一回合的第二步必须复用同一条连接。 */
	static async for(agent, turn, { signal, decision }) {
		const current = ACTIVE.get(agent);
		if (current && current.turn === turn && !current.closed && !current.finished) return current;
		if (current) current.close("回合结束/换代");
		const next = new LabTurn(agent, turn, signal, decision);
		await next.open();
		ACTIVE.set(agent, next);
		return next;
	}

	/** 打开连接、绑定会话、设置工作区、起流。 */
	async open() {
		if (this.config.verbose) {
			const shape = this.decision.messages.map((message) => ({
				role: message.role,
				source: message.source?.kind ?? null,
				text: messageText(message).slice(0, 48).replace(/\s+/g, " "),
			}));
			this.#log(`messages: ${JSON.stringify(shape)}`);
		}
		if (this.config.canned) {
			this.source = { provider: "mbot", model: "canned" };
			this.iterator = cannedEvents({ text: turnText(this.decision.messages) })[Symbol.asyncIterator]();
			return;
		}
		const identity = mbotIdentity(this.agent.session.id, this.config);
		this.clientId = identity.clientId;
		this.sessionId = identity.sessionId;
		this.link = await MbotLink.open(this.config.socket);
		await this.link.request(
			"session_switch",
			{ client_id: this.clientId, session_id: this.sessionId },
			{ timeoutMs: 30_000 },
		);
		await this.#ensureWorkspace();
		await this.#switchModel();
		this.usageBefore = await this.#usageSnapshot();
		const text = turnText(this.decision.messages);
		if (!text.trim()) {
			/* 没有用户输入却要跑回合，说明状态不对；别去 daemon 那里瞎跑一轮。 */
			throw new MbotError("回合没有可发送的用户输入", { code: "MBOT_EMPTY_INPUT" });
		}
		this.#log(`turn ${this.turn} → ${this.sessionId} · ${text.slice(0, 80)}`);
		const frames = this.link.stream(
			"run_turn_stream",
			{ client_id: this.clientId, text },
			{
				signal: this.signal,
				idleMs: () => (this.pendingTools > 0 ? this.config.toolMs : this.config.stallMs),
				timeoutMs: 60_000,
				/* keepalive 每 15 秒一次，不能算进展，否则模型卡住时会永远转圈 */
				isProgress: (frame) => frame.event !== "keepalive",
			},
		);
		this.iterator = toLabEvents(frames)[Symbol.asyncIterator]();
	}

	/** 工作区：daemon 侧已经在这个路径就跳过，否则 `remote_workspace_use`。 */
	async #ensureWorkspace() {
		/* 会话级三层选择（机器/proj/目录）优先于环境变量里的默认值 */
		let selection = null;
		try {
			selection = resolveSessionWorkspace(this.agent.session.id);
		} catch (error) {
			throw new MbotError(`工作区配置有问题：${String(error?.message || error)}`, { code: "WORKSPACE_INVALID" });
		}
		const workspace = selection
			? { login: selection.login, path: selection.root }
			: this.config.workspace;
		if (!workspace || !workspace.login) return;
		try {
			const status = await this.link.request(
				"remote_workspace_status",
				{ client_id: this.clientId },
				{ timeoutMs: 20_000 },
			);
			const active = status?.active === true && status?.worker_online === true;
			const resolved = status?.state?.resolved_path;
			if (active && resolved === workspace.path) return;
			const used = await this.link.request(
				"remote_workspace_use",
				{ client_id: this.clientId, login: workspace.login, path: workspace.path, profile: "dev" },
				{ timeoutMs: 30_000 },
			);
			if (used && used.ok === false) {
				throw new MbotError(`${workspace.login} 拒绝了工作区 ${workspace.path}`, { code: "MBOT_WORKSPACE" });
			}
		} catch (error) {
			if (error instanceof MbotError && error.code === "MBOT_WORKSPACE") throw error;
			throw new MbotError(
				`${workspace.login} 离线或工作区不可用（${workspace.path}）`,
				{ code: "MBOT_WORKER_OFFLINE", info: { cause: String(error?.message || error) } },
			);
		}
	}

	/**
	 * DSH 侧选中的模型 → daemon 的 `model_switch`。
	 * 0.2.x 的 step() 会带 `decision.selection`；0.1.x 没有，就不切（用 daemon 当前模型）。
	 */
	async #switchModel() {
		const selection = this.decision?.selection;
		const model = typeof selection?.model === "string" ? selection.model : "";
		if (!model) return;
		const effort = typeof selection?.reasoningEffort === "string" ? selection.reasoningEffort : "";
		/* daemon 把档位做成了模型变体：<模型>__<档位>；没选档位就用基模型 */
		const candidates = effort ? [`${model}__${effort}`, model] : [model];
		for (const name of candidates) {
			try {
				await this.link.request("model_switch", { name }, { timeoutMs: 15_000 });
				if (this.config.verbose) this.#log(`model_switch → ${name}`);
				return;
			} catch (error) {
				/* 变体不存在就退回基模型；都失败也不让回合失败，daemon 会用当前模型继续跑 */
				const last = error;
				if (name === candidates[candidates.length - 1]) {
					this.#log(`model_switch 失败（继续用当前模型）：${String(last?.message || last)}`);
				}
			}
		}
	}

	async #usageSnapshot() {
		try {
			const result = await this.link.request(
				"get_token_usage",
				{ session_id: this.sessionId },
				{ timeoutMs: 15_000 },
			);
			return result?.usage ?? null;
		} catch {
			return null;
		}
	}

	/** 下一条 lab 事件；流结束返回 null。 */
	async next() {
		if (this.signal.aborted) throw abortError();
		/*
		 * 卡住时原来一个字都不会写进日志，事后完全没法查。
		 * 这里按 30 秒打一次心跳日志，出事时能看到「在等模型等了多久」。
		 */
		const since = Date.now();
		const watchdog = setInterval(() => {
			const waited = Math.round((Date.now() - since) / 1000);
			const budget = this.pendingTools > 0 ? this.config.toolMs : this.config.stallMs;
			this.#log(
				this.pendingTools > 0
					? `… 工具执行中已 ${waited}s（上限 ${Math.round(budget / 1000)}s）`
					: `… 等模型响应已 ${waited}s（上限 ${Math.round(budget / 1000)}s，超时判卡死）`,
			);
		}, 30_000);
		let step;
		try {
			step = await this.iterator.next();
		} finally {
			clearInterval(watchdog);
		}
		if (step.done) return null;
		const event = step.value;
		/* 工具在跑的时候只有心跳，用工具期的超时；否则用等模型的超时 */
		if (event.type === "tool-call") this.pendingTools += 1;
		if (event.type === "tool-result") {
			this.pendingTools = Math.max(0, this.pendingTools - 1);
			/* 后台化命令：插件继续盯，跑完主动汇报（见 job-watch.js） */
			const background = extractBackgroundJob(event.text);
			if (background) {
				trackJob({
					agent: this.agent,
					jobId: background.jobId,
					command: background.command,
					log: (message) => this.#log(message),
				});
			}
		}
		if (this.config.verbose) this.#log(`← ${event.type}${event.text ? ` ${JSON.stringify(event.text.slice(0, 40))}` : ""}`);
		if (event.type === "final") {
			this.finished = true;
			this.usage = usageDelta(this.usageBefore, event.result?.token_usage);
			if (event.ok === false) {
				const message = String(event.error?.message ?? "mbot 回合失败");
				throw new MbotError(message, {
					code: String(event.error?.code ?? "MBOT_TURN_FAILED"),
					info: event.errorInfo ?? event.error,
				});
			}
		}
		return event;
	}

	close(reason) {
		if (this.closed) return;
		this.closed = true;
		this.link?.close();
		this.iterator?.return?.().catch(() => {});
		void reason;
	}

	#log(message) {
		process.stderr.write(`[lab-mbot] ${message}\n`);
	}
}

/**
 * 会话累计用量 → 本回合增量。
 * daemon 的 token_usage 是会话累计值（`agent.get_token_usage`），
 * 而 DSH 的胶囊显示的是单回合用量，所以这里取差。
 */
export function usageDelta(before, after) {
	if (!after || typeof after !== "object") return undefined;
	const start = before && typeof before === "object" ? before : {};
	const diff = (key) => {
		const value = Number(after[key]);
		if (!Number.isFinite(value)) return null;
		const base = Number(start[key]);
		return Number.isFinite(base) ? Math.max(0, value - base) : Math.max(0, value);
	};
	const inputRaw = diff("prompt_cache_miss_tokens") ?? diff("prompt_tokens");
	const output = diff("completion_tokens");
	if (inputRaw === null && output === null) return undefined;
	if ((inputRaw ?? 0) + (output ?? 0) === 0) return undefined;
	const usage = {
		inputTokens: inputRaw ?? 0,
		outputTokens: output ?? 0,
	};
	const total = diff("total_tokens");
	if (total !== null) usage.totalTokens = total;
	const cached = diff("prompt_cache_hit_tokens");
	if (cached !== null) usage.cacheReadTokens = cached;
	const reasoningRaw = after.completion_tokens_details?.reasoning_tokens;
	if (Number.isFinite(reasoningRaw)) usage.reasoningTokens = Math.max(0, Number(reasoningRaw));
	return usage;
}

/** 把 daemon 的上下文占用暴露给上层（M3 的统计行用）。 */
export function contextUsage(usage) {
	if (!usage || typeof usage !== "object") return null;
	const current = Number(usage.context_window_current_tokens);
	const max = Number(usage.context_window_max_tokens);
	if (!Number.isFinite(current) || !Number.isFinite(max) || max <= 0) return null;
	return { current, max };
}
