/**
 * 被 vendor 后的 `dsh-agent-loop` 调用的 `step()` 实现。
 *
 * 原生 step() 做两件事：调模型（流式）→ 跑工具（dsh-tools 调度器）。
 * 这里两件事都交给 mbot：
 *   - mbot 的 assistant_delta / reasoning_delta → 喂给 DSH 的
 *     `AssistantStreamAttempt`（前端的流式渲染、落库、token 统计全部照旧）
 *   - mbot 的 tool_call / tool_result trace → 直接落成 DSH 的
 *     `tool/call` / `tool/result` 会话事件（前端渲染成工具卡片）
 *
 * 一个 DSH step 对应 mbot 的一次模型调用（以 `llm_request` trace 为界），
 * 所以在同一回合里 step() 会返回 null 表示「还有下一步」。
 * turn/start、step/start、step/end、turn/end、取消、落盘都在 vendor 来的
 * `turn()` 里，不重复实现。
 */
import { LlmError, createAssistantMessage, createToolResultMessage } from "@deepseek-ai/dsh-llm";
import { LabTurn } from "./turn.js";
import { MbotError, abortError } from "./wire.js";
import { requestApproval } from "./approval.js";

/** 失败也要让前端收敛：把错误变成一次 abort，交给 DSH 的 turn/end 收尾。 */
function asError(error) {
	if (error instanceof LlmError) return error;
	if (error instanceof MbotError) return new LlmError(error.message, error.code, error.info ?? { message: error.message });
	return error;
}

/**
 * 一个 step。
 * @returns `null` = 本回合还有下一步；`{kind:"completed"}` = 回合说完了。
 */
export async function labStep(agent, decision, rt) {
	try {
		return await stepOnce(agent, decision, rt);
	} catch (error) {
		if (process.env.LAB_MBOT_DEBUG === "1") {
			process.stderr.write(`[lab-mbot] step failed: ${error?.stack || error}\n`);
		}
		throw error;
	}
}

async function stepOnce(agent, decision, rt) {
	if (agent.phase.kind !== "running") throw new Error(`agent "${agent.id}": step outside running phase`);
	const { turn, step, abort: { signal } } = agent.phase;
	signal.throwIfAborted();

	/* 用户输入先落库，和原生行为一致（历史里必须有这一条） */
	for (const message of decision.messages) {
		agent.session.append("user/message", message, { surfaceOp: "append" });
	}

	let lab;
	try {
		lab = await LabTurn.for(agent, turn, { signal, decision });
	} catch (error) {
		throw asError(error);
	}

	const live = new rt.AssistantStreamAttempt(
		agent.session.id,
		++agent.assistantAttemptCounter,
		() => ++agent.assistantStreamRevision,
		turn,
		step,
		(frame) => {
			agent.dispatch.emit("agent/assistant-stream", { frame });
		},
	);

	/*
	 * 正文必须比工具卡片先落库（前端顺序才是「正文 → 卡片」）。
	 * 但**时间戳**又必须在事件真正发生时落，否则 tool/call 与 tool/result 几乎同一时刻，
	 * 前端算出来的「工具调用用时」永远是 0 秒。
	 * 折中：一遇到第一个工具事件就先把手里的正文落成 assistant/message，随后立刻落工具事件。
	 */
	let settled = false;
	const settleAssistant = () => {
		if (settled) return;
		settled = true;
		const blocks = live.blocks();
		try {
			if (blocks.length > 0) {
				live.settle("assistant/message", () =>
					agent.session.append(
						"assistant/message",
						{
							turn,
							step,
							message: createAssistantMessage({ content: blocks, source: lab.source }),
							...(live.usage === undefined ? {} : { usage: live.usage }),
							stream: live.stream,
						},
						{ surfaceOp: "append" },
					).seq,
				);
			} else {
				live.settle("assistant/attempt", () =>
					agent.session.append("assistant/attempt", { turn, step, stream: live.stream }).seq,
				);
			}
		} catch (error) {
			if (!live.ended) live.abandon();
			throw error;
		}
	};

	/** 已落库的工具调用：callId → 事件 seq（result 要引用它）。 */
	const callSeqs = new Map();
	const tools = {
		appendCall(callId, name, args) {
			settleAssistant();
			const { seq } = agent.session.append("tool/call", {
				turn,
				step,
				callId,
				name,
				arguments: args ?? "",
			});
			callSeqs.set(callId, seq);
		},
		appendResult(item) {
			const message = createToolResultMessage({
				callId: item.callId,
				content: [{ type: "text", text: item.text || (item.ok ? "(无输出)" : "(失败，无输出)") }],
				isError: item.ok === false,
			});
			const callSeq = callSeqs.get(item.callId);
			agent.session.append(
				"tool/result",
				{
					turn,
					step,
					message,
					...(item.error && typeof item.error === "object" ? { error: item.error } : {}),
				},
				{
					surfaceOp: "append",
					...(callSeq === undefined ? {} : { sourceEventSeqs: [callSeq] }),
				},
			);
		},
	};

	let started = false;
	let continueTurn = false;

	try {
		live.start();
		started = true;
		continueTurn = await pump({ lab, live, tools, signal });
		signal.throwIfAborted();
	} catch (error) {
		if (!started) throw asError(error);
		if (settled) {
			lab.close(String(error?.message ?? error));
			throw asError(error);
		}
		try {
			/*
			 * 失败（取消 / daemon 卡死 / 超时）也要给前端留下可读的东西：
			 * 有部分内容就落成 interrupted 的 assistant/message —— 否则只落 assistant/attempt，
			 * 前端刷新后那段文字会消失，用户也看不出这一轮其实是失败的。
			 */
			const content = live.interruptedBlocks();
			if (content.length > 0) {
				live.settle("assistant/message", () =>
					agent.session.append(
						"assistant/message",
						{
							turn,
							step,
							message: createAssistantMessage({ content, source: lab.source }),
							interrupted: true,
							stream: live.stream,
						},
						{ surfaceOp: "append" },
					).seq,
				);
			} else {
				live.settle("assistant/attempt", () =>
					agent.session.append("assistant/attempt", { turn, step, stream: live.stream }).seq,
				);
			}
		} catch (settlementError) {
			throw new AggregateError([error, settlementError], "Assistant stream failed and its durable settlement was rejected", {
				cause: error,
			});
		}
		lab.close(String(error?.message ?? error));
		throw asError(error);
	}

	try {
		settleAssistant();
	} catch (error) {
		if (!live.ended) live.abandon();
		lab.close(String(error?.message ?? error));
		throw error;
	}

	if (continueTurn) return null;
	lab.close("回合完成");
	return { kind: "completed" };
}

