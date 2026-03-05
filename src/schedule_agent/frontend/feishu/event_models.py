from __future__ import annotations

"""
飞书事件回调数据模型。

仅建模当前需要的字段，其他字段通过 extra="allow" 忽略。
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class FeishuChallengeRequest(BaseModel):
    """URL 验证请求体"""

    model_config = ConfigDict(extra="allow")

    type: str
    token: Optional[str] = None
    challenge: str


class FeishuSenderId(BaseModel):
    model_config = ConfigDict(extra="allow")

    open_id: Optional[str] = None
    user_id: Optional[str] = None
    union_id: Optional[str] = None


class FeishuSender(BaseModel):
    model_config = ConfigDict(extra="allow")

    sender_id: FeishuSenderId
    sender_type: str = Field(default="user")
    tenant_key: Optional[str] = None


class FeishuMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    message_id: str
    chat_id: str
    chat_type: str  # p2p / group
    message_type: str
    content: str

    @property
    def text(self) -> str:
        """
        解析文本消息内容。

        飞书文本消息 content 形如：'{"text":"明天早上8点开会"}'
        """
        import json

        try:
            data = json.loads(self.content)
            value = data.get("text")
            return str(value) if value is not None else ""
        except Exception:
            return self.content


class FeishuMessageEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    sender: FeishuSender
    message: FeishuMessage


class FeishuEventHeader(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str
    event_type: str
    token: Optional[str] = None


class FeishuEventEnvelope(BaseModel):
    """飞书事件总包裹"""

    model_config = ConfigDict(extra="allow")

    schema: str
    header: FeishuEventHeader
    event: FeishuMessageEvent

