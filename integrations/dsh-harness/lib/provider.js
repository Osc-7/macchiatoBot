/**
 * 把 mbot 的模型目录注册成 DSH 的若干 provider。
 *
 * 为什么要这么做：
 *   - DSH 的模型选择器只认 `ctx.llm.listProviders()` 里的 provider，没注册过就
 *     「暂无可用模型」，发送按钮是禁用的；
 *   - 真实推理发生在 mbot 里，所以这里的 adapter **不实现流式调用**：
 *     `stream()` 只在有人绕过本包去调 LLM 时才会被触发（例如会话标题生成），
 *     它直接报错，让上层退回默认行为。
 *
 * provider 按 mbot 每个模型的 `base_url` 归并（DeepSeek 官方 / Codex / Gemini /
 * Kimi / GLM / TAI …），这样前端「服务商 → 模型」两级选择就是 mbot 真实的后端划分。
 * 模型 id 直接沿用 mbot 的 provider 注册名，选中后由 step() 调 `model_switch`，
 * 两边同名同义。
 */
import { appendFileSync } from "node:fs";
import { LlmAdapter, LlmError } from "@deepseek-ai/dsh-llm";
import { labConfig } from "./lab/config.js";
import { MbotLink } from "./lab/wire.js";

/** daemon 侧的档位变体用 `<模型>__<档位>` 命名。 */
const EFFORT_SEP = "__";
const EFFORT_ORDER = ["low", "medium", "high", "xhigh"];
const EFFORT_LABELS = { low: "低", medium: "中", high: "高", xhigh: "极高" };

/** 候选 provider：一次性全部注册，没模型的会在目录里被自动过滤掉。 */
const PROVIDER_NAMES = {
	deepseek: "DeepSeek 官方",
	codex: "OpenAI / Codex",
	gemini: "Google Gemini",
	kimi: "Moonshot Kimi",
	glm: "智谱 GLM",
	anthropic: "Anthropic",
	tai: "TAI",
	mbot: "macchiatoBot · 其他",
};

/** base_url 主机名 → (provider id, 中文名)。顺序即展示顺序。 */
const BACKENDS = [
	{ match: /api\.deepseek\.com/i, id: "deepseek", name: "DeepSeek 官方" },
	{ match: /chatgpt\.com|api\.openai\.com/i, id: "codex", name: "OpenAI / Codex" },
	{ match: /generativelanguage\.googleapis\.com|googleapis/i, id: "gemini", name: "Google Gemini" },
	{ match: /api\.kimi\.com|moonshot/i, id: "kimi", name: "Moonshot Kimi" },
	{ match: /bigmodel\.cn|zhipu/i, id: "glm", name: "智谱 GLM" },
	{ match: /anthropic|claude/i, id: "anthropic", name: "Anthropic" },
	{ match: /tai|triverse/i, id: "tai", name: "TAI" },
];

/** 未知后端统一落在这里，保证模型不会因为认不出 host 就消失。 */
const FALLBACK = { id: "mbot", name: "macchiatoBot · 其他" };

function backendOf(entry) {
	const baseUrl = String(entry?.base_url ?? "");
	const hit = BACKENDS.find((backend) => backend.match.test(baseUrl));
	return hit ?? FALLBACK;
}

/** 一次 `model_list` 的结果缓存（目录很短命，30 秒足够）。 */
class ModelDirectory {
	constructor(config) {
		this.config = config;
		this.cache = null;
		this.loadedAt = 0;
	}

	async models({ maxAgeMs = 30_000 } = {}) {
		if (this.cache !== null && Date.now() - this.loadedAt < maxAgeMs) return this.cache;
		const link = await MbotLink.open(this.config.socket, { timeoutMs: 5_000 });
		try {
			const result = await link.request("model_list", { client_id: "dsh:catalog" }, { timeoutMs: 8_000 });
			const models = Array.isArray(result?.models) ? result.models : [];
			if (models.length > 0) {
				this.cache = models;
				this.loadedAt = Date.now();
			}
			return models;
		} finally {
			link.close();
		}
	}
}