/**
 * 把 mbot 事件摊进当前 step：正文/思考进流，工具事件排队，遇到下一步的
 * `llm_request` 就停。返回 true 表示「后面还有步骤」。
 */
async function pump({ lab, live, tools, signal }) {
	let nextIndex = 0;
	let reasoningIndex;
	let textIndex;
	let sawContent = false;
	/** toolCallId → { index, name, arguments } */
	const toolBlocks = new Map();
	const calls = new Set();

	const openBlock = (blockType) => {
		const index = nextIndex++;
		live.push({ type: "block-start", index, blockType });
		return index;
	};

	/* 权限请求单独跑，不阻塞事件泵（用户点「允许」之前正文照常流动） */
	const pendingApprovals = new Set();
	const askPermission = (event) => {
		const task = requestApproval({
			agent: lab.agent,
			permissionId: event.permissionId,
			payload: event.payload,
			signal,
			log: (message) => lab.log?.(message),
		}).catch(() => {});
		pendingApprovals.add(task);
		task.finally(() => pendingApprovals.delete(task));
	};

	while (true) {
		if (signal.aborted) throw abortError();
		const event = await lab.next();
		if (event === null) {
			throw new MbotError("daemon 的回合流没有收尾就结束了", { code: "MBOT_STREAM_CLOSED" });
		}
		switch (event.type) {
			case "llm-request": {
				if (sawContent) return true;
				break;
			}
			case "reasoning": {
				sawContent = true;
				if (reasoningIndex === undefined) reasoningIndex = openBlock("reasoning");
				live.push({ type: "reasoning-delta", index: reasoningIndex, text: event.text });
				break;
			}
			case "text": {
				sawContent = true;
				if (textIndex === undefined) textIndex = openBlock("text");
				live.push({ type: "text-delta", index: textIndex, text: event.text });
				break;
			}
			case "tool-call": {
				sawContent = true;
				const callId = event.callId || `mbot-call-${toolBlocks.size + 1}`;
				const index = openBlock("tool-call");
				toolBlocks.set(callId, { index, name: event.name, arguments: event.arguments });
				calls.add(callId);
				live.push({
					type: "tool-call-delta",
					index,
					id: callId,
					name: event.name,
					argumentsDelta: event.arguments,
				});
				tools.appendCall(callId, event.name, event.arguments);
				/* 工具之后通常是新一轮模型调用，正文/思考要重新开块 */
				textIndex = undefined;
				reasoningIndex = undefined;
				break;
			}
			case "tool-result": {
				sawContent = true;
				if (!calls.has(event.callId)) {
					/* 没有对应 call 的孤立结果：落库会被会话不变式拒绝，直接丢 */
					break;
				}
				calls.delete(event.callId);
				tools.appendResult({
					callId: event.callId,
					name: event.name,
					ok: event.ok,
					text: event.text,
					error: event.error,
				});
				textIndex = undefined;
				reasoningIndex = undefined;
				break;
			}
			case "permission": {
				/* 不产生正文，只把审批请求派出去 */
				askPermission(event);
				break;
			}
			case "notice": {
				/* 工作区切换之类的系统说明：当作一条独立正文 */
				sawContent = true;
				if (textIndex === undefined) textIndex = openBlock("text");
				live.push({ type: "text-delta", index: textIndex, text: `${event.text}\n\n` });
				break;
			}
			case "final": {
				if (lab.usage !== undefined) live.push({ type: "usage", usage: lab.usage });
				live.push({ type: "finish", reason: { kind: "stop" } });
				return false;
			}
			default:
				break;
		}
	}
}
