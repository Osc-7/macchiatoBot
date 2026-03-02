"""Automation connectors."""

from .base import BaseConnector, ConnectorFetchItem, ConnectorFetchResult
from .canvas import CanvasConnector
from .course_stub import CourseConnectorStub
from .email_stub import EmailConnectorStub

__all__ = [
    "BaseConnector",
    "ConnectorFetchItem",
    "ConnectorFetchResult",
    "CanvasConnector",
    "CourseConnectorStub",
    "EmailConnectorStub",
]
