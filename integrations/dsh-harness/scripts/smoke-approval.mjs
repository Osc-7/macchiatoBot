#!/usr/bin/env node
/**
 * smoke-approval.mjs —— 审批链路的离线自检。
 *
 * 覆盖两段：
 *   1. 事件映射：daemon 的 `feishu_permission_notify` 帧 → `{type:"permission"}` 事件
 *   2. 回执：给定 approval 服务的四种结果，断言插件发出正确的 `resolve_permission`
 *      （用假 unix socket 收请求，不连真 daemon）
 */
import { createServer } from "node:net";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const events = await import("../lib/lab/events.js");
const approval = await import("../lib/lab/approval.js");

let failures = 0;
const check = (label, ok, extra) => {
	if (!ok) failures += 1;
	console.log(`${ok ? "✓" : "✗"} ${label}${extra ? ` · ${extra}` : ""}`);
};

/* ---------- 1. 帧 → 事件 ---------- */
async function* frames(list) {
	for (const frame of list) yield frame;
}
const collected = [];
for await (const event of events.toLabEvents(frames([
	{ stream: true, event: "feishu_permission_notify", permission_id: "perm-1", payload: { summary: "写文件", tool_name: "bash" } },
	{ stream: true, event: "keepalive" },
	{ stream: true, event: "final" },
]))) {
	collected.push(event);
}
const permission = collected.find((event) => event.type === "permission");
check("notify 帧映射成 permission 事件", permission?.permissionId === "perm-1", JSON.stringify(permission ?? null));

/* ---------- 2. 回执：四种 outcome ---------- */
const dir = mkdtempSync(join(tmpdir(), "lab-approval-"));
const socketPath = join(dir, "daemon.sock");
const received = [];
const server = createServer((socket) => {
	socket.setEncoding("utf8");
	socket.on("data", (chunk) => {
		for (const line of chunk.split("\n")) {
			if (!line.trim()) continue;
			const request = JSON.parse(line);
			if (request.method !== "resolve_permission") continue;
			received.push(request.params);
			socket.write(JSON.stringify({ id: request.id, ok: true, result: { ok: true } }) + "\n");
		}
	});
});
await new Promise((resolve) => server.listen(socketPath, resolve));

process.env.LAB_MBOT_SOCK = socketPath;

function fakeAgent(outcome) {
	return {
		ctx: {
			approval: {
				request: async () => outcome,
			},
		},
	};
}

for (const [outcome, expectedAllowed] of [
	["allowed-once", true],
	["rejected", false],
	["cancelled", false],
	["unavailable", false],
]) {
	await approval.requestApproval({
		agent: fakeAgent(outcome),
		permissionId: `perm-${outcome}`,
		payload: { summary: "测试", tool_name: "bash", timeout_seconds: 30 },
		log: () => {},
	});
	const last = received[received.length - 1];
	check(`${outcome} → allowed=${expectedAllowed}`, last?.allowed === expectedAllowed, JSON.stringify(last));
}

/* 没有 approval 服务时也要 fail closed 并回执 */
await approval.requestApproval({
	agent: { ctx: {} },
	permissionId: "perm-none",
	payload: { summary: "无服务", timeout_seconds: 30 },
	log: () => {},
});
const none = received[received.length - 1];
check("没有审批服务时按拒绝回执", none?.allowed === false, JSON.stringify(none));

check("每条回执都带 permission_id", received.every((item) => typeof item.permission_id === "string" && item.permission_id.length > 0), `${received.length} 条`);

server.close();
rmSync(dir, { recursive: true, force: true });
console.log(failures === 0 ? "\n全部通过" : `\n${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
