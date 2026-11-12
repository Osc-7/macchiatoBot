#!/usr/bin/env node
/**
 * mbot-sh — 在指定机器/目录里跑一条 shell 命令，走 daemon 的 tool_exec。
 *
 *   mbot-sh sii2:/root 'ls -la'
 *   mbot-sh g1:/home/jyj/bash 'du -sh *'
 *   mbot-sh :/tmp 'uname -a'          # 空机器名 = daemon 本机
 *
 * 为什么要它：daemon 的工具跑在会话绑定的工作区上，而 ttyd 终端在服务器本机，
 * 两者之间没有 ssh。这个脚本把「一条命令」翻译成 `remote_workspace_use` +
 * `tool_exec(bash)`，于是终端里也能直接操作 sii2/g1/a100。
 */
import { labConfig, parseWorkspace } from "../lib/lab/config.js";
import { MbotLink } from "../lib/lab/wire.js";

const args = process.argv.slice(2);
if (args.length === 0 || args[0] === "-h" || args[0] === "--help") {
	console.error("用法：mbot-sh <机器>:<路径> <命令…>   （空机器名 = daemon 本机）");
	process.exit(args.length === 0 ? 2 : 0);
}

const workspace = parseWorkspace(args[0]);
const command = args.slice(1).join(" ");
if (!workspace || !command) {
	console.error("用法：mbot-sh <机器>:<路径> <命令…>");
	process.exit(2);
}

const config = labConfig();
const stamp = `${process.pid}-${Date.now()}`;
const clientId = `mbot-sh:${stamp}`;
const sessionId = `dsh-sh-${stamp.slice(-12)}`;

let link;
try {
	link = await MbotLink.open(config.socket, { timeoutMs: 5_000 });
	await link.request("session_switch", { client_id: clientId, session_id: sessionId }, { timeoutMs: 20_000 });
	if (workspace.login) {
		const used = await link.request(
			"remote_workspace_use",
			{ client_id: clientId, login: workspace.login, path: workspace.path || "/root", profile: "dev" },
			{ timeoutMs: 40_000 },
		);
		if (used && used.ok === false) throw new Error(`${workspace.login} 拒绝工作区 ${workspace.path}`);
	}
	const result = await link.request(
		"tool_exec",
		{
			/* 关键：_dispatch 用 params.client_id 找 active_session，
			 * 不传就会落到 "default"，workspace 绑定全部失效、命令跑在 daemon 本机。 */
			client_id: clientId,
			tool: "bash",
			args: {
				command,
				description: "mbot-sh",
				timeout_seconds: Number(process.env.MBOT_SH_TIMEOUT || 120),
			},
		},
		{ timeoutMs: 180_000 },
	);
	const data = result?.data ?? {};
	/* tool_exec 目前只在 daemon 本机生效（工作区的 remote 状态不进 tool_exec 的上下文），
	 * 所以万一跑成了本机，要明确提示，别让人以为命令是在 sii2 上跑的。 */
	if (data && typeof data === "object" && data.remote === false && workspace.login) {
		process.stderr.write(
			`mbot-sh: 注意 —— 这条命令实际跑在 daemon 本机（remote=false）。` +
				`要走 ${workspace.login}，请用会话里的 /machine + 让 agent 执行，或用 tool_exec 之外的通道。\n`,
		);
	}
	const text = typeof data === "string" ? data : String(data.stdout ?? data.output ?? result?.message ?? "");
	if (text) process.stdout.write(text.endsWith("\n") ? text : `${text}\n`);
	if (data?.stderr) process.stderr.write(String(data.stderr));
	if (result?.success === false) {
		if (result?.error) process.stderr.write(`${JSON.stringify(result.error)}\n`);
		process.exitCode = 1;
	}
} catch (error) {
	console.error(`mbot-sh: ${error?.message || error}`);
	process.exitCode = 1;
} finally {
	link?.close();
}
