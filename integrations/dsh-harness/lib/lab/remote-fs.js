/**
 * 远程文件访问：把 daemon 的 `tool_exec(bash)` 当成一条「远程 shell 通道」用。
 *
 * 为什么是 bash 而不是专门的 file IPC：
 *   daemon 的 `tool_exec` 会走会话绑定的工作区（远程就是 worker 上的目录），
 *   而 worker 没有对外暴露 HTTP/ssh。实测 `tool_exec` 必须带 `client_id`，
 *   否则 daemon 会用 "default" 会话（= 本机），这是最容易踩的坑。
 *
 * 每条连接的请求是串行的（daemon 一个连接一次处理一条），所以这里用
 * promise 链做互斥，避免并发请求互相吃掉应答。
 */
import { MbotError, MbotLink } from "./wire.js";
import { labConfig, mbotIdentity } from "./config.js";

/** key（通常是会话 id）→ 已绑定的远程通道。 */
const POOL = new Map();

class RemoteChannel {
	constructor(link, clientId, sessionId, workspace) {
		this.link = link;
		this.clientId = clientId;
		this.sessionId = sessionId;
		this.workspace = workspace;
		this.chain = Promise.resolve();
		this.warmed = false;
	}

	/** 串行执行一条命令，返回 stdout。 */
	run(script, { timeoutMs = 60_000 } = {}) {
		const next = this.chain.then(() => this.#exec(script, timeoutMs));
		this.chain = next.catch(() => {});
		return next;
	}

	/**
	 * 确保 daemon 的 core pool 里有这个会话的 AgentCore。
	 * `tool_exec` 直接取 core_pool.get_entry(session_id)，会话空闲被回收后就没有工具注册表了；
	 * `terminal_spawn` 内部走 CorePool.acquire()，只实例化不跑模型，是最便宜的热身。
	 */
	async warmup() {
		try {
			await this.link.request(
				"terminal_spawn",
				{ client_id: this.clientId, session_id: this.sessionId, source: "system", user_id: "root" },
				{ timeoutMs: 30_000 },
			);
			return true;
		} catch {
			return false;
		}
	}

	async #exec(script, timeoutMs) {
		if (!this.warmed) {
			this.warmed = true;
			await this.warmup();
		}
		const response = await this.link.request(
			"tool_exec",
			{
				/* daemon 用 params.client_id 找 active_session；漏了它命令就跑到本机。 */
				client_id: this.clientId,
				tool: "bash",
				args: { command: script, description: "dsh workspace-files", timeout_seconds: Math.ceil(timeoutMs / 1000) },
			},
			{ timeoutMs },
		);
		const data = response?.data;
		const stdout = typeof data === "string" ? data : String(data?.stdout ?? "");
		if (response?.success === false) {
			const stderr = typeof data === "object" && data !== null ? String(data.stderr ?? "") : "";
			throw new MbotError(stderr.trim() || String(response?.message || "远程命令失败"), {
				code: "WORKSPACE_REMOTE_FAILED",
			});
		}
		return stdout;
	}

	close() {
		try {
			this.link.close();
		} catch {
			/* ignore */
		}
	}
}

/** 把字符串安全地放进 shell 单引号里。 */
function quote(value) {
	return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

/**
 * 拿到一条远程通道（同一 key + 同一机器复用同一条连接）。
 *
 * @param {object} params
 * @param {string} params.key - 复用键（会话 id / WS 连接 id）
 * @param {string} params.login - 机器登录名（空 = 本机，不建通道）
 * @param {string} params.path - 该机器上的工作区绝对路径
 * @param {boolean} [params.force] - 忽略缓存重建
 */
export async function remoteChannel({ key, login, path, force = false }) {
	if (!login) return null;
	const signature = `${login}:${path}`;
	const existing = POOL.get(key);
	if (!force && existing && existing.workspace === signature && !existing.link.closed) return existing;
	if (existing) {
		existing.close();
		POOL.delete(key);
	}
	const config = labConfig();
	const identity = mbotIdentity(key, config);
	const link = await MbotLink.open(config.socket, { timeoutMs: 5_000 });
	await link.request("session_switch", { client_id: identity.clientId, session_id: identity.sessionId }, { timeoutMs: 30_000 });
	const used = await link.request(
		"remote_workspace_use",
		{ client_id: identity.clientId, login, path: path || "/root", profile: "dev" },
		{ timeoutMs: 40_000 },
	);
	if (used && used.ok === false) {
		link.close();
		throw new MbotError(`${login} 拒绝工作区 ${path}`, { code: "WORKSPACE_REMOTE_FAILED" });
	}
	const channel = new RemoteChannel(link, identity.clientId, identity.sessionId, signature);
	POOL.set(key, channel);
	return channel;
}

/** 会话不再绑定远程时释放连接。 */
export function releaseChannel(key) {
	const existing = POOL.get(String(key));
	if (existing) {
		existing.close();
		POOL.delete(String(key));
	}
}
