"""
飞书前端接入模块。

本模块负责：
- 从全局 Config 读取飞书相关配置；
- 封装飞书开放平台 HTTP 调用客户端；
- 定义事件回调数据模型；
- 将飞书会话映射到 Schedule Agent 会话；
- 通过 AutomationIPCClient 将消息转发给 automation daemon。
"""

