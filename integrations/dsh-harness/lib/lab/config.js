/**
 * lab 侧的运行配置：全部走环境变量，默认值对准生产（服务器上的 daemon）。
 *
 * 关键变量：
 *   LAB_MBOT_SOCK         daemon unix socket 路径
 *   LAB_MBOT_WORKSPACE    `机器:路径`，例如 `sii2:/inspire/hdd/...`；留空=用 daemon 默认工作区
 *   LAB_MBOT_OWNER        client_id 前缀，默认 dsh
 *   LAB_MBOT_MODE         `canned` = 不连 daemon，用假事件（离线跑 UI）
 *   LAB_MBOT_STALL_S      等模型多久没有任何事件算卡死，默认 600（实测有 307s 的合法步骤）
 *   LAB_MBOT_TOOL_TIMEOUT_S  工具执行期的容忍度，默认 1800
 *   LAB_MBOT_TURN_CAP_S   单回合硬上限，默认 1800
 *   LAB_MBOT_VERBOSE=1    把事件流打到 stderr
 */
import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";

/**
 * macchiatoBot 的 checkout 目录，用来定位 unix socket 和默认配置目录。
 * 优先级：LAB_MBOT_ROOT → MACCHIATO_HOME → $HOME/macchiatoBot
 */
function defaultRoot() {
	if (process.env.LAB_MBOT_ROOT) return process.env.LAB_MBOT_ROOT;
	if (process.env.MACCHIATO_HOME) return process.env.MACCHIATO_HOME;
	return join(homedir(), "macchiatoBot");
}

const DEFAULT_ROOT = defaultRoot();

/** `机器:路径` → `{login, path}`；没有冒号则视为本机路径。 */
export function parseWorkspace(raw) {
	const text = String(raw ?? "").trim();
	if (!text) return null;
	const cut = text.indexOf(":");
	if (cut < 0) return { login: "", path: text };
	return { login: text.slice(0, cut).trim(), path: text.slice(cut + 1).trim() };
}

export function labConfig() {
	const env = process.env;
	return {
		socket: env.LAB_MBOT_SOCK || join(env.MB_ROOT || DEFAULT_ROOT, "data/automation/automation.sock"),
		workspace: parseWorkspace(env.LAB_MBOT_WORKSPACE),
		owner: env.LAB_MBOT_OWNER || "dsh",
		canned: env.LAB_MBOT_MODE === "canned",
		verbose: env.LAB_MBOT_VERBOSE === "1",
		/*
		 * 等模型的容忍度。实测（2026-11-12，24 小时 220 个步骤）：
		 * p50=5.8s、p90=12s、p95=19s，但**最长有 307s 的合法步骤**（大上下文首 token 慢）。
		 * 原来 180s 会把这类正常请求误杀，所以默认提到 600s；
		 * 真正的死锁（以前出现过 668s 无任何输出）仍然会在 10 分钟内被判定。
		 */
		stallMs: Math.max(30, Number(env.LAB_MBOT_STALL_S || 600)) * 1000,
		/*
		 * 工具执行期的容忍度。工具在跑的时候 daemon 只有心跳、没有事件，
		 * 拿「等模型」的 180 秒去判会误杀（2026-11-11 23:30 实测：
		 * 一个长 bash 跑到 200 秒被 MBOT_STALLED 打断）。默认放到 30 分钟。
		 */
		toolMs: Math.max(30, Number(env.LAB_MBOT_TOOL_TIMEOUT_S || 1800)) * 1000,
		turnCapMs: Math.max(30, Number(env.LAB_MBOT_TURN_CAP_S || 1800)) * 1000,
	};
}

/**
 * DSH 会话 ↔ mbot 会话的稳定映射。
 *
 * 不用落盘：session_id 由 DSH 的会话 id 哈希出来，进程重启/会话重开都指向同一个
 * mbot 会话，`session_switch` 自己会 create_if_missing，重连即续上。
 */
export function mbotIdentity(dshSessionId, cfg) {
	const digest = createHash("sha1").update(String(dshSessionId)).digest("hex").slice(0, 12);
	return { clientId: `${cfg.owner}:${dshSessionId}`, sessionId: `dsh-${digest}` };
}
