#!/usr/bin/env node
/**
 * fake-daemon.mjs — 一个「装死」的假 daemon，用来验证回合看门狗。
 *
 *   node scripts/fake-daemon.mjs /tmp/fake-mbot.sock [--silent-after 2] [--no-final]
 *
 * 行为：
 *   - session_switch / model_switch / get_token_usage / model_list 正常应答
 *   - run_turn_stream 先发几条 delta，然后**不发 final 也不断流**（模拟 daemon 卡死）
 *
 * 用 LAB_MBOT_SOCK=… LAB_MBOT_STALL_S=5 跑 dsh，前端应该 5 秒左右收敛成错误，
 * 而不是永远转圈 —— 这就是「回答进程老不停止」的回归测试。
 */
import { createServer } from "node:net";
import { rmSync } from "node:fs";

const socketPath = process.argv[2] || "/tmp/fake-mbot.sock";
const argOf = (name, fallback) => {
	const index = process.argv.indexOf(name);
	return index >= 0 ? Number(process.argv[index + 1]) : fallback;
};
const silentAfter = argOf("--silent-after", 2);
const sendFinal = process.argv.includes("--send-final");

try {
	rmSync(socketPath, { force: true });
} catch {
	/* ignore */
}

const server = createServer((socket) => {
	let buffer = "";
	let turnSocket = null;
	socket.setEncoding("utf8");
	const send = (frame) => {
		if (!socket.destroyed) socket.write(JSON.stringify(frame) + "\n");
	};

	socket.on("data", (text) => {
		buffer += text;
		let cut;
		while ((cut = buffer.indexOf("\n")) >= 0) {
			const line = buffer.slice(0, cut);
			buffer = buffer.slice(cut + 1);
			if (!line.trim()) continue;
			const request = JSON.parse(line);
			const { id, method } = request;
			if (method === "session_switch") send({ id, ok: true, result: { created: true, active_session_id: "fake" } });
			else if (method === "model_switch") send({ id, ok: true, result: { ok: true, info: { name: String(request.params?.name) } } });
			else if (method === "model_list")
				send({
					id,
					ok: true,
					result: {
						models: [
							{ name: "fake_flash", model: "fake-flash", label: "Fake Flash", base_url: "https://api.deepseek.com/v1", is_active: true, context_window: 1048576 },
						],
					},
				});
			else if (method === "get_token_usage")
				send({
					id,
					ok: true,
					result: {
						usage: {
							prompt_tokens: 0,
							completion_tokens: 0,
							total_tokens: 0,
							call_count: 0,
							context_window_max_tokens: 1048576,
							context_window_current_tokens: 0,
						},
					},
				});
			else if (method === "remote_workspace_status") send({ id, ok: true, result: { active: false, worker_online: null, state: null } });
			else if (method === "run_turn_stream") {
				turnSocket = socket;
				send({ id, stream: true, event: "llm_request" });
				let sent = 0;
				const tick = setInterval(() => {
					if (socket.destroyed) {
						clearInterval(tick);
						return;
					}
					if (sent < silentAfter) {
						send({
							id,
							stream: true,
							event: sent === 0 ? "reasoning_delta" : "assistant_delta",
							delta: sent === 0 ? "我先想一下……" : "正在处理，马上就好。",
						});
						sent += 1;
						return;
					}
					clearInterval(tick);
					if (sendFinal) send({ id, stream: true, event: "final", ok: true, result: { output_text: "" } });
					/* 不 final、不断流：模拟 daemon 卡死 */
				}, 200);
			} else send({ id, ok: true, result: {} });
		}
	});

	socket.on("close", () => {
		if (turnSocket === socket) process.stderr.write("[fake-daemon] 客户端断开了回合连接\n");
	});
	socket.on("error", () => {});
});

server.listen(socketPath, () => {
	process.stderr.write(`[fake-daemon] listening ${socketPath}（${silentAfter} 条增量后装死${sendFinal ? "，会发 final" : ""}）\n`);
});
