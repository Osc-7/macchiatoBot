/**
 * 后台任务跟踪：工具把命令后台化之后，插件继续盯着它，跑完主动汇报。
 *
 * 为什么需要：回合生命周期绑在 dsh-lab 进程上，用户关掉页面/进程重启时回合就断了。
 * 但**后台 job 活在过去 daemon 里**（独立进程组 + 日志文件），
 * 所以只要有人盯着它，就能在它结束时把结果重新送进会话——即使用户早就关页面了。
 *
 * 链路：
 *   工具结果里出现 `backgrounded: true` / `job_id`
 *     → trackJob() 起一个 watcher（10s 轮询 daemon 的 bash job_status）
 *     → 终态时 `agent.followup(...)`：DSH 的空闲 agent 会被唤醒，自动开一轮把结果讲出来
 *
 * 护栏：同一 job 只汇报一次、最多同时跟 8 个、超过 6 小时放弃、轮询失败不打断。
 */
import { MbotLink } from "./wire.js";
import { labConfig, mbotIdentity } from "./config.js";

/** job_id → 已汇报/已跟踪，避免重复 */
const TRACKED = new Set();
/** 正在跑的 watcher 数量上限 */
const MAX_WATCHERS = 8;
const POLL_MS = 10_000;
const MAX_AGE_MS = 6 * 60 * 60 * 1000;
const TERMINAL = new Set(["finished", "failed", "timed_out", "cancelled"]);

let activeWatchers = 0;

/**
 * 从工具结果文本里认出「后台化」。
 * mbot 的 bash 工具在后台化时返回 `{"data":{"backgrounded":true,"job_id":"job_xxx"}}`。
 *
 * @param {string} text - 工具结果的文本内容
 * @returns {{jobId: string, command: string}|null}
 */
export function extractBackgroundJob(text) {
	if (!text || typeof text !== "string") return null;
	if (!text.includes("job_id")) return null;
	if (!/backgrounded"\s*:\s*true/.test(text) && !/"status"\s*:\s*"running"/.test(text)) return null;
	let payload;
	try {
		payload = JSON.parse(text);
	} catch {
		/* 工具结果有时是包了一层 text 的 JSON 字符串 */
		const start = text.indexOf("{");
		if (start < 0) return null;
		try {
			payload = JSON.parse(text.slice(start));
		} catch {
			return null;
		}
	}
	const data = payload?.data ?? payload;
	const jobId = String(data?.job_id ?? "").trim();
	if (!jobId.startsWith("job_")) return null;
	return { jobId, command: String(data?.command ?? "") };
}

/**
 * 起一个 watcher 盯这个 job，结束时汇报。
 *
 * @param {object} params
 * @param {any} params.agent - 用来 followup 的 agent（DSH 侧）
 * @param {string} params.jobId
 * @param {string} [params.command]
 * @param {string} [params.clientId] - daemon 会话标识（默认按 agent.session.id 推）
 * @param {(message: string) => void} [params.log]
 * @returns {boolean} 是否真的开始跟踪
 */
export function trackJob({ agent, jobId, command = "", clientId, log = () => {} }) {
	if (!jobId || TRACKED.has(jobId)) return false;
	if (activeWatchers >= MAX_WATCHERS) {
		log(`job-watch: 已有 ${activeWatchers} 个 watcher，跳过 ${jobId}`);
		return false;
	}
	TRACKED.add(jobId);
	activeWatchers += 1;
	const startedAt = Date.now();
	const sessionId = String(agent?.session?.id ?? "");

	const finish = (reason) => {
		activeWatchers = Math.max(0, activeWatchers - 1);
		log(`job-watch: ${jobId} 结束（${reason}）`);
	};

	const poll = async () => {
		const config = labConfig();
		const identity = clientId ? { clientId, sessionId } : mbotIdentity(sessionId, config);
		let link;
		try {
			link = await MbotLink.open(config.socket, { timeoutMs: 5_000 });
			const response = await link.request(
				"tool_exec",
				{ client_id: identity.clientId, tool: "bash", args: { job_status: jobId } },
				{ timeoutMs: 15_000 },
			);
			const data = response?.data ?? {};
			const status = String(data.status ?? "");
			if (!TERMINAL.has(status)) {
				if (Date.now() - startedAt > MAX_AGE_MS) {
					finish("超时放弃");
					return;
				}
				setTimeout(poll, POLL_MS);
				return;
			}
			finish(`状态 ${status}`);
			const exitCode = data.exit_code ?? "?";
			const lines = [
				`后台任务 ${jobId} 已完成（状态 ${status}，退出码 ${exitCode}）。`,
				command ? `命令：${command.slice(0, 200)}` : "",
				data.log_path ? `日志：${data.log_path}` : "",
			].filter(Boolean);
			const text = lines.join("\n");
			/*
			 * 收件箱按 message.id 去重（重复会抛 "already pending"），
			 * 所以这里必须给一个唯一 id；形状对齐 session 日志里的 user/message。
			 */
			const message = {
				id: `lab-job-${jobId}-${Date.now().toString(36)}`,
				role: "user",
				content: [{ type: "text", text }],
				source: { kind: "user", rpcId: `lab-job-${jobId}` },
			};
			agent?.followup?.(message);
			log(`job-watch: ${jobId} 已汇报给会话`);
		} catch (error) {
			/* 轮询失败不放弃：daemon 可能正在重启，下一轮再试 */
			if (Date.now() - startedAt > MAX_AGE_MS) {
				finish("轮询持续失败");
				return;
			}
			setTimeout(poll, POLL_MS * 3);
		} finally {
			link?.close();
		}
	};

	setTimeout(poll, POLL_MS);
	log(`job-watch: 开始跟踪 ${jobId}`);
	return true;
}

/** 测试用：重置内部状态。 */
export function __resetForTest() {
	TRACKED.clear();
	activeWatchers = 0;
}
