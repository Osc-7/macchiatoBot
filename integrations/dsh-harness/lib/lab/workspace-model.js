/**
 * 工作区模型：机器 → proj → 目录，三层显式化。
 *
 * 两份 JSON，都是「单一事实来源」，宿主插件、命令、WS 桥三方共用：
 *   ~/.dsh/lab-mbot-machines.json     机器与 proj 目录（人的配置）
 *   ~/.dsh/lab-mbot-workspaces.json   会话绑定（session → {machine, proj, dir}）
 *
 * 只依赖 node 内置模块，所以 WS 桥（独立进程）也能直接 import。
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, posix } from "node:path";
import { homedir } from "node:os";

const HOME = process.env.DSH_HOME || join(homedir(), ".dsh");
const MACHINES_PATH = join(HOME, "lab-mbot-machines.json");
const BINDINGS_PATH = join(HOME, "lab-mbot-workspaces.json");

/** 首次运行写进去的模板。 */
const DEFAULT_MACHINES = {
	default_machine: "sii2",
	machines: {
		sii2: {
			label: "SII 二号机",
			login: "sii2",
			default_proj: "root",
			projs: {
				root: { path: "/root", default_dir: "." },
				tmp: { path: "/tmp" },
			},
		},
		g1: {
			label: "g1",
			login: "g1",
			default_proj: "home",
			projs: {
				home: { path: "/home/jyj", default_dir: "." },
			},
		},
		a100: {
			label: "a100",
			login: "a100",
			default_proj: "root",
			projs: {
				root: { path: "/root", default_dir: "." },
			},
		},
		local: {
			label: "daemon 本机",
			login: "",
			default_proj: "home",
			projs: {
				home: { path: homedir(), default_dir: "." },
			},
		},
	},
};

function readJson(path, fallback) {
	try {
		const parsed = JSON.parse(readFileSync(path, "utf8"));
		return parsed && typeof parsed === "object" ? parsed : fallback;
	} catch {
		return fallback;
	}
}

