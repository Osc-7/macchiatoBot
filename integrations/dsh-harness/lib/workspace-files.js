/**
 * 远程工作区的文件读取实现：把官方 `workspaceFiles` 服务的读文件方法换成
 * 「会话用 /machine 绑过远程 → 走 daemon 的 `tool_exec(bash)` 读远端；否则调用原实现」。
 *
 * 由 `workspace-files-patch.js` 在宿主侧调用；本文件只做实现，不注册插件。
 *
 * 两个约束（都踩过）：
 *   1. 不能用 `#private` 字段/方法 —— cordis 的服务代理会把 `this` 换成 shadow 对象，
 *      V8 的私有 brand check 会抛 `Receiver must be an instance of class ...`。
 *   2. 补上去的函数是**闭包**，不读 `this`；需要的配置在安装时抓一次。
 */
import { RemoteError } from "@deepseek-ai/dsh-typert-protocol";
import { remoteChannel } from "./lab/remote-fs.js";
import { resolveSessionWorkspace } from "./lab/workspace-model.js";

function quote(value) {
	return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

/** 这个会话绑了哪台机器（机器/proj/目录三层解析）；没绑返回 null（回落到官方实现）。 */
function remoteWorkspaceOf(sessionId) {
	const selection = resolveSessionWorkspace(sessionId);
	return selection && selection.login ? selection : null;
}

/**
 * 在官方服务实例上安装远程覆盖。
 * @param service - 已被 cordis 注册的 `workspaceFiles` 服务实例
 */
export function installRemoteWorkspaceFiles(service) {
	const config = service.config ?? {};
	const maxBytes = Number(config.maxBytes) || 2 * 1024 * 1024;
	const maxFileBytes = Number(config.maxFileBytes) || 32 * 1024 * 1024;
	const maxLines = Number(config.maxLines) || 5000;
	const maxEntries = Number(config.maxEntries) || 2000;

	/** 官方实现（绑定好 this，代理下也能正常调用）。 */
	const base = {
		read: service.read.bind(service),
		readBytes: service.readBytes.bind(service),
		readAll: service.readAll.bind(service),
		readRelated: service.readRelated.bind(service),
		stat: service.stat.bind(service),
		list: service.list.bind(service),
	};

	const overrides = {
		async read(workspaceFileScope, path, range, signal) {
			const workspace = remoteWorkspaceOf(workspaceFileScope?.sessionId);
			if (!workspace) return base.read(workspaceFileScope, path, range, signal);
			const channel = await remoteChannel({ key: String(workspaceFileScope.sessionId), login: workspace.login, path: workspace.root });
			if (!channel) return base.read(workspaceFileScope, path, range, signal);
			const limit = Math.min(Number(range?.limit ?? maxLines) || maxLines, maxLines);
			const offset = Math.max(1, Number(range?.offset ?? 1) || 1);
			const end = offset + limit - 1;
			const stdout = await channel.run(
				`f=${quote(path)}; ` +
					`[ -e "$f" ] || { echo '@@MISSING'; exit 0; }; ` +
					`[ -f "$f" ] || { echo '@@NOTFILE'; exit 0; }; ` +
					`awk -v s=${offset} -v e=${end} 'NR>=s && NR<=e' "$f" | base64 -w0; echo; ` +
					`stat -c '%s|%Y' "$f"; ` +
					`awk 'END{print NR}' "$f"`,
				{ timeoutMs: 60_000 },
			);
			const [pageB64 = "", statLine = "", totalRaw = "0"] = stdout.split("\n");
			if (pageB64 === "@@MISSING" || statLine === "@@MISSING") {
				throw new RemoteError("workspace-file/not-found", `"${path}" 在 ${workspace.login} 上不存在`, { path });
			}
			if (pageB64 === "@@NOTFILE") {
				throw new RemoteError("workspace-file/not-regular-file", `"${path}" 不是普通文件`, { path, kind: "other" });
			}
			const text = Buffer.from(pageB64, "base64").toString("utf8");
			if (text.includes("\0")) throw new RemoteError("workspace-file/not-text", `"${path}" 是二进制`, { path });
			if (Buffer.byteLength(text, "utf8") > maxBytes) {
				throw new RemoteError("workspace-file/too-large", `"${path}" 这一页超过 ${maxBytes} 字节`, { path, limit: maxBytes });
			}
			const [sizeRaw, mtimeRaw] = statLine.split("|");
			const total = Number(totalRaw.trim()) || 0;
			const lines = text.length === 0 ? 0 : text.split("\n").length;
			return {
				absolutePath: String(path),
				version: `${mtimeRaw}-${sizeRaw}`,
				bytes: Number(sizeRaw) || 0,
				offset,
				text,
				lines,
				eof: offset + Math.max(lines, 1) - 1 >= total,
			};
		},

		async readBytes(workspaceFileScope, path, range, signal) {
			const workspace = remoteWorkspaceOf(workspaceFileScope?.sessionId);
			if (!workspace) return base.readBytes(workspaceFileScope, path, range, signal);
			const channel = await remoteChannel({ key: String(workspaceFileScope.sessionId), login: workspace.login, path: workspace.root });
			if (!channel) return base.readBytes(workspaceFileScope, path, range, signal);
			const offset = Math.max(0, Number(range?.offset ?? 0) || 0);
			const length = Math.min(Number(range?.length ?? maxBytes) || maxBytes, maxBytes);
			const stdout = await channel.run(
				`f=${quote(path)}; [ -f "$f" ] || { echo '@@NOTFILE'; exit 0; }; ` +
					`dd if="$f" bs=1 skip=${offset} count=${length} 2>/dev/null | base64 -w0; echo; ` +
					`stat -c '%s|%Y' "$f"`,
				{ timeoutMs: 60_000 },
			);
			const [dataRaw = "", statLine = ""] = stdout.split("\n");
			if (dataRaw === "@@NOTFILE") {
				throw new RemoteError("workspace-file/not-regular-file", `"${path}" 不是普通文件`, { path, kind: "other" });
			}
			const [sizeRaw, mtimeRaw] = statLine.split("|");
			const size = Number(sizeRaw) || 0;
			const data = dataRaw.trim();
			return {
				absolutePath: String(path),
				version: `${mtimeRaw}-${sizeRaw}`,
				bytes: size,
				offset,
				data,
				eof: offset + Math.floor((data.length * 3) / 4) >= size,
			};
		},

		async readAll(workspaceFileScope, path, signal) {
			const workspace = remoteWorkspaceOf(workspaceFileScope?.sessionId);
			if (!workspace) return base.readAll(workspaceFileScope, path, signal);
			const channel = await remoteChannel({ key: String(workspaceFileScope.sessionId), login: workspace.login, path: workspace.root });
			if (!channel) return base.readAll(workspaceFileScope, path, signal);
			const stdout = await channel.run(
				`f=${quote(path)}; [ -f "$f" ] || { echo '@@NOTFILE'; exit 0; }; ` +
					`stat -c '%s|%Y' "$f"; base64 -w0 "$f"; echo`,
				{ timeoutMs: 90_000 },
			);
			const newline = stdout.indexOf("\n");
			const statLine = newline < 0 ? stdout : stdout.slice(0, newline);
			if (statLine === "@@NOTFILE") {
				throw new RemoteError("workspace-file/not-regular-file", `"${path}" 不是普通文件`, { path, kind: "other" });
			}
			const [sizeRaw, mtimeRaw] = statLine.split("|");
			const size = Number(sizeRaw) || 0;
			if (size > maxFileBytes) {
				throw new RemoteError("workspace-file/too-large", `"${path}" 有 ${size} 字节，超过 ${maxFileBytes}`, {
					path,
					limit: maxFileBytes,
				});
			}
			return {
				absolutePath: String(path),
				version: `${mtimeRaw}-${sizeRaw}`,
				bytes: size,
				offset: 0,
				data: (newline < 0 ? "" : stdout.slice(newline + 1)).trim(),
				eof: true,
			};
		},

		async readRelated(workspaceFileScope, path, relativePath, signal) {
			const workspace = remoteWorkspaceOf(workspaceFileScope?.sessionId);
			if (!workspace) return base.readRelated(workspaceFileScope, path, relativePath, signal);
			const joined = `${String(path).replace(/\/[^/]*$/, "")}/${relativePath}`.replace(/\/\.\//g, "/");
			return overrides.readAll(workspaceFileScope, joined, signal);
		},

		async stat(workspaceFileScope, path, signal) {
			const workspace = remoteWorkspaceOf(workspaceFileScope?.sessionId);
			if (!workspace) return base.stat(workspaceFileScope, path, signal);
			const channel = await remoteChannel({ key: String(workspaceFileScope.sessionId), login: workspace.login, path: workspace.root });
			if (!channel) return base.stat(workspaceFileScope, path, signal);
			const stdout = (
				await channel.run(
					`f=${quote(path)}; if [ ! -e "$f" ]; then echo '@@MISSING'; ` +
						`elif [ -L "$f" ] && [ ! -e "$f" ]; then echo '@@BROKEN'; ` +
						`else stat -c '%s|%Y' "$f"; fi`,
					{ timeoutMs: 30_000 },
				)
			).trim();
			if (stdout === "@@MISSING" || stdout === "@@BROKEN" || stdout === "") return undefined;
			const [sizeRaw, mtimeRaw] = stdout.split("|");
			return { absolutePath: String(path), version: `${mtimeRaw}-${sizeRaw}`, bytes: Number(sizeRaw) || 0 };
		},

		async list(workspaceFileScope, path, signal) {
			const workspace = remoteWorkspaceOf(workspaceFileScope?.sessionId);
			if (!workspace) return base.list(workspaceFileScope, path, signal);
			const channel = await remoteChannel({ key: String(workspaceFileScope.sessionId), login: workspace.login, path: workspace.root });
			if (!channel) return base.list(workspaceFileScope, path, signal);
			const base_ = (workspace.root || "/root").replace(/\/+$/, "") || "/";
			const requested = String(path ?? "");
			/*
			 * 前端传的路径以「会话工作区根」为基准（那是宿主本地的 cwd），
			 * 而这里要的是远端 proj 根下的相对路径，所以先把本地前缀剥掉。
			 */
			let relative = requested.replace(/^\/+/, "");
			const localRoot = String(workspaceFileScope?.workspaceRoot ?? "").replace(/^\/+/, "").replace(/\/+$/, "");
			if (localRoot.length > 0) {
				if (relative === localRoot) relative = "";
				else if (relative.startsWith(`${localRoot}/`)) relative = relative.slice(localRoot.length + 1);
			}
			const target = relative.length === 0 ? base_ : `${base_}/${relative}`;
			const stdout = await channel.run(
				`d=${quote(target)}; [ -d "$d" ] || { echo '@@NOTDIR'; exit 0; }; ` +
					`find "$d" -mindepth 1 -maxdepth 1 -printf '%f\\t%Y\\t%s\\n' 2>/dev/null | head -n ${maxEntries + 1}`,
				{ timeoutMs: 60_000 },
			);
			if (stdout.startsWith("@@NOTDIR")) {
				throw new RemoteError("workspace-file/not-directory", `"${target}" 不是目录`, { path: requested, kind: "file" });
			}
			const rows = stdout.split("\n").filter((line) => line.length > 0);
			const truncated = rows.length > maxEntries;
			const entries = rows.slice(0, maxEntries).map((line) => {
				const [name, typeChar, size] = line.split("\t");
				const type = typeChar === "d" ? "directory" : typeChar === "f" ? "file" : "other";
				return type === "file" ? { name, type, size: Number(size) || 0 } : { name, type };
			});
			return { path: relative, entries, truncated };
		},
	};

	/* 只覆盖方法，不动服务实例的其它部分（typert 绑定、lookups、changes 流都保持官方实现） */
	service.read = (...args) => overrides.read(...args);
	service.readBytes = (...args) => overrides.readBytes(...args);
	service.readAll = (...args) => overrides.readAll(...args);
	service.readRelated = (...args) => overrides.readRelated(...args);
	service.stat = (...args) => overrides.stat(...args);
	service.list = (...args) => overrides.list(...args);
}
