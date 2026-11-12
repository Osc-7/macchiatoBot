/**
 * 远程文件访问：**不替换**官方的 workspace-files 服务，只在宿主侧把读文件的方法换成
 * 「会话绑了远程机器就读远端，否则调用原实现」。
 *
 * 为什么不替换服务：`@deepseek-ai/dsh-api-workspace-files` 的**浏览器端**（lib/client.js）
 * 会给 `ctx.resources` 注册 `file` 协议 provider —— 文档预览靠它。把那一行 disable 掉，
 * 预览就会报「文件资源服务不可用」（本仓库踩过这个坑）。所以官方行必须留着。
 *
 * 补丁方式：cordis 的 gateway 是按方法名 `Reflect.get(service, "read")` 取函数的，
 * 所以直接在服务实例上覆盖这些方法即可；函数体全部用闭包，不依赖 `this`
 * （cordis 的服务代理会把 `this` 换成 shadow 对象，`#private` 会直接抛错）。
 */
import { installRemoteWorkspaceFiles } from "./workspace-files.js";

export const name = "lab-workspace-files-override";
export const inject = ["workspaceFiles"];

export function apply(ctx) {
	const service = ctx.workspaceFiles;
	if (!service) return;
	try {
		installRemoteWorkspaceFiles(service);
	} catch (error) {
		ctx.logger?.warn?.(`lab-workspace-files: 覆盖失败，保持官方实现：${String(error?.message || error)}`);
	}
}