function writeJson(path, value) {
	try {
		mkdirSync(dirname(path), { recursive: true });
		writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`);
		return true;
	} catch {
		return false;
	}
}

/** 读机器配置；文件不存在时写一份模板并返回。 */
export function loadMachines() {
	const current = readJson(MACHINES_PATH, null);
	if (current && current.machines && typeof current.machines === "object") return current;
	writeJson(MACHINES_PATH, DEFAULT_MACHINES);
	return structuredClone(DEFAULT_MACHINES);
}

export function machinesPath() {
	return MACHINES_PATH;
}

export function bindingsPath() {
	return BINDINGS_PATH;
}

/** 机器列表（给命令和 UI 用）。 */
export function listMachines() {
	const config = loadMachines();
	return {
		default_machine: config.default_machine ?? Object.keys(config.machines)[0] ?? "",
		machines: Object.entries(config.machines).map(([name, machine]) => ({
			name,
			label: machine.label ?? name,
			login: typeof machine.login === "string" ? machine.login : name,
			default_proj: machine.default_proj ?? Object.keys(machine.projs ?? {})[0] ?? "",
			projs: Object.entries(machine.projs ?? {}).map(([proj, value]) => ({
				name: proj,
				path: String(value?.path ?? ""),
				default_dir: String(value?.default_dir ?? "."),
			})),
		})),
	};
}

/** 取一台机器（原始结构）。 */
export function getMachine(name) {
	const config = loadMachines();
	return config.machines?.[name] ?? null;
}

/** 取一个 proj。 */
export function getProj(machineName, projName) {
	const machine = getMachine(machineName);
	if (!machine) return null;
	return machine.projs?.[projName] ?? null;
}

/** 往某台机器加/改一个 proj（写回配置）。 */
export function upsertProj(machineName, projName, path) {
	const config = loadMachines();
	const machine = config.machines?.[machineName];
	if (!machine) return { ok: false, error: `没有机器 ${machineName}` };
	machine.projs ??= {};
	const existed = Boolean(machine.projs[projName]);
	machine.projs[projName] = { ...(machine.projs[projName] ?? {}), path };
	if (!machine.default_proj) machine.default_proj = projName;
	const saved = writeJson(MACHINES_PATH, config);
	return { ok: saved, existed, error: saved ? undefined : "配置写入失败" };
}

/** 规范化目录片段：禁止绝对路径与 `..` 逃逸。 */
export function normalizeDir(input) {
	const raw = String(input ?? "").trim().replace(/\\/g, "/");
	if (raw.length === 0) return ".";
	if (raw.split("/").includes("..")) throw new Error(`目录不能含 '..'：${input}`);
	const normalized = posix.normalize(`/${raw}`).replace(/^\/+/, "");
	if (normalized.startsWith("..")) throw new Error(`目录不能越出 proj 根：${input}`);
	return normalized.length === 0 ? "." : normalized;
}

/** 把 proj 路径与 dir 拼成绝对根。 */
export function joinRoot(basePath, dir) {
	const base = posix.normalize(String(basePath || "/"));
	const extra = normalizeDir(dir);
	if (extra === ".") return base === "." ? "/" : base;
	return posix.join(base, extra);
}

/** 判断 child 是否在 root 内（含相等）。 */
export function isInsideRoot(root, child) {
	const r = posix.normalize(root);
	const c = posix.normalize(child);
	return c === r || c.startsWith(`${r.replace(/\/+$/, "")}/`);
}

// ---------------------------------------------------------------- 会话绑定

/** 读全部绑定（自动迁移旧格式）。 */
export function readBindings() {
	const raw = readJson(BINDINGS_PATH, {});
	const out = {};
	for (const [sessionId, value] of Object.entries(raw)) {
		const binding = migrateBinding(value);
		if (binding) out[sessionId] = binding;
	}
	return out;
}

/**
 * 旧格式 `"sii2:/tmp"` / `"/tmp"` → 新格式。
 * 迁移后保留 legacyPath，语义与今天完全一致。
 */
export function migrateBinding(value) {
	if (value === undefined || value === null) return null;
	if (typeof value === "object") {
		const machine = typeof value.machine === "string" ? value.machine : "";
		const proj = typeof value.proj === "string" ? value.proj : "";
		const dir = typeof value.dir === "string" ? value.dir : "";
		/* 兼容两种字段名：新格式写 legacyPath，早期手改的文件可能写 path */
		const rawPath = typeof value.legacyPath === "string" ? value.legacyPath : typeof value.path === "string" ? value.path : "";
		if (!machine && !rawPath) return null;
		return { machine, proj, dir, legacyPath: rawPath };
	}
	const text = String(value).trim();
	if (text.length === 0) return null;
	const cut = text.indexOf(":");
	if (cut < 0) return { machine: "local", proj: "", dir: "", legacyPath: text };
	return { machine: text.slice(0, cut).trim() || "local", proj: "", dir: "", legacyPath: text.slice(cut + 1).trim() };
}

/** 写一个会话的绑定。 */
export function setBinding(sessionId, binding) {
	const all = readBindings();
	all[String(sessionId)] = binding;
	const saved = writeJson(BINDINGS_PATH, all);
	return saved;
}

export function getBinding(sessionId) {
	return readBindings()[String(sessionId)] ?? null;
}

// ---------------------------------------------------------------- 解析

/**
 * 会话 → 实际工作区。
 * @returns {{machine, proj, dir, root, login, label, legacyPath?, source}} | null
 */
export function resolveSessionWorkspace(sessionId) {
	const binding = getBinding(sessionId);
	if (!binding) return null;
	const machines = loadMachines();
	const machineName = binding.machine || machines.default_machine || "local";
	const machine = machines.machines?.[machineName] ?? null;
	if (!machine) {
		/* 机器不在配置里但带了绝对路径：按 legacy 走 */
		if (binding.legacyPath) {
			return {
				machine: machineName,
				proj: "",
				dir: "",
				root: posix.normalize(binding.legacyPath),
				login: machineName === "local" ? "" : machineName,
				label: machineName,
				legacyPath: binding.legacyPath,
				source: "legacy",
			};
		}
		return null;
	}
	const login = typeof machine.login === "string" ? machine.login : machineName;
	/* 旧写法 `/machine sii2:/abs/path` 没选 proj：绝对路径优先，保持今天的行为 */
	if (binding.legacyPath && !binding.proj) {
		return {
			machine: machineName,
			proj: "",
			dir: "",
			root: posix.normalize(binding.legacyPath),
			login,
			label: machine.label ?? machineName,
			legacyPath: binding.legacyPath,
			source: "legacy",
		};
	}
	const projName = binding.proj || machine.default_proj || Object.keys(machine.projs ?? {})[0] || "";
	const proj = machine.projs?.[projName] ?? null;
	if (binding.legacyPath && !proj) {
		return {
			machine: machineName,
			proj: "",
			dir: "",
			root: posix.normalize(binding.legacyPath),
			login,
			label: machine.label ?? machineName,
			legacyPath: binding.legacyPath,
			source: "legacy",
		};
	}
	if (!proj) return null;
	const dir = binding.dir || proj.default_dir || ".";
	return {
		machine: machineName,
		proj: projName,
		dir,
		root: joinRoot(String(proj.path ?? "/"), dir),
		login,
		label: machine.label ?? machineName,
		source: "model",
	};
}

/** 人类可读的一行描述。 */
export function describeSelection(selection) {
	if (!selection) return "未绑定（工具跑在 daemon 本机）";
	const who = selection.login ? selection.login : "本机";
	const where = [selection.proj, selection.dir && selection.dir !== "." ? selection.dir : ""].filter(Boolean).join("/");
	return `${who}${where ? ` · ${where}` : ""} → ${selection.root}`;
}
