#!/usr/bin/env node
/**
 * smoke-model.mjs — 工作区模型（机器 → proj → 目录）的离线自检。
 *
 *   node scripts/smoke-model.mjs
 *
 * 用一个临时 DSH_HOME，跑一遍：模板生成、旧格式迁移、proj 解析、
 * 越界拦截、新增 proj。不依赖 daemon。
 */
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const home = mkdtempSync(join(tmpdir(), "lab-model-"));
process.env.DSH_HOME = home;

const model = await import("../lib/lab/workspace-model.js");

let failures = 0;
function check(label, condition, extra) {
	const ok = Boolean(condition);
	if (!ok) failures += 1;
	console.log(`${ok ? "✓" : "✗"} ${label}${extra ? ` · ${extra}` : ""}`);
}

/* 1. 首次运行写模板 */
const initial = model.listMachines();
check("首次运行生成机器模板", initial.machines.length >= 4, initial.machines.map((m) => m.name).join(","));
check("默认机器存在", initial.machines.some((m) => m.name === initial.default_machine), initial.default_machine);

/* 2. 显式三层 */
model.setBinding("s-model", { machine: "sii2", proj: "root", dir: "train/exp1" });
const resolved = model.resolveSessionWorkspace("s-model");
check("机器/proj/目录 解析", resolved?.root === "/root/train/exp1", resolved?.root);
check("解析带 login", resolved?.login === "sii2", resolved?.login);

/* 3. 旧写法的绝对路径优先（与升级前行为一致） */
model.setBinding("s-legacy", { machine: "sii2", proj: "", dir: "", legacyPath: "/tmp" });
const legacy = model.resolveSessionWorkspace("s-legacy");
check("legacy 绝对路径优先", legacy?.root === "/tmp" && legacy?.source === "legacy", `${legacy?.root}/${legacy?.source}`);

/* 4. 更早的字符串格式迁移 */
writeFileSync(join(home, "lab-mbot-workspaces.json"), JSON.stringify({ "s-string": "sii2:/somewhere/else" }));
const migrated = model.resolveSessionWorkspace("s-string");
check("字符串格式自动迁移", migrated?.root === "/somewhere/else", migrated?.root);

/* 5. 越界拦截 */
let blocked = false;
try {
	model.setBinding("s-escape", { machine: "sii2", proj: "root", dir: "../../etc" });
	model.resolveSessionWorkspace("s-escape");
} catch (error) {
	blocked = /\.\./.test(error.message);
}
check("目录含 .. 被拒绝", blocked);

/* 6. 新增 proj 并立即生效 */
const added = model.upsertProj("sii2", "exp", "/root/exp");
check("新增 proj 写入配置", added.ok === true, JSON.stringify(added));
model.setBinding("s-new", { machine: "sii2", proj: "exp", dir: "0712" });
const fresh = model.resolveSessionWorkspace("s-new");
check("新增 proj 可解析", fresh?.root === "/root/exp/0712", fresh?.root);

/* 7. 未知机器 + 绝对路径 → 仍可用 */
model.setBinding("s-unknown", { machine: "nope", proj: "", dir: "", legacyPath: "/data/x" });
const unknown = model.resolveSessionWorkspace("s-unknown");
check("未知机器按 legacy 兜底", unknown?.root === "/data/x", unknown?.root);

/* 8. 根路径越界判断 */
check("isInsideRoot 正例", model.isInsideRoot("/root/train", "/root/train/a/b") === true);
check("isInsideRoot 反例", model.isInsideRoot("/root/train", "/root/train2/a") === false);

rmSync(home, { recursive: true, force: true });
console.log(failures === 0 ? "\n全部通过" : `\n${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
