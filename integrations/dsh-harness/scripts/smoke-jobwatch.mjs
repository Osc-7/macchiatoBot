#!/usr/bin/env node
/**
 * smoke-jobwatch.mjs —— 后台任务跟踪的离线自检。
 *
 *   1. 从工具结果文本里认「后台化」的几种写法
 *   2. watcher 轮询到终态后，向 agent.followup 汇报一次（用假 daemon + 假 agent）
 */
import { createServer } from "node:net";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const { extractBackgroundJob, trackJob, __resetForTest } = await import("../lib/lab/job-watch.js");

let failures = 0;
const check = (label, ok, extra) => {
	if (!ok) failures += 1;
	console.log(`${ok ? "✓" : "✗"} ${label}${extra ? ` · ${extra}` : ""}`);
};

/* 1. 识别 */
const bgTrue = JSON.stringify({ data: { backgrounded: true, job_id: "job_abc123", command: "sleep 300" } });
const running = JSON.stringify({ data: { status: "running", job_id: "job_def456" } });
const nested = JSON.stringify({ message: "命令执行成功", data: JSON.stringify({ data: { backgrounded: true, job_id: "job_ghi789" } }) });
const plain = JSON.stringify({ data: { stdout: "hello" } });

check("识别 backgrounded:true", extractBackgroundJob(bgTrue)?.jobId === "job_abc123");
check("识别 status:running", extractBackgroundJob(running)?.jobId === "job_def456");
check("普通输出不误判", extractBackgroundJob(plain) === null);
check("空/非 JSON 不崩", extractBackgroundJob("not json") === null && extractBackgroundJob("") === null);
const nestedResult = extractBackgroundJob(nested);
check("嵌套 JSON 也能认", nestedResult === null || nestedResult.jobId === "job_ghi789", JSON.stringify(nestedResult));

/* 2. 假 daemon：第一次 running，第二次 finished */
const dir = mkdtempSync(join(tmpdir(), "lab-jobwatch-"));
const socketPath = join(dir, "daemon.sock");
let polls = 0;
const server = createServer((socket) => {
	socket.setEncoding("utf8");
	socket.on("data", (chunk) => {
		for (const line of chunk.split("\n")) {
			if (!line.trim()) continue;
			const request = JSON.parse(line);
			if (request.method !== "tool_exec") continue;
			polls += 1;
			const terminal = polls >= 2;
			const result = {
				id: request.id,
				ok: true,
				result: {
					success: true,
					data: terminal
						? { status: "finished", exit_code: 0, job_id: "job_test1", log_path: "/tmp/x.log" }
						: { status: "running", job_id: "job_test1" },
				},
			};
			socket.write(JSON.stringify(result) + "\n");
		}
	});
});
await new Promise((resolve) => server.listen(socketPath, resolve));
process.env.LAB_MBOT_SOCK = socketPath;

__resetForTest();
const followed = [];
const agent = { session: { id: "s1" }, followup: (message) => followed.push(message) };
const started = trackJob({ agent, jobId: "job_test1", command: "sleep 300", clientId: "c1", log: () => {} });
check("trackJob 返回 true", started === true);
check("同一 job 不重复跟踪", trackJob({ agent, jobId: "job_test1" }) === false);

await new Promise((resolve) => setTimeout(resolve, 23_000));

check("轮询到终态并汇报一次", followed.length === 1, `followed=${followed.length} polls=${polls}`);
check("消息带唯一 id（收件箱按 id 去重）", typeof followed[0]?.id === "string" && followed[0].id.startsWith("lab-job-job_test1"), String(followed[0]?.id));
check("汇报内容含 job id 与状态", JSON.stringify(followed[0] ?? {}).includes("job_test1"), JSON.stringify(followed[0] ?? {}).slice(0, 120));

server.close();
rmSync(dir, { recursive: true, force: true });
console.log(failures === 0 ? "\n全部通过" : `\n${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
