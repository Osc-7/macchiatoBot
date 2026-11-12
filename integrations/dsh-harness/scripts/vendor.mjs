#!/usr/bin/env node
/**
 * vendor.mjs — 从本机已安装的 dsh 里取 `@deepseek-ai/dsh-agent-loop`，
 * 定点打一处补丁，生成 `lib/index.js`。
 *
 * 为什么是「复制 + 定点改」而不是从零写一个 AgentFactory：
 * 见 NOTES.md —— `createAgent`/`resumeAgent` 背后有整套会话发布机制
 * （SessionPreparation / FactoryOwnership / appendUnstoredSuffix / inbox 投影），
 * 自己重写等于跟一个还在 rc 的包赛跑。
 *
 * 补丁只有两处（升级时 diff 一眼能看完）：
 *   1. 顶部加一行 import：`import { labStep } from "./lab/step.js";`
 *   2. `step()` 整个方法体换成一行委派：`return labStep(this, decision, {...})`
 *
 * 锚点只用「结构」不用注释原文，所以 0.1.5-rc.2 和 0.2.2-rc.1 都能打：
 *   - step() 起点：`\tasync step(decision) {`
 *   - step() 终点：后面第一个 `\tasync prepareRequest(` 之前的文档注释
 *
 * 用法：
 *   node scripts/vendor.mjs          # 生成 lib/index.js（覆盖）
 *   node scripts/vendor.mjs --check  # 校验 lib/index.js 是否与「现在安装的版本」一致
 *   node scripts/vendor.mjs --show   # 打印补丁点
 *
 * 环境变量 DSH_AGENT_LOOP_DIR 可显式指定包目录（服务器上是嵌套在 dsh 里的）。
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const PKG = "@deepseek-ai/dsh-agent-loop";

/** 新 step()：一行委派，逻辑全在 lib/lab/step.js（不必重新 vendor 就能改）。 */
const STEP_REPLACEMENT = [
	"\tasync step(decision) {",
	"\t\treturn labStep(this, decision, { AssistantStreamAttempt });",
	"\t}",
].join("\n");

const IMPORT_LINE = 'import { labStep } from "./lab/step.js";';
const STEP_START = "\tasync step(decision) {";
const STEP_NEXT_METHOD = "\n\tasync prepareRequest(";

const sha256 = (text) => createHash("sha256").update(text).digest("hex");

function fail(message) {
	console.error(`vendor: ${message}`);
	process.exit(1);
}

/** 依次在若干候选目录里找已安装的 dsh 包。 */
function findPackageDir() {
	const candidates = [
		process.env.DSH_AGENT_LOOP_DIR,
		/* 本地（桌面/CLI 安装）：共享的 profiles node_modules */
		join(homedir(), ".dsh/profiles/node_modules", PKG),
		/* 服务器（全局 npm 安装）：嵌套在 dsh 包里 */
		"/opt/node-v24.22.2-linux-x64/lib/node_modules/@deepseek-ai/dsh/node_modules",
		join(homedir(), ".workbuddy/binaries/node/workspace/node_modules", PKG),
		join(homedir(), ".workbuddy/binaries/node/workspace/node_modules/@deepseek-ai/dsh/node_modules", PKG),
	]
		.filter(Boolean)
		.map((candidate) => (candidate.endsWith(PKG) ? candidate : join(candidate, PKG)));
	for (const dir of candidates) {
		if (existsSync(join(dir, "package.json")) && existsSync(join(dir, "lib/index.js"))) return dir;
	}
	fail(`找不到已安装的 ${PKG}。找过：\n  ${candidates.join("\n  ")}\n有用 DSH_AGENT_LOOP_DIR=<包目录> 指定。`);
}

/** 用结构锚点切出 [前, step(), 后] 三段。 */
function splitStep(source) {
	const start = source.indexOf(STEP_START);
	if (start < 0) fail(`上游源码里找不到锚点 ${JSON.stringify(STEP_START)}（版本变了？）`);
	const next = source.indexOf(STEP_NEXT_METHOD, start);
	if (next < 0) fail(`step() 之后找不到 ${JSON.stringify(STEP_NEXT_METHOD)}（版本变了？）`);
	/* 把 preapareRequest 前面的文档注释留给 after，保证 step() 只吃到自己的右大括号 */
	const doc = source.lastIndexOf("\n\t/**", next);
	const end = doc > start ? doc + 1 : next;
	return { before: source.slice(0, start), step: source.slice(start, end), after: source.slice(end) };
}

