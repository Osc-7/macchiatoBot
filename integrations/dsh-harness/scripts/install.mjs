#!/usr/bin/env node
/**
 * install.mjs — 把本包装进一个 dsh profile，并把它挂到该 profile 的 bundle 层。
 *
 * 为什么不用 pnpm：本包依赖的 `@deepseek-ai/*` 必须和正在运行的 dsh 用同一份实例
 * （否则 `session.append`、`AssistantStreamAttempt` 这些结构体对不上）。
 * 直接把包放进 profile 的 node_modules，Node 会沿目录往上找到
 * `~/.dsh/profiles/node_modules/@deepseek-ai/*`，正是 dsh 自己用的那份。
 *
 * 用法：
 *   node scripts/install.mjs            # 装进 profile `lab`
 *   node scripts/install.mjs <profile>  # 装进指定 profile
 *   node scripts/install.mjs --list     # 列出 profile
 */
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const PKG = "@lab/dsh-agent-mbot";
const PKG_LOOP = "@deepseek-ai/dsh-agent-loop";

const home = process.env.DSH_HOME || join(homedir(), ".dsh");
const profilesDir = join(home, "profiles");

const profile = process.argv.slice(2).find((arg) => !arg.startsWith("-")) || "lab";

if (process.argv.includes("--list")) {
	console.log(existsSync(profilesDir) ? join(profilesDir, "*") : `${profilesDir} 不存在`);
	process.exit(0);
}

const profileDir = join(profilesDir, profile);
const target = join(profileDir, "node_modules", PKG);

if (!existsSync(join(profilesDir, "node_modules", "@deepseek-ai"))) {
	console.error(
		`找不到共享依赖 ${join(profilesDir, "node_modules", "@deepseek-ai")}。\n` +
			`先启动一次任意 profile（例如 \`dsh web --no-open\`），让 dsh 铺好共享 node_modules。`,
	);
	process.exit(1);
}

/* 0. 版本自检：lib/index.js 必须是为「这台机器上的 dsh-agent-loop」vendor 的 */
const vendorInfoPath = join(ROOT, "lib/vendor.json");
const installedLoop = join(profilesDir, "node_modules", "@deepseek-ai", "dsh-agent-loop", "package.json");
if (existsSync(vendorInfoPath) && existsSync(installedLoop)) {
	const vendored = JSON.parse(readFileSync(vendorInfoPath, "utf8"));
	const installed = JSON.parse(readFileSync(installedLoop, "utf8"));
	if (vendored.version !== installed.version) {
		console.error(
			`lib/index.js 是为 ${PKG_LOOP}@${vendored.version} vendor 的，这台机器装的是 ${installed.version}。\n` +
				`先跑一遍：node scripts/vendor.mjs`,
		);
		process.exit(1);
	}
}

/* 1. 拷包（去掉 node_modules 链接和脚本，只留运行时） */
rmSync(target, { recursive: true, force: true });
mkdirSync(target, { recursive: true });
cpSync(ROOT, target, {
	recursive: true,
	filter: (source) => {
		const rel = source.slice(ROOT.length + 1);
		if (rel.startsWith("node_modules")) return false;
		if (rel.startsWith("scripts")) return false;
		if (rel.endsWith(".md")) return false;
		return true;
	},
});

/* 2. 确保 profile 存在，且把本包列进 bundles（bundle 自己带 cordis.patch.yml） */
mkdirSync(profileDir, { recursive: true });
const manifestPath = join(profileDir, "package.json");
const manifest = existsSync(manifestPath)
	? JSON.parse(readFileSync(manifestPath, "utf8"))
	: {
			name: `dsh-profile-${profile}`,
			private: true,
			dependencies: {},
			dsh: { profile: { bundles: ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"], patchReload: "live" } },
		};
const bundles = manifest.dsh?.profile?.bundles;
if (!Array.isArray(bundles)) {
	console.error(`${manifestPath} 里没有 dsh.profile.bundles，拒绝改别人的 profile。`);
	process.exit(1);
}
if (!bundles.includes(PKG)) bundles.push(PKG);
writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + "\n");

/* 3. 空的 patch 层（用户覆盖点）与根 cordis.yml */
const patchPath = join(profileDir, "cordis.patch.yml");
if (!existsSync(patchPath)) {
	writeFileSync(
		patchPath,
		"# 你的 patch 层：在 bundle 之后应用（覆盖配置 / 禁用 / 插入）\n" +
			"# 本 profile 的 mbot 接入在 @lab/dsh-agent-mbot 这个 bundle 里，不要重复写。\n[]\n",
	);
}
const rootPath = join(profileDir, "cordis.yml");
if (!existsSync(rootPath)) writeFileSync(rootPath, "# 由 profile 的 bundle + patch 组合，不要手改\n[]\n");

/* 4. dsh 自身的两个小补丁（固定 token / 公布地址）：升级 dsh 后要重打 */
const patchScript = join(ROOT, "scripts", "patch-dsh-web-url.mjs");
if (existsSync(patchScript) && !process.argv.includes("--no-dsh-patch")) {
	const result = spawnSync(process.execPath, [patchScript], { stdio: "inherit" });
	if (result.status !== 0) console.warn("（dsh web-url 补丁没打上，可手动跑 scripts/patch-dsh-web-url.mjs）");
}

console.log(
	`installed ${PKG} → ${target}\n` +
		`profile ${profile}: bundles = ${bundles.join(", ")}\n\n` +
		`启动（0.1.5 不接受 \`--profile x web\`，直接给 port 即可）：\n` +
		`  dsh --profile ${profile} --no-open --port 18766\n\n` +
		`部署与排障说明见 docs/dsh-harness-bridge.md\n`,
);
