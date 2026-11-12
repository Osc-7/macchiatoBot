/**
 * 文件操作：本地与远程各一个后端，接口一致。
 *
 * - 本地：直接 `node:fs/promises`
 * - 远程：通过 daemon 的 `tool_exec(bash)`（`channel.run(script)`）
 *
 * 两条路径都做同一件事：把「客户端给的相对路径」拼到 root 上、
 * 规范化、校验不越界，然后执行。宿主侧（workspaceFiles 覆盖层）与
 * WS 桥（独立进程）共用这一份，避免两套 bash 片段漂移。
 *
 * 只依赖 node 内置模块，WS 桥可以直接 import。
 */
import { readdir, readFile, stat as fsStat, writeFile, mkdir } from "node:fs/promises";
import { posix } from "node:path";

export const LIMITS = {
	/** 一次文本读取的最大字节数（超过就报 too-large） */
	maxBytes: 2 * 1024 * 1024,
	/** 整读二进制（PDF/图片）的上限 */
	maxBlobBytes: 32 * 1024 * 1024,
	/** 目录一次最多返回多少项 */
	maxEntries: 2000,
};

/** shell 单引号安全转义。 */
export function quote(value) {
	return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function fail(code, message, extra = {}) {
	const error = new Error(message);
	error.code = code;
	Object.assign(error, extra);
	return error;
}

/** 相对路径 → root 内的绝对路径；越界直接抛。 */
export function resolveInRoot(root, relative) {
	const base = posix.normalize(String(root || "/"));
	const raw = String(relative ?? "").trim().replace(/\\/g, "/");
	const absolute = raw.startsWith("/") ? posix.normalize(raw) : posix.normalize(posix.join(base, raw));
	const inside = absolute === base || absolute.startsWith(`${base.replace(/\/+$/, "")}/`);
	if (!inside) throw fail("outside-root", `路径越出工作区根：${relative}`, { path: relative });
	return absolute;
}

/** 相对 root 的展示路径。 */
export function relativeToRoot(root, absolute) {
	const base = posix.normalize(String(root || "/")).replace(/\/+$/, "");
	if (absolute === base) return "";
	return absolute.startsWith(`${base}/`) ? absolute.slice(base.length + 1) : absolute;
}

const MIME_BY_EXT = {
	pdf: "application/pdf",
	png: "image/png",
	jpg: "image/jpeg",
	jpeg: "image/jpeg",
	gif: "image/gif",
	webp: "image/webp",
	svg: "image/svg+xml",
	bmp: "image/bmp",
	ico: "image/x-icon",
	mp4: "video/mp4",
	webm: "video/webm",
	mp3: "audio/mpeg",
	wav: "audio/wav",
	zip: "application/zip",
	gz: "application/gzip",
	tar: "application/x-tar",
	json: "application/json",
	woff: "font/woff",
	woff2: "font/woff2",
	ttf: "font/ttf",
};

export function mimeOf(path) {
	const ext = String(path).split(".").pop()?.toLowerCase() ?? "";
	return MIME_BY_EXT[ext] ?? "application/octet-stream";
}

// ---------------------------------------------------------------- bash 片段

export function scriptList(dir, maxEntries = LIMITS.maxEntries) {
	const q = quote(dir);
	return (
		`d=${q}; [ -d "$d" ] || { echo '@@NOTDIR'; exit 0; }; ` +
		`find "$d" -mindepth 1 -maxdepth 1 -printf '%f\\t%Y\\t%s\\n' 2>/dev/null | sort | head -n ${maxEntries + 1}`
	);
}

export function parseList(stdout, maxEntries = LIMITS.maxEntries) {
	if (String(stdout).startsWith("@@NOTDIR")) throw fail("not-dir", "不是一个目录");
	const rows = String(stdout).split("\n").filter((line) => line.length > 0);
	const truncated = rows.length > maxEntries;
	const entries = rows.slice(0, maxEntries).map((line) => {
		const [name, typeChar, size] = line.split("\t");
		const type = typeChar === "d" ? "directory" : typeChar === "f" ? "file" : "other";
		return type === "file" ? { name, type, size: Number(size) || 0 } : { name, type };
	});
	return { entries, truncated };
}

export function scriptRead(path, offset, limit) {
	const q = quote(path);
	const end = offset + limit - 1;
	return (
		`f=${q}; [ -e "$f" ] || { echo '@@MISSING'; exit 0; }; ` +
		`[ -f "$f" ] || { echo '@@NOTFILE'; exit 0; }; ` +
		`awk -v s=${offset} -v e=${end} 'NR>=s && NR<=e' "$f" | base64 -w0; echo; ` +
		`stat -c '%s|%Y' "$f"; ` +
		`awk 'END{print NR}' "$f"`
	);
}

export function parseRead(stdout, offset) {
	const [b64 = "", statLine = "", totalRaw = "0"] = String(stdout).split("\n");
	if (b64 === "@@MISSING" || statLine === "@@MISSING") throw fail("not-found", "文件不存在");
	if (b64 === "@@NOTFILE") throw fail("not-file", "不是普通文件");
	const buffer = Buffer.from(b64, "base64");
	const text = buffer.toString("utf8");
	if (text.includes("\0")) throw fail("binary", "这是二进制文件");
	if (buffer.byteLength > LIMITS.maxBytes) throw fail("too-large", "这一页太大了");
	const [sizeRaw, mtimeRaw] = statLine.split("|");
	const total = Number(String(totalRaw).trim()) || 0;
	const lines = text.length === 0 ? 0 : text.split("\n").length;
	return {
		text,
		version: `${mtimeRaw}-${sizeRaw}`,
		bytes: Number(sizeRaw) || 0,
		lines,
		eof: offset + Math.max(lines, 1) - 1 >= total,
	};
}

export function scriptBlob(path, maxBytes = LIMITS.maxBlobBytes) {
	const q = quote(path);
	return (
		`f=${q}; [ -f "$f" ] || { echo '@@NOTFILE'; exit 0; }; ` +
		`s=$(stat -c %s "$f"); if [ "$s" -gt ${maxBytes} ]; then echo "@@TOOBIG|$s"; exit 0; fi; ` +
		`stat -c '%s|%Y' "$f"; base64 -w0 "$f"; echo`
	);
}

export function parseBlob(stdout) {
	const text = String(stdout);
	if (text.startsWith("@@NOTFILE")) throw fail("not-file", "不是普通文件");
	if (text.startsWith("@@TOOBIG|")) throw fail("too-large", "文件太大，超过整读上限");
	const newline = text.indexOf("\n");
	const statLine = newline < 0 ? text : text.slice(0, newline);
	const [sizeRaw, mtimeRaw] = statLine.split("|");
	return {
		data: newline < 0 ? "" : text.slice(newline + 1).trim(),
		bytes: Number(sizeRaw) || 0,
		version: `${mtimeRaw}-${sizeRaw}`,
	};
}

export function scriptWrite(path, base64) {
	const q = quote(path);
	return `f=${q}; d=$(dirname "$f"); mkdir -p "$d"; printf '%s' ${quote(base64)} | base64 -d > "$f" && stat -c '%s|%Y' "$f"`;
}

export function scriptByteWindow(path, offset, length) {
	const q = quote(path);
	return `f=${q}; [ -f "$f" ] || { echo '@@NOTFILE'; exit 0; }; dd if="$f" bs=1 skip=${offset} count=${length} 2>/dev/null | base64 -w0; echo; stat -c '%s|%Y' "$f"`;
}

export function parseByteWindow(stdout) {
	const [dataRaw = "", statLine = ""] = String(stdout).split("\n");
	if (dataRaw === "@@NOTFILE") throw fail("not-file", "不是普通文件");
	const [sizeRaw, mtimeRaw] = statLine.split("|");
	const data = dataRaw.trim();
	const size = Number(sizeRaw) || 0;
	return { data, bytes: size, version: `${mtimeRaw}-${sizeRaw}` };
}

export function scriptStat(path) {
	const q = quote(path);
	return `f=${q}; if [ ! -e "$f" ]; then echo '@@MISSING'; elif [ -L "$f" ] && [ ! -e "$f" ]; then echo '@@BROKEN'; else stat -c '%s|%Y|%F' "$f"; fi`;
}

// ---------------------------------------------------------------- 后端

/**
 * 本地后端：root 之内的普通文件系统操作。
 * @param {string} root - 绝对根目录
 */
export function localBackend(root) {
	return {
		kind: "local",
		root,
		async list(relative, { maxEntries = LIMITS.maxEntries } = {}) {
			const dir = resolveInRoot(root, relative);
			let dirents;
			try {
				dirents = await readdir(dir, { withFileTypes: true });
			} catch (error) {
				if (error.code === "ENOTDIR") throw fail("not-dir", "不是一个目录");
				if (error.code === "ENOENT") throw fail("not-found", "目录不存在");
				throw error;
			}
			const rows = [];
			for (const dirent of dirents.sort((a, b) => a.name.localeCompare(b.name))) {
				const type = dirent.isDirectory() ? "directory" : dirent.isFile() ? "file" : "other";
				if (type !== "file") {
					rows.push({ name: dirent.name, type });
					continue;
				}
				let size = 0;
				try {
					size = (await fsStat(posix.join(dir, dirent.name))).size;
				} catch {
					/* 竞态删除就算了 */
				}
				rows.push({ name: dirent.name, type, size });
			}
			const truncated = rows.length > maxEntries;
			return { entries: rows.slice(0, maxEntries), truncated };
		},
		async read(relative, { offset = 1, limit = 2000 } = {}) {
			const file = resolveInRoot(root, relative);
			const info = await fsStat(file).catch(() => null);
			if (!info) throw fail("not-found", "文件不存在");
			if (!info.isFile()) throw fail("not-file", "不是普通文件");
			const content = await readFile(file);
			if (content.byteLength > LIMITS.maxBytes) throw fail("too-large", "文件太大，按页读");
			const text = content.toString("utf8");
			if (text.includes("\0")) throw fail("binary", "这是二进制文件");
			const all = text.split("\n");
			const page = all.slice(offset - 1, offset - 1 + limit);
			return {
				text: page.join("\n"),
				version: `${Math.floor(info.mtimeMs / 1000)}-${info.size}`,
				bytes: info.size,
				lines: page.length,
				eof: offset - 1 + page.length >= all.length,
			};
		},
		async blob(relative) {
			const file = resolveInRoot(root, relative);
			const info = await fsStat(file).catch(() => null);
			if (!info) throw fail("not-found", "文件不存在");
			if (!info.isFile()) throw fail("not-file", "不是普通文件");
			if (info.size > LIMITS.maxBlobBytes) throw fail("too-large", "文件太大，超过整读上限");
			const data = await readFile(file);
			return { data: data.toString("base64"), bytes: info.size, version: `${Math.floor(info.mtimeMs / 1000)}-${info.size}` };
		},
		async write(relative, text) {
			const file = resolveInRoot(root, relative);
			await mkdir(posix.dirname(file), { recursive: true });
			const buffer = Buffer.from(String(text ?? ""), "utf8");
			if (buffer.byteLength > LIMITS.maxBytes) throw fail("too-large", "写入内容超过上限");
			await writeFile(file, buffer);
			const info = await fsStat(file);
			return { bytes: info.size, version: `${Math.floor(info.mtimeMs / 1000)}-${info.size}` };
		},
		async stat(relative) {
			const file = resolveInRoot(root, relative);
			const info = await fsStat(file).catch(() => null);
			if (!info) return undefined;
			return { bytes: info.size, version: `${Math.floor(info.mtimeMs / 1000)}-${info.size}`, type: info.isDirectory() ? "directory" : info.isFile() ? "file" : "other" };
		},
	};
}

/**
 * 远程后端：所有操作拼成 bash，通过 daemon 的 tool_exec 执行。
 * @param {{run: (script: string, opts?: object) => Promise<string>}} channel
 * @param {string} root - 远端绝对根目录
 */
export function remoteBackend(channel, root) {
	return {
		kind: "remote",
		root,
		async list(relative, { maxEntries = LIMITS.maxEntries } = {}) {
			const dir = resolveInRoot(root, relative);
			const stdout = await channel.run(scriptList(dir, maxEntries), { timeoutMs: 60_000 });
			return parseList(stdout, maxEntries);
		},
		async read(relative, { offset = 1, limit = 2000 } = {}) {
			const file = resolveInRoot(root, relative);
			const stdout = await channel.run(scriptRead(file, offset, limit), { timeoutMs: 60_000 });
			return parseRead(stdout, offset);
		},
		async blob(relative) {
			const file = resolveInRoot(root, relative);
			const stdout = await channel.run(scriptBlob(file), { timeoutMs: 120_000 });
			return parseBlob(stdout);
		},
		async write(relative, text) {
			const file = resolveInRoot(root, relative);
			const buffer = Buffer.from(String(text ?? ""), "utf8");
			if (buffer.byteLength > LIMITS.maxBytes) throw fail("too-large", "写入内容超过上限");
			const stdout = await channel.run(scriptWrite(file, buffer.toString("base64")), { timeoutMs: 90_000 });
			const [sizeRaw, mtimeRaw] = String(stdout).trim().split("|");
			return { bytes: Number(sizeRaw) || buffer.byteLength, version: `${mtimeRaw}-${sizeRaw}` };
		},
		async stat(relative) {
			const file = resolveInRoot(root, relative);
			const stdout = (await channel.run(scriptStat(file), { timeoutMs: 30_000 })).trim();
			if (!stdout || stdout.startsWith("@@")) return undefined;
			const [sizeRaw, mtimeRaw, kind] = stdout.split("|");
			return {
				bytes: Number(sizeRaw) || 0,
				version: `${mtimeRaw}-${sizeRaw}`,
				type: kind === "directory" ? "directory" : kind === "regular file" ? "file" : "other",
			};
		},
	};
}