/** 在最后一条 import 语句之后插入我们的 import。 */
function insertImport(source) {
	const region = source.indexOf("\n//#region");
	const head = region < 0 ? source : source.slice(0, region);
	const matches = [...head.matchAll(/^import [^\n]*;$/gm)];
	if (matches.length === 0) fail("上游源码里找不到 import 语句（版本变了？）");
	const last = matches[matches.length - 1];
	const at = last.index + last[0].length;
	return `${source.slice(0, at)}\n${IMPORT_LINE}${source.slice(at)}`;
}

function patchSource(source) {
	const { before, after, step } = splitStep(source);
	if (!step.includes("this.loopCtx.llm.stream(request)")) {
		console.warn("vendor: 警告 —— step() 里没看到 this.loopCtx.llm.stream(request)，请确认上游结构未变");
	}
	if (!step.includes("live.settle(") || !step.includes("AssistantStreamAttempt")) {
		fail("step() 的结构和预期不符（找不到 AssistantStreamAttempt / live.settle）");
	}
	return `${insertImport(before)}${STEP_REPLACEMENT}${after}`;
}

function readInstalled() {
	const dir = findPackageDir();
	const manifest = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
	return { dir, version: manifest.version, source: readFileSync(join(dir, "lib/index.js"), "utf8") };
}

const args = process.argv.slice(2);
const { dir, version, source } = readInstalled();
const patched = patchSource(source);
const indexPath = join(ROOT, "lib/index.js");
const upstreamPath = join(ROOT, "lib/upstream/index.js");
const manifestPath = join(ROOT, "lib/vendor.json");

if (args.includes("--show")) {
	const { step } = splitStep(source);
	console.log(`package  ${PKG}@${version}\nfrom     ${dir}`);
	console.log(`\n--- step() 将被替换（${step.split("\n").length} 行）---`);
	console.log(`${step.split("\n").slice(0, 4).join("\n")}\n\t...\n${step.split("\n").slice(-3).join("\n")}`);
	console.log(`\n--- 替换为 ---\n${STEP_REPLACEMENT}`);
	process.exit(0);
}

if (args.includes("--check")) {
	if (!existsSync(indexPath)) fail("lib/index.js 不存在，先运行 node scripts/vendor.mjs");
	const recorded = existsSync(manifestPath) ? JSON.parse(readFileSync(manifestPath, "utf8")) : null;
	if (recorded?.version !== version) {
		fail(`vendor 时的版本是 ${recorded?.version ?? "(无记录)"}，现在安装的是 ${version}；请重新运行 node scripts/vendor.mjs`);
	}
	if (sha256(readFileSync(indexPath, "utf8")) !== sha256(patched)) {
		fail("lib/index.js 与「刚 vendor 出来」的结果不一致；重新运行 node scripts/vendor.mjs");
	}
	console.log(`vendor --check ok · ${PKG}@${version} · ${sha256(patched).slice(0, 16)}`);
	process.exit(0);
}

mkdirSync(dirname(upstreamPath), { recursive: true });
writeFileSync(upstreamPath, source);
writeFileSync(indexPath, patched);
writeFileSync(
	manifestPath,
	JSON.stringify(
		{
			package: PKG,
			version,
			sourceDir: dir,
			upstreamSha256: sha256(source),
			patchedSha256: sha256(patched),
			patches: ["import labStep", "step() -> labStep(this, decision, {AssistantStreamAttempt})"],
			generatedAt: new Date().toISOString(),
		},
		null,
		2,
	) + "\n",
);
const removed = splitStep(source).step.split("\n").length;
console.log(
	`vendor ok · ${PKG}@${version}\n` +
		`  from ${dir}\n` +
		`  upstream ${source.length}B → ${sha256(source).slice(0, 16)}\n` +
		`  patched  ${patched.length}B → ${sha256(patched).slice(0, 16)}\n` +
		`  补丁: step() 去掉 ${removed} 行、加回 ${STEP_REPLACEMENT.split("\n").length} 行 + 1 行 import，其余逐字节保持原样`,
);
