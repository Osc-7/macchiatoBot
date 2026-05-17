"""Shared summary prompt fragments for kernel summarization paths."""

SUMMARY_USER_APPEND = """Please summarize everything above. / 请总结上文内容。

请直接输出可用于后续对话延续的摘要正文：
- 保留用户目标、偏好、关键事实、明确决定、当前进展、工具结果中的重要数据、待办和未决问题。
- 压缩细枝末节，保留会影响后续判断的信息。
- 使用与对话一致的语言；不要寒暄，不要解释你在总结。"""
