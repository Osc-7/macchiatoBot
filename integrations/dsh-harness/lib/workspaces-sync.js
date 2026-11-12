/**
 * 把「机器 → proj」同步成 DSH 的工作区条目，让左侧栏直接体现工作区拆分。
 *
 * 远端机器的 proj 走 FUSE 挂载点 `~/mnt/<机器>/<proj>`（lab-mount@ 单元），
 * 本机（login 为空）直接用 proj 的真实路径。
 *
 * 同步是幂等的：`workspaceRegistry.create()` 对已存在的目录只返回已有条目，不改标题。
 * 定时跑（60s）是因为：
 *   - 挂载由 systemd 在机器上线后拉起，可能晚于 dsh 启动；
 *   - `/proj add` 新增的 proj 不用重启就能出现在侧栏。
 */
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { listMachines } from "./lab/workspace-model.js";

export const name = "lab-workspaces-sync";
export const inject = ["workspaceRegistry"];

const MNT = join(process.env.HOME || homedir(), "mnt");

/**
 * /proc/self/mountinfo 里的挂载点集合。
 * 远端目录必须是**真正的挂载点**才算数：unit 起不来时 mkdir 会留下空目录，
 * 只看 existsSync 会把「机器离线」误判成「工作区可用」。
 */
function mountPoints() {
	try {
		const points = new Set();
		for (const line of readFileSync("/proc/self/mountinfo", "utf8").split("\n")) {
			const fields = line.split(" ");
			if (fields.length > 4 && fields[4]) points.add(fields[4]);
		}
		return points;
	} catch {
		return new Set();
	}
}

/** 这条机器/proj 现在是否真的可用。 */
export function isReady(machine, proj, mounts = mountPoints()) {
	const path = workspacePathOf(machine, proj);
	if (!path) return false;
	return machine.login ? mounts.has(path) : existsSync(path);
}

/** 一条机器/proj 对应的本地路径（远端 → FUSE 挂载点）。 */
export function workspacePathOf(machine, proj) {
	return machine.login ? join(MNT, machine.name, proj.name) : String(proj.path ?? "");
}

export function apply(ctx) {
	let running = false;

	const sync = async () => {
		if (running) return;
		running = true;
		try {
			const { machines } = listMachines();
			const mounts = mountPoints();
			for (const machine of machines) {
				for (const proj of machine.projs) {
					if (!isReady(machine, proj, mounts)) continue;
					const path = workspacePathOf(machine, proj);
					try {
						await ctx.workspaceRegistry.create(path, `${machine.name} · ${proj.name}`);
					} catch (error) {
						/* 单个失败不影响其它：路径没就绪、挂载还没起来都算正常 */
						ctx.logger?.debug?.(`lab-workspaces-sync: 跳过 ${path}：${String(error?.message || error)}`);
					}
				}
			}
		} finally {
			running = false;
		}
	};

	ctx.effect(() => {
		void sync();
		const timer = setInterval(() => {
			void sync();
		}, 60_000);
		return () => clearInterval(timer);
	}, "lab-workspaces-sync: 定时同步机器/proj 工作区");
}
