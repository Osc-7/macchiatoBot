/**
 * 工作区命令：机器 → proj → 目录（三层显式模型）。
 *
 *   /machine                     列出机器（标出当前）
 *   /machine sii2                选机器 → 用它 default_proj + default_dir
 *   /machine sii2:/abs/path      兼容旧写法（绝对路径，保持升级前的行为）
 *   /proj                        列出当前机器的 proj
 *   /proj data                   切 proj（用它 path/default_dir）
 *   /proj data/exp/0712          切 proj 并进子目录
 *   /proj add exp=/root/exp      新增/修改 proj（写配置文件）
 *   /workspace                   看当前三层解析结果
 *   /usage                       看 daemon 侧上下文与花费
 *
 * 所有状态都落在 ~/.dsh/lab-mbot-*.json，与 WS 文件通道同源。
 */
import { labConfig, mbotIdentity } from "./lab/config.js";
import { MbotLink } from "./lab/wire.js";
import {
	bindingsPath,
	describeSelection,
	getBinding,
	listMachines,
	resolveSessionWorkspace,
	setBinding,
	upsertProj,
} from "./lab/workspace-model.js";

export const name = "lab-mbot-commands";
export const inject = ["commands"];

/** 当前绑定 → `{machine, proj, dir, legacyPath}`（没有就用默认机器）。 */
function currentBinding(sessionId) {
	const binding = getBinding(sessionId);
	if (binding) return binding;
	const machines = listMachines();
	return { machine: machines.default_machine, proj: "", dir: "", legacyPath: "" };
}

function machineLine(machine, currentMachine, currentProj) {
	const mark = machine.name === currentMachine ? "●" : "○";
	const projs = machine.projs.map((proj) => (machine.name === currentMachine && proj.name === currentProj ? `[${proj.name}]` : proj.name));
	return `${mark} ${machine.name}${machine.login ? "" : "（本机）"} · ${proj_join(projs)}`;
}

function proj_join(items) {
	return items.length === 0 ? "（无 proj）" : items.join(" ");
}

