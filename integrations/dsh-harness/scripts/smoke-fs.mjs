#!/usr/bin/env node
/**
 * smoke-fs.mjs —— lib/lab/fs-ops.js 的离线自检。
 *
 *   node scripts/smoke-fs.mjs
 *
 * 覆盖：本地后端（列表/读/二进制拦截/blob/写/越界）、远端脚本生成与解析
 * （list/read/blob/byte-window/stat）。远端那半用假 stdout，不连机器。
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const fsOps = await import("../lib/lab/fs-ops.js");

let failures = 0;
function check(label, condition, extra) {
	const ok = Boolean(condition);
	if (!ok) failures += 1;
	console.log(`${ok ? "✓" : "✗"} ${label}${extra ? ` · ${extra}` : ""}`);
}

/* ---------------------------------------------------------------- 本地后端 */
const root = mkdtempSync(join(tmpdir(), "lab-fs-"));
mkdirSync(join(root, "sub"));
writeFileSync(join(root, "a.txt"), "line1\nline2\nline3\n");
writeFileSync(join(root, "b.bin"), Buffer.from([0x00, 0x01, 0x02]));
writeFileSync(join(root, "c.pdf"), "%PDF-1.4 fake");

const local = fsOps.localBackend(root);

const listing = await local.list("");
check("本地 ls", listing.entries.some((e) => e.name === "a.txt") && listing.entries.some((e) => e.name === "sub" && e.type === "directory"), listing.entries.map((e) => e.name).join(","));

const page = await local.read("a.txt", { offset: 1, limit: 2 });
check("本地 分页读", page.text === "line1\nline2" && page.eof === false, JSON.stringify(page.text));

let binaryBlocked = false;
try {
	await local.read("b.bin");
} catch (error) {
	binaryBlocked = error.code === "binary";
}
check("本地 二进制拦截", binaryBlocked);

const blob = await local.blob("c.pdf");
check("本地 blob + mime", Buffer.from(blob.data, "base64").toString() === "%PDF-1.4 fake" && fsOps.mimeOf("c.pdf") === "application/pdf");

const written = await local.write("sub/new.txt", "hello 世界");
check("本地 写入", written.bytes > 0 && (await local.read("sub/new.txt")).text === "hello 世界");

let outsideBlocked = false;
try {
	await local.read("../etc/passwd");
} catch (error) {
	outsideBlocked = error.code === "outside-root";
}
check("本地 越界拦截", outsideBlocked);

/* ---------------------------------------------------------------- 远端脚本 */
check("quote 单引号安全", fsOps.quote("a'b") === `'a'\\''b'`);
check("resolveInRoot 正常", fsOps.resolveInRoot("/root", "train/x") === "/root/train/x");
check("resolveInRoot 拒绝 ..", (() => {
	try {
		fsOps.resolveInRoot("/root", "../x");
		return false;
	} catch (error) {
		return error.code === "outside-root";
	}
})());

const listScript = fsOps.scriptList("/root/tmp");
check("脚本 ls 带 -printf", listScript.includes("-printf") && listScript.includes("'/root/tmp'"));

const parsedList = fsOps.parseList("a.txt\tf\t12\nsub\td\t4096\n");
check("解析 ls", parsedList.entries.length === 2 && parsedList.entries[0].size === 12 && parsedList.entries[1].type === "directory");

const readStdout = `${Buffer.from("l1\nl2").toString("base64")}\n1000|1700000000\n9`;
const parsedRead = fsOps.parseRead(readStdout, 1);
check("解析 read", parsedRead.text === "l1\nl2" && parsedRead.version === "1700000000-1000", parsedRead.version);

let readNotFound = false;
try {
	fsOps.parseRead("@@MISSING\n\n0", 1);
} catch (error) {
	readNotFound = error.code === "not-found";
}
check("解析 read 缺失", readNotFound);

const blobStdout = `597|1700000000\n${Buffer.from("%PDF-1.4").toString("base64")}`;
const parsedBlob = fsOps.parseBlob(blobStdout);
check("解析 blob", Buffer.from(parsedBlob.data, "base64").toString() === "%PDF-1.4" && parsedBlob.bytes === 597);

let tooBig = false;
try {
	fsOps.parseBlob("@@TOOBIG|999999999");
} catch (error) {
	tooBig = error.code === "too-large";
}
check("解析 blob 超限", tooBig);

const window = fsOps.parseByteWindow(`${Buffer.from("abcdef").toString("base64")}\n6|1700000000`);
check("解析 字节窗口", Buffer.from(window.data, "base64").toString() === "abcdef");

const statLine = fsOps.scriptStat("/root/x");
check("脚本 stat 带分支", statLine.includes("@@MISSING") && statLine.includes("stat -c"));

/* ---------------------------------------------------------------- 收尾 */
rmSync(root, { recursive: true, force: true });
console.log(failures === 0 ? "\n全部通过" : `\n${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
