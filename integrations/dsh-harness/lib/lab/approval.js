/**
 * 权限审批：把 mbot daemon 的 `feishu_permission_notify` 接到 DSH 的审批 UI 上。
 *
 * 链路：
 *   daemon `run_turn_stream` → 事件 `feishu_permission_notify {permission_id, payload}`
 *     → 本模块 `requestApproval()` → `ctx.approval.request({agent, toolName, reason, signal})`
 *     → DSH 弹出审批卡片 → 人类选择
 *     → 另开一条 IPC 连接发 `resolve_permission {permission_id, allowed, …}` → daemon 放行/拒绝
 *
 * 为什么另开连接：主连接的事件流被 `run_turn_stream` 独占（串行 reader），
 * 并发读会互相抢帧；`resolve_permission` 是 daemon 侧全局注册表，任意连接都能回。
 *
 * 失败一律 fail closed（拒绝），与「没人处理就超时拒绝」的现有行为一致。
 */
import { MbotError, MbotLink } from "./wire.js";
import { labConfig } from "./config.js";

/** mbot 侧默认等 300 秒；留 5 秒余量，先把「用户没理」变成主动拒绝而不是超时。 */
const DEFAULT_TIMEOUT_MS = 295_000;

/**
 * 一条短命的 IPC 连接，用来回审批结果。
 * @param {string} socketPath
 */
async function replyLink(socketPath) {
	return MbotLink.open(socketPath, { timeoutMs: 5_000 });
}


/**
 * 取审批服务。
 *
 * cordis 的上下文代理要求「先 inject 才能读服务」，而这里拿到的是 **agent 的 scope**
 * （不是本插件的），直接 `agent.ctx.approval` 会抛
 * `cannot get property "approval" without inject`。
 * `ctx.reflect.get(name, false)` 是官方提供的「不走 inject」读取口。
 *
 * @param {any} agent
 */
export function approvalServiceOf(agent) {
	const candidates = [agent?.ctx, agent?.loopCtx, agent?.ctx?.root];
	for (const ctx of candidates) {
		if (!ctx) continue;
		try {
			const direct = ctx.approval;
			if (direct?.request) return direct;
		} catch {
			/* 未注入，继续走 reflect */
		}
		try {
			const reflected = ctx.reflect?.get?.("approval", false);
			if (reflected?.request) return reflected;
		} catch {
			/* 忽略 */
		}
	}
	return null;
}

/**
 * 把 mbot 的 payload 变成一句人话，作为 DSH 审批卡片的说明。
 * @param {Record<string, any>} payload
 */
export function describePermission(payload) {
	const parts = [];
	if (payload.summary) parts.push(String(payload.summary));
	if (payload.command) parts.push(`命令：${String(payload.command).slice(0, 400)}`);
	else if (payload.cwd) parts.push(`目录：${payload.cwd}`);
	if (Array.isArray(payload.risk_reasons) && payload.risk_reasons.length > 0) {
		parts.push(`风险：${payload.risk_reasons.join("、")}`);
	}
	if (Array.isArray(payload.path_grants) && payload.path_grants.length > 0) {
		const prefixes = payload.path_grants.map((grant) => String(grant?.path_prefix ?? "")).filter(Boolean);
		if (prefixes.length > 0) parts.push(`涉及路径：${prefixes.join("、")}`);
	}
	if (payload.tool_name) parts.push(`工具：${payload.tool_name}`);
	return parts.join("\n") || "mbot 请求执行一个需要批准的操作";
}

/**
 * 处理一条权限请求：问用户，然后把结果回给 daemon。
 *
 * @param {object} params
 * @param {import("@deepseek-ai/dsh-agent").Agent} params.agent
 * @param {string} params.permissionId
 * @param {Record<string, any>} params.payload
 * @param {AbortSignal} [params.signal]
 * @param {(message: string) => void} [params.log]
 */
export async function requestApproval({ agent, permissionId, payload, signal, log = () => {} }) {
	if (!permissionId) {
		log("permission: 缺少 permission_id，忽略");
		return;
	}
	const config = labConfig();
	const payloadTimeout = Number(payload?.timeout_seconds ?? 0);
	const timeoutMs = payloadTimeout > 0 ? Math.max(5_000, payloadTimeout * 1000 - 5_000) : DEFAULT_TIMEOUT_MS;

	/** @type {"allowed-once" | "rejected" | "cancelled" | "unavailable"} */
	let outcome = "unavailable";
	try {
		const service = approvalServiceOf(agent);
		if (!service?.request) {
			/* 宿主没挂审批服务：保持「无人可问 = 拒绝」的既有行为 */
			log(`permission ${permissionId}: 取不到 approval 服务（未挂载或作用域不可见），按拒绝处理`);
		} else {
			const timeout = AbortSignal.timeout(timeoutMs);
			const merged = signal ? AbortSignal.any([signal, timeout]) : timeout;
			outcome = await service.request({
				agent,
				toolName: String(payload?.tool_name ?? "mbot"),
				reason: describePermission(payload),
				signal: merged,
			});
		}
	} catch (error) {
		log(`permission ${permissionId}: 审批失败 —— ${String(error?.message ?? error)}`);
		outcome = "unavailable";
	}

	const allowed = outcome === "allowed-once";
	let link;
	try {
		link = await replyLink(config.socket);
		await link.request(
			"resolve_permission",
			{
				permission_id: permissionId,
				allowed,
				clarify_requested: false,
				persist_acl: false,
				note: allowed ? "approved-via-dsh" : `denied-via-dsh:${outcome}`,
			},
			{ timeoutMs: 15_000 },
		);
		log(`permission ${permissionId}: ${allowed ? "已批准" : `已拒绝（${outcome}）`}`);
	} catch (error) {
		/* 回不了也没办法，daemon 那边会按超时处理 */
		log(`permission ${permissionId}: 回执失败 —— ${String(error?.message ?? error)}`);
	} finally {
		link?.close();
	}
}