export function apply(ctx) {
	/* ---------------------------------------------------------- /machine */
	ctx.commands.register({
		name: "machine",
		description: "选择工作机器（机器 → proj → 目录三层模型）",
		input: { hint: "机器名，或 机器:/绝对路径（旧写法）；留空列出全部", attachments: false },
		handler: ({ agent, rawInput }) => {
			const sessionId = agent.session.id;
			const binding = currentBinding(sessionId);
			const machines = listMachines();
			const raw = String(rawInput ?? "").trim();

			if (raw.length === 0) {
				const lines = [`默认机器：${machines.default_machine}`, ""];
				for (const machine of machines.machines) {
					lines.push(machineLine(machine, binding.machine, binding.proj));
				}
				lines.push("", "用法：/machine sii2 · /machine sii2:/绝对路径 · /proj <name>");
				return { kind: "success", text: lines.join("\n") };
			}

			/* 旧写法：机器:/绝对路径 */
			const colon = raw.indexOf(":");
			if (colon >= 0) {
				const machineName = raw.slice(0, colon).trim();
				const path = raw.slice(colon + 1).trim();
				if (!path.startsWith("/")) return { kind: "error", text: "旧写法要求绝对路径，例如 /machine sii2:/root" };
				if (!machines.machines.some((machine) => machine.name === machineName)) {
					return { kind: "error", text: `没有机器 ${machineName}（已有：${machines.machines.map((m) => m.name).join(", ")}）` };
				}
				setBinding(sessionId, { machine: machineName, proj: "", dir: "", legacyPath: path });
				const selection = resolveSessionWorkspace(sessionId);
				return { kind: "success", text: `已切到 ${describeSelection(selection)}（绝对路径模式）` };
			}

			const machine = machines.machines.find((entry) => entry.name === raw);
			if (!machine) {
				return { kind: "error", text: `没有机器 ${raw}（已有：${machines.machines.map((m) => m.name).join(", ")}）` };
			}
			setBinding(sessionId, { machine: machine.name, proj: "", dir: "", legacyPath: "" });
			const selection = resolveSessionWorkspace(sessionId);
			return {
				kind: "success",
				text:
					`已切到 ${describeSelection(selection)}\n` +
					`（proj=${machine.default_proj}；要换项目用 /proj，要进子目录用 /proj ${machine.default_proj}/子目录）`,
			};
		},
	});

	/* ------------------------------------------------------------ /proj */
	ctx.commands.register({
		name: "proj",
		description: "在当前机器下选择 proj（项目）与子目录",
		input: { hint: "<proj>[/子目录] · add <名字>=<路径> · 留空列出", attachments: false },
		handler: ({ agent, rawInput }) => {
			const sessionId = agent.session.id;
			const binding = currentBinding(sessionId);
			const machines = listMachines();
			const machine = machines.machines.find((entry) => entry.name === binding.machine);
			if (!machine) return { kind: "error", text: `当前机器 ${binding.machine} 不在配置里` };
			const raw = String(rawInput ?? "").trim();

			if (raw.length === 0) {
				const lines = [`机器 ${machine.name} 的 proj（默认 ${machine.default_proj}）：`];
				for (const proj of machine.projs) {
					const mark = proj.name === (binding.proj || machine.default_proj) ? "●" : "○";
					lines.push(`${mark} ${proj.name} → ${proj.path}${proj.default_dir !== "." ? `（默认进 ${proj.default_dir}）` : ""}`);
				}
				lines.push("", "用法：/proj data · /proj data/exp/0712 · /proj add exp=/root/exp");
				return { kind: "success", text: lines.join("\n") };
			}

			if (raw.startsWith("add ")) {
				const spec = raw.slice(4).trim();
				const eq = spec.indexOf("=");
				if (eq <= 0) return { kind: "error", text: "用法：/proj add <名字>=<绝对路径>" };
				const projName = spec.slice(0, eq).trim();
				const projPath = spec.slice(eq + 1).trim();
				if (!projPath.startsWith("/")) return { kind: "error", text: "proj 路径要绝对路径" };
				const result = upsertProj(machine.name, projName, projPath);
				if (!result.ok) return { kind: "error", text: `写入配置失败：${result.error}` };
				return {
					kind: "success",
					text: `${result.existed ? "已更新" : "已新增"} proj：${machine.name}/${projName} → ${projPath}\n用 /proj ${projName} 切过去`,
				};
			}

			const slash = raw.indexOf("/");
			const projName = slash < 0 ? raw : raw.slice(0, slash);
			const dir = slash < 0 ? "" : raw.slice(slash + 1);
			const proj = machine.projs.find((entry) => entry.name === projName);
			if (!proj) {
				return { kind: "error", text: `机器 ${machine.name} 没有 proj ${projName}（已有：${machine.projs.map((p) => p.name).join(", ")}）` };
			}
			setBinding(sessionId, { machine: machine.name, proj: projName, dir, legacyPath: "" });
			try {
				const selection = resolveSessionWorkspace(sessionId);
				return { kind: "success", text: `已切到 ${describeSelection(selection)}` };
			} catch (error) {
				return { kind: "error", text: String(error?.message || error) };
			}
		},
	});

	/* ------------------------------------------------------- /workspace */
	ctx.commands.register({
		name: "workspace",
		description: "查看当前会话的机器 / proj / 目录与解析出的绝对路径",
		handler: ({ agent }) => {
			const sessionId = agent.session.id;
			const binding = getBinding(sessionId);
			let selection = null;
			let resolutionError = "";
			try {
				selection = resolveSessionWorkspace(sessionId);
			} catch (error) {
				resolutionError = String(error?.message || error);
			}
			const lines = [`会话：${sessionId}`];
			if (!binding) {
				lines.push("绑定：未设置（回落到环境变量 LAB_MBOT_WORKSPACE / daemon 本机）");
			} else {
				lines.push(
					`绑定：机器=${binding.machine || "?"} · proj=${binding.proj || "（默认）"} · 目录=${binding.dir || "（默认）"}` +
						(binding.legacyPath ? ` · 绝对路径=${binding.legacyPath}` : ""),
				);
			}
			if (selection) lines.push(`解析：${describeSelection(selection)}`);
			if (resolutionError) lines.push(`解析失败：${resolutionError}`);
			lines.push(`配置文件：${bindingsPath()}`);
			return { kind: "success", text: lines.join("\n") };
		},
	});

	/* ----------------------------------------------------------- /usage */
	ctx.commands.register({
		name: "usage",
		description: "看 daemon 侧这轮的上下文占用与 token 花费",
		handler: async ({ agent }) => {
			const config = labConfig();
			const identity = mbotIdentity(agent.session.id, config);
			let link;
			try {
				link = await MbotLink.open(config.socket, { timeoutMs: 5_000 });
				const result = await link.request("get_token_usage", { session_id: identity.sessionId }, { timeoutMs: 10_000 });
				const usage = result?.usage ?? {};
				/* 会话从 daemon 内存里淘汰后，get_token_usage 会回到全 0（历史仍在 DSH 的日志里） */
				if (Number(usage.call_count) === 0 && Number(usage.total_tokens) === 0) {
					return {
						kind: "success",
						text:
							`daemon 里这个会话当前未加载（空闲淘汰），拿不到上下文占用。\n` +
							`上一轮的 token/缓存数字看消息下方的胶囊；会话未过期时这里会显示 ` +
							`上下文 current/max、累计用量和花费。\nmbot 会话：${identity.sessionId}`,
					};
				}
				const current = Number(usage.context_window_current_tokens) || 0;
				const max = Number(usage.context_window_max_tokens) || 0;
				const percent = max > 0 ? ((current / max) * 100).toFixed(1) : "?";
				const thousand = (value) => Number(value || 0).toLocaleString("en-US");
				const lines = [
					`上下文：${thousand(current)} / ${thousand(max)} tok（${percent}%）`,
					`累计：⬆︎ ${thousand(usage.prompt_tokens)} · ⬇︎ ${thousand(usage.completion_tokens)} · Σ ${thousand(usage.total_tokens)}`,
					`缓存命中：${thousand(usage.prompt_cache_hit_tokens)} tok · 调用 ${usage.call_count ?? 0} 次`,
				];
				if (usage.cost_yuan !== undefined) lines.push(`花费：¥${Number(usage.cost_yuan).toFixed(4)}`);
				lines.push(`mbot 会话：${identity.sessionId}`);
				return { kind: "success", text: lines.join("\n") };
			} catch (error) {
				return { kind: "error", text: `读 daemon 用量失败：${String(error?.message || error)}` };
			} finally {
				link?.close();
			}
		},
	});
}
