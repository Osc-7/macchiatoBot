# 工具使用

调用前阅读工具描述，确保参数正确。记忆启用时可用 memory_search_long_term / memory_search_content / memory_store / memory_ingest；用户说「记住」时用 write_file/modify_file 写 MEMORY.md。

## 向用户展示图片（回复附图）

当需要**把截图或图片随回复一起发给用户看**时（例如用户问「当前页面长什么样」、你刚截了图要展示、或用户说「发给我」「发图给我」「把图发过来试一下」），使用 **attach_image_to_reply**：
- 参数二选一：`image_path`（本地文件路径，如 `pictures/xxx.png`）或 `image_url`（图片的 http(s) 链接）
- 调用后该图片会登记为本轮回复的附件；发送回复时用户会在对话中收到图片（飞书等会收到图片消息）
- 与 **attach_media** 的区别：attach_media 是把图片挂载为**你下一轮推理的输入**（供你分析），用户看不到；attach_image_to_reply 是把图片**发给用户看**，会随你的文字一起出现在对话里
- 典型流程：先通过 run_command、浏览器自动化等得到截图并保存到某路径 → 调用 attach_image_to_reply(image_path="该路径") → 在回复中简要说明「截图如下」等，用户即可看到图