/** 一个 adapter 服务全部 candidate provider：按 base_url 把模型分到对应对。 */
class MbotAdapter extends LlmAdapter {
	constructor(directory) {
		super();
		this.directory = directory;
	}

	providerInfo(provider) {
		return { id: provider, name: PROVIDER_NAMES[provider] ?? provider };
	}

	async listModels(provider) {
		let models;
		try {
			models = await this.directory.models();
		} catch {
			return [];
		}
		return models
			.filter((entry) => backendOf(entry).id === provider)
			/* `<模型>__<档位>` 是 daemon 侧的档位变体，折叠进基模型的 reasoning 里，不单独出现 */
			.filter((entry) => !String(entry.name ?? "").includes(EFFORT_SEP))
			.map((entry) => ({
				provider,
				id: String(entry.name ?? entry.model ?? ""),
				name: String(entry.label || entry.api_model || entry.model || entry.name || ""),
				...(entry.model ? { description: String(entry.model) } : {}),
				...(entry.vision === true ? { inputModalities: ["text", "image"] } : {}),
			}));
	}

	async resolveModel(provider, model) {
		let models = [];
		try {
			models = await this.directory.models();
		} catch {
			models = [];
		}
		const entry = models.find((candidate) => String(candidate.name) === model) ?? null;
		const contextWindow = Number(entry?.context_window);
		/* 同一个基模型在 daemon 里可能被摊成 `<模型>__low` / `__high` … 这些就是可选档位 */
		const efforts = models
			.map((candidate) => String(candidate.name ?? ""))
			.filter((name) => name.startsWith(`${model}${EFFORT_SEP}`))
			.map((name) => name.slice(model.length + EFFORT_SEP.length))
			.filter((effort) => effort.length > 0)
			.sort((left, right) => EFFORT_ORDER.indexOf(left) - EFFORT_ORDER.indexOf(right));
		return {
			provider,
			/* LlmResolvedModelInfo 继承 LlmModelInfo：除了 provider/model 还必须带 id/name */
			id: model,
			name: String(entry?.label || entry?.api_model || model),
			...(Number.isFinite(contextWindow) && contextWindow > 0 ? { context: { contextWindow } } : {}),
			...(efforts.length === 0
				? {}
				: {
						reasoning: {
							efforts: efforts.map((effort) => ({ id: effort, name: EFFORT_LABELS[effort] ?? effort })),
							defaultEffort: efforts.includes("medium") ? "medium" : efforts[0],
						},
					}),
		};
	}

	async *stream(options) {
		throw new LlmError(
			`mbot provider 不直接跑推理（provider=${options?.provider} model=${options?.model}）：` +
				"回合由 @lab/dsh-agent-mbot 驱动 macchiatoBot 完成",
			"MBOT_NOT_A_MODEL_ADAPTER",
		);
	}
}

export const name = "lab-mbot-provider";
export const inject = ["llm"];

/** 排查用：把 provider 的装载过程写到固定文件（logger 会被各个宿主吞掉）。 */
function trace(message) {
	try {
		appendFileSync(process.env.LAB_PROVIDER_TRACE || "/tmp/lab-provider.log", `${new Date().toISOString()} ${message}\n`);
	} catch {
		/* 写不了就算了 */
	}
}

export function apply(ctx) {
	try {
		trace(`apply 进入 · llm=${typeof ctx.llm}`);
		const adapter = new MbotAdapter(new ModelDirectory(labConfig()));
		/* 同步注册：cordis 不等 async apply，注册晚了前端目录就是空的。 */
		ctx.llm.registerAdapter(Object.keys(PROVIDER_NAMES), adapter);
		trace(`已注册 ${Object.keys(PROVIDER_NAMES).join(",")}`);
	} catch (error) {
		trace(`注册失败：${error && error.stack ? error.stack : error}`);
		throw error;
	}
}
