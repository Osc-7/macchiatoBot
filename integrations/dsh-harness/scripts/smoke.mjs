#!/usr/bin/env node
/**
 * smoke.mjs — 不走 DSH，直接验证「线协议 + 事件映射」这一段。
 *
 *   LAB_MBOT_SOCK=/tmp/lab-mbot.sock node scripts/smoke.mjs "跑一个 pwd 看看"
 *
 * 这是 M2 的最短反馈环：如果这里的事件流是对的，插件里那套映射就是对的。
 */
import { labConfig, mbotIdentity } from "../lib/lab/config.js";
import { toLabEvents } from "../lib/lab/events.js";
import { MbotLink } from "../lib/lab/wire.js";

const text = process.argv.slice(2).join(" ") || "跑一个 pwd，然后把结果告诉我";
const config = labConfig();
const identity = mbotIdentity(`smoke-${Date.now()}`, config);

console.log(`socket   ${config.socket}`);
console.log(`session  ${identity.sessionId}`);
console.log(`prompt   ${text}\n`);

const started = Date.now();
const stamp = () => `${((Date.now() - started) / 1000).toFixed(2)}s`;

const link = await MbotLink.open(config.socket);
try {
	const switched = await link.request(
		"session_switch",
		{ client_id: identity.clientId, session_id: identity.sessionId },
		{ timeoutMs: 30_000 },
	);
	console.log(`switch   ${JSON.stringify(switched)}`);
	const usage = await link.request("get_token_usage", { session_id: identity.sessionId }, { timeoutMs: 15_000 });
	console.log(`usage0   ${JSON.stringify(usage?.usage ?? null)}\n`);

	const frames = link.stream("run_turn_stream", { client_id: identity.clientId, text }, { idleMs: 180_000 });
	let deltas = 0;
	for await (const event of toLabEvents(frames)) {
		switch (event.type) {
			case "text":
				process.stdout.write(event.text);
				deltas += 1;
				break;
			case "reasoning":
				deltas += 1;
				break;
			case "llm-request":
				process.stdout.write(`\n[${stamp()}] ── llm_request #${event.iteration} ──\n`);
				break;
			case "tool-call":
				process.stdout.write(`\n[${stamp()}] 🧰 ${event.name} ${event.arguments.slice(0, 160)}\n`);
				break;
			case "tool-result":
				process.stdout.write(
					`[${stamp()}] ${event.ok ? "✓" : "✗"} ${event.name} · ${String(event.text).slice(0, 200).replace(/\n/g, " ⏎ ")}\n`,
				);
				break;
			case "notice":
				process.stdout.write(`\n[${stamp()}] 📌 ${event.text}\n`);
				break;
			case "final": {
				const tokenUsage = event.result?.token_usage ?? {};
				console.log(
					`\n[${stamp()}] final ok=${event.ok} deltas=${deltas} ` +
						`tokens=${tokenUsage.prompt_tokens}+${tokenUsage.completion_tokens} ` +
						`ctx=${tokenUsage.context_window_current_tokens}/${tokenUsage.context_window_max_tokens}`,
				);
				if (event.ok === false) console.log(`error: ${JSON.stringify(event.error)}`);
				break;
			}
			default:
				break;
		}
	}
} finally {
	link.close();
}
