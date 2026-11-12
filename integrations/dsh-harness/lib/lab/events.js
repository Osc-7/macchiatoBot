/**
 * daemon 帧 → lab 事件。
 *
 * 事件来自 daemon 的 `run_turn_stream`（字段以 daemon 源码为准，不是猜的）：
 *   assistant_delta  {delta}                 模型正文增量
 *   reasoning_delta  {delta}                 思考增量
 *   trace            {data}                  结构化事件，`data.type` 见下
 *   workspace_notice {message}               工作区切换说明
 *   keepalive        {}                      保活，忽略
 *   final            {ok,result|error}       收尾
 *
 * trace.data 的几种类型（src/agent_core/agent/agent.py）：
 *   llm_request {type,turn_id,iteration,tool_count}        ← 一次模型调用的开始（步骤边界）
 *   tool_call   {type,turn_id,iteration,tool_call_id,name,arguments}
 *   tool_result {type,turn_id,iteration,tool_call_id,name,success,message,error,data_preview}
 */

/** 把 arguments（可能是 dict 或字符串）统一成 JSON 字符串——DSH 侧就是这么存的。 */
function argumentsText(value) {
	if (typeof value === "string") return value;
	if (value === undefined || value === null) return "";
	try {
		return JSON.stringify(value);
	} catch {
		return String(value);
	}
}

function traceEvent(data) {
	if (!data || typeof data !== "object") return null;
	const type = String(data.type ?? "");
	const iteration = Number.isFinite(data.iteration) ? data.iteration : undefined;
	if (type === "llm_request") return { type: "llm-request", iteration };
	if (type === "tool_call") {
		return {
			type: "tool-call",
			callId: String(data.tool_call_id ?? data.id ?? ""),
			name: String(data.name ?? data.tool ?? "tool"),
			arguments: argumentsText(data.arguments ?? data.args),
			iteration,
		};
	}
	if (type === "tool_result") {
		const ok = data.success !== false;
		const preview = typeof data.data_preview === "string" ? data.data_preview : "";
		const message = typeof data.message === "string" ? data.message : "";
		const error = data.error && typeof data.error === "object" ? data.error : undefined;
		return {
			type: "tool-result",
			callId: String(data.tool_call_id ?? data.id ?? ""),
			name: String(data.name ?? data.tool ?? "tool"),
			ok,
			text: preview || message || (ok ? "" : String((error && error.message) || "失败")),
			error,
			durationMs: Number.isFinite(data.duration_ms) ? data.duration_ms : undefined,
			iteration,
		};
	}
	return { type: "trace", data };
}

/** 逐帧翻译成 lab 事件；`final` 之后一定结束。 */
export async function* toLabEvents(frames) {
	for await (const frame of frames) {
		if (frame.stream !== true) {
			yield { type: "final", ok: frame.ok !== false, result: frame.result ?? {}, error: frame.error };
			return;
		}
		switch (frame.event) {
			case "assistant_delta": {
				const text = typeof frame.delta === "string" ? frame.delta : "";
				if (text) yield { type: "text", text };
				break;
			}
			case "reasoning_delta": {
				const text = typeof frame.delta === "string" ? frame.delta : "";
				if (text) yield { type: "reasoning", text };
				break;
			}
			case "trace": {
				const event = traceEvent(frame.data);
				if (event) yield event;
				break;
			}
			case "workspace_notice": {
				const text = String(frame.message ?? "");
				if (text) yield { type: "notice", text };
				break;
			}
			case "final": {
				yield {
					type: "final",
					ok: frame.ok !== false,
					result: frame.result ?? {},
					error: frame.error,
					errorInfo: frame.error_info,
				};
				return;
			}
			case "feishu_permission_notify": {
				/* mbot 在等人类批准；交给 DSH 的审批 UI，答复走另一条连接 */
				yield {
					type: "permission",
					permissionId: String(frame.permission_id ?? ""),
					payload: frame.payload ?? {},
				};
				break;
			}
			case "keepalive":
				break;
			default:
				break;
		}
	}
	yield { type: "final", ok: false, result: {}, error: { message: "daemon 流意外结束" } };
}

/**
 * 离线假事件：`LAB_MBOT_MODE=canned` 时用，验证「DSH 前端 ← step() ← 事件映射」这条链路，
 * 不依赖 daemon。形状与真事件完全一致，所以能覆盖映射和 UI。
 */
export async function* cannedEvents({ text }) {
	const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
	yield { type: "llm-request", iteration: 1 };
	const reasoning = "先确认一下环境，再把要看的目录列出来。";
	for (const chunk of split(reasoning, 6)) {
		yield { type: "reasoning", text: chunk };
		await sleep(30);
	}
	for (const chunk of split(`收到：${text}\n\n这是离线假数据（LAB_MBOT_MODE=canned），用来验证前端链路。`, 10)) {
		yield { type: "text", text: chunk };
		await sleep(30);
	}
	const callId = "canned-call-1";
	yield { type: "tool-call", callId, name: "bash", arguments: JSON.stringify({ command: "ls -la /tmp" }) };
	await sleep(120);
	yield {
		type: "tool-result",
		callId,
		name: "bash",
		ok: true,
		text: "total 0\ndrwxrwxrwt 12 root root 4096 Nov 10 16:00 .\ndrwxr-xr-x 20 root root 4096 Nov 10 15:00 ..",
		durationMs: 120,
	};
	yield { type: "llm-request", iteration: 2 };
	for (const chunk of split("\n\n目录是空的，假回合到此结束。", 8)) {
		yield { type: "text", text: chunk };
		await sleep(30);
	}
	yield {
		type: "final",
		ok: true,
		result: {
			output_text: "",
			token_usage: {
				prompt_tokens: 1200,
				completion_tokens: 90,
				total_tokens: 1290,
				prompt_cache_hit_tokens: 1000,
				context_window_max_tokens: 1048576,
				context_window_current_tokens: 1290,
			},
		},
	};
}

function split(text, size) {
	const out = [];
	for (let i = 0; i < text.length; i += size) out.push(text.slice(i, i + size));
	return out;
}
