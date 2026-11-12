/**
 * DSH 消息 → 纯文本。
 *
 * DSH 的 message 是结构化 content 块（text / reasoning / tool-call），
 * 而 mbot 的回合入口只吃一段文本，所以这里只取 text 块。
 */

/** 一条消息的可见文本。 */
export function messageText(message) {
	const content = message?.content;
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.filter((block) => block && block.type === "text" && typeof block.text === "string")
		.map((block) => block.text)
		.join("");
}

/**
 * 是否是用户真正输入的消息。
 *
 * DSH 会把「运行时上下文」「技能目录」这类东西也作为 user 消息插进决策队列，
 * 它们的 source.kind 不是 "user"（实测真实用户消息是 `{"kind":"user"}`）。
 */
export function isUserInput(message) {
	if (!message || message.role !== "user") return false;
	const source = message.source;
	if (source === undefined || source === null) return true;
	if (typeof source !== "object") return true;
	return source.kind === "user";
}

/**
 * 去掉 DSH 注入给模型的包装块（技能目录等）。这些内容属于 DSH 的 prompt 组装，
 * 不该当成用户的话转给 mbot。
 */
export function stripInjectedBlocks(text) {
	return String(text ?? "")
		.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, "")
		.replace(/<available_skills>[\s\S]*?<\/available_skills>/g, "")
		.replace(/<system-reminder>[\s\S]*$/g, "")
		.trim();
}

/** 从一批决策消息里拼出这一回合该发给 mbot 的文本。 */
export function turnText(messages) {
	const inputs = messages.filter(isUserInput).map((message) => stripInjectedBlocks(messageText(message)));
	const clean = inputs.filter((text) => text.length > 0);
	if (clean.length > 0) return clean.join("\n\n");
	const fallback = messages.map((message) => stripInjectedBlocks(messageText(message))).filter((text) => text.length > 0);
	return fallback.join("\n\n");
}
