#!/usr/bin/env node
/**
 * smoke-stall.mjs —— 验证「模型卡住但 daemon 一直发心跳」时能判定卡死。
 *
 *   node scripts/smoke-stall.mjs
 *
 * 起一个假的 unix socket，对 run_turn_stream 只回 keepalive（每 100ms 一次），
 * 然后断言 MbotLink.stream 在 idleMs 之后抛 MBOT_STALLED，而不是无限等待。
 */
import { createServer } from "node:net";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const wire = await import("../lib/lab/wire.js");
const { MbotLink } = wire;

const dir = mkdtempSync(join(tmpdir(), "lab-stall-"));
const socketPath = join(dir, "daemon.sock");

let failures = 0;
const check = (label, ok, extra) => {
	if (!ok) failures += 1;
	console.log(`${ok ? "✓" : "✗"} ${label}${extra ? ` · ${extra}` : ""}`);
};

/* 假 daemon：收到 run_turn_stream 就每 100ms 发一个 keepalive，永不结束 */
const server = createServer((socket) => {
	socket.setEncoding("utf8");
	socket.on("data", (chunk) => {
		for (const line of chunk.split("\n")) {
			if (!line.trim()) continue;
			let request;
			try {
				request = JSON.parse(line);
			} catch {
				continue;
			}
			if (request.method !== "run_turn_stream") continue;
			const timer = setInterval(() => {
				if (socket.destroyed) {
					clearInterval(timer);
					return;
				}
				socket.write(JSON.stringify({ id: request.id, stream: true, event: "keepalive" }) + "\n");
			}, 100);
			socket.on("close", () => clearInterval(timer));
		}
	});
});
await new Promise((resolve) => server.listen(socketPath, resolve));

const link = await MbotLink.open(socketPath, { timeoutMs: 3_000 });
const startedAt = Date.now();
let error = null;
let heartbeats = 0;
try {
	for await (const frame of link.stream("run_turn_stream", { client_id: "t", text: "hi" }, {
		idleMs: 800,
		timeoutMs: 1_000,
		isProgress: (frame) => frame.event !== "keepalive",
	})) {
		if (frame.event === "keepalive") heartbeats += 1;
	}
} catch (failure) {
	error = failure;
}
const elapsed = Date.now() - startedAt;

check("心跳会被正常消费", heartbeats > 0, `${heartbeats} 个`);
check("只发心跳时会判定卡死", error?.code === "MBOT_STALLED", error?.message);
check("判定时间接近 idleMs（不被心跳刷新）", elapsed >= 700 && elapsed < 2_500, `${elapsed}ms`);


/* 对照：工具执行阶段应该按 toolMs 放行，而不是 180 秒就杀 */
const link3 = await MbotLink.open(socketPath, { timeoutMs: 3_000 });
let toolError = null;
let frames = 0;
const startedAt3 = Date.now();
try {
	for await (const frame of link3.stream("run_turn_stream", { client_id: "t3" }, {
		timeoutMs: 1_000,
		/* 模拟：工具在跑 → 用 2.5 秒的工具预算；等模型只有 0.8 秒 */
		idleMs: () => (frames > 0 ? 5_000 : 800),
		isProgress: (frame) => frame.event !== "keepalive",
	})) {
		frames += 1;
		if (Date.now() - startedAt3 > 3_000) break;
	}
} catch (failure) {
	toolError = failure;
}
check("工具执行期用长预算时不会被短超时误杀", toolError === null, toolError?.message ?? `跑了 ${Date.now() - startedAt3}ms`);
link3.close();

link.close();
server.close();
rmSync(dir, { recursive: true, force: true });

console.log(failures === 0 ? "\n全部通过" : `\n${failures} 项失败`);
/* 假 daemon 的定时器可能还在，直接退出，别等事件循环 */
process.exit(failures === 0 ? 0 : 1);
