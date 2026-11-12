#!/usr/bin/env node
/**
 * patch-dsh-web-url.mjs —— 给 dsh 0.1.5 的 web 启动流程打两个小补丁。
 *
 * 背景：0.1.5 没有 `--public-url`（0.2.x 才有），而且每次启动都会重新生成
 * 一次性 launch token，导致：
 *   1. 日志里打印的是 http://127.0.0.1:18766/?token=…，在反向代理后面没法直接用；
 *   2. 每次重启 dsh 都换 token，手机/新设备要重新拿 URL。
 *
 * 补丁：
 *   A. `processLaunchToken()` 优先读 `DSH_WEB_TOKEN`，设了就固定用这个；
 *   B. 打印的 URL 若设了 `DSH_WEB_PUBLIC_URL`，用它替换本地 origin。
 *
 * 幂等：已经打过就跳过。dsh 升级后需要重跑（install.mjs 不碰 dsh 自身）。
 *
 *   node scripts/patch-dsh-web-url.mjs            # 打补丁
 *   node scripts/patch-dsh-web-url.mjs --check    # 只看状态
 *   node scripts/patch-dsh-web-url.mjs --revert   # 还原
 */
import { readFileSync, writeFileSync, copyFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const DSH_ROOT = process.env.DSH_INSTALL_ROOT ?? "/home/macchiato/dsh15/node_modules/@deepseek-ai/dsh";
const CONNECTION = join(DSH_ROOT, "node_modules/@deepseek-ai/dsh-client-connection/lib/index.js");
const WEBAPP = join(DSH_ROOT, "node_modules/@deepseek-ai/dsh-web-app/lib/index.js");

const MARK = "@lab-patched";

const CONNECTION_FROM = `function processLaunchToken(owner) {
	const existing = PROCESS_LAUNCH_TOKENS.get(owner);
	if (existing !== void 0) return existing;`;
const CONNECTION_TO = `function processLaunchToken(owner) {
	/* ${MARK}: DSH_WEB_TOKEN 固定 launch token，重启不换 */
	const fixedToken = (process.env.DSH_WEB_TOKEN ?? "").trim();
	if (fixedToken !== "") return fixedToken;
	const existing = PROCESS_LAUNCH_TOKENS.get(owner);
	if (existing !== void 0) return existing;`;

const WEBAPP_FROM = `if (config.printUrl) console.log(\`dsh web: \${authenticatedUrl}\${lanUrl === void 0 ? "" : \` (LAN: \${lanUrl})\`}\`);`;
const WEBAPP_TO = `if (config.printUrl) {
				/* ${MARK}: DSH_WEB_PUBLIC_URL 指定对外公布的地址（反向代理场景） */
				const publicUrl = (process.env.DSH_WEB_PUBLIC_URL ?? "").trim();
				const announced = publicUrl === "" ? authenticatedUrl : new URL(\`/?token=\${new URL(authenticatedUrl).searchParams.get("token") ?? ""}\`, publicUrl).href;
				console.log(\`dsh web: \${announced}\${lanUrl === void 0 ? "" : \` (LAN: \${lanUrl})\`}\`);
			}`;

function report(status) {
	const rows = [
		["launch token 固定 (DSH_WEB_TOKEN)", status.token],
		["公布地址 (DSH_WEB_PUBLIC_URL)", status.url],
	];
	for (const [label, ok] of rows) console.log(`${ok ? "✓" : "·"} ${label}`);
}

function main() {
	const mode = process.argv.includes("--revert") ? "revert" : process.argv.includes("--check") ? "check" : "apply";
	const connectionSource = readFileSync(CONNECTION, "utf8");
	const webappSource = readFileSync(WEBAPP, "utf8");
	const hasToken = connectionSource.includes(`${MARK}: DSH_WEB_TOKEN`);
	const hasUrl = webappSource.includes(`${MARK}: DSH_WEB_PUBLIC_URL`);

	if (mode === "check") {
		report({ token: hasToken, url: hasUrl });
		return 0;
	}

	if (mode === "revert") {
		if (hasToken) {
			copyFileSync(`${CONNECTION}.pre-lab`, CONNECTION);
			console.log("已还原 dsh-client-connection");
		}
		if (hasUrl) {
			copyFileSync(`${WEBAPP}.pre-lab`, WEBAPP);
			console.log("已还原 dsh-web-app");
		}
		if (!hasToken && !hasUrl) console.log("没有需要还原的补丁");
		return 0;
	}

	if (hasToken && hasUrl) {
		console.log("补丁已存在，跳过");
		report({ token: true, url: true });
		return 0;
	}

	if (!hasToken) {
		if (!connectionSource.includes(CONNECTION_FROM)) {
			console.error("找不到 processLaunchToken 的原文，dsh 版本可能变了；中止");
			return 1;
		}
		if (!existsSync(`${CONNECTION}.pre-lab`)) copyFileSync(CONNECTION, `${CONNECTION}.pre-lab`);
		writeFileSync(CONNECTION, connectionSource.replace(CONNECTION_FROM, CONNECTION_TO));
		console.log("已打补丁：launch token 固定");
	}

	if (!hasUrl) {
		if (!webappSource.includes(WEBAPP_FROM)) {
			console.error("找不到 URL 打印语句的原文，dsh 版本可能变了；中止");
			return 1;
		}
		if (!existsSync(`${WEBAPP}.pre-lab`)) copyFileSync(WEBAPP, `${WEBAPP}.pre-lab`);
		writeFileSync(WEBAPP, webappSource.replace(WEBAPP_FROM, WEBAPP_TO));
		console.log("已打补丁：公布地址");
	}

	report({ token: true, url: true });
	return 0;
}

process.exit(main());
