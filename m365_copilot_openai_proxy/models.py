"""Request/response shapes, as plain dataclasses with hand-written parsing.

Replaces pydantic. Each ``from_dict`` mirrors pydantic's ``extra="ignore"``
behaviour (unknown keys are dropped) and raises ``ValueError`` on invalid
input, which the app layer turns into an HTTP 400.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

_OPENAI_ROLES = {"system", "developer", "user", "assistant", "tool"}
_ANTHROPIC_ROLES = {"user", "assistant"}

Content = Union[str, list["ContentPart"]]


@dataclass
class ContentPart:
    type: str
    text: str | None = None

    @classmethod
    def from_obj(cls, obj: Any) -> "ContentPart":
        if isinstance(obj, dict):
            return cls(type=str(obj.get("type", "")), text=obj.get("text"))
        if isinstance(obj, str):
            return cls(type="text", text=obj)
        return cls(type="", text=None)


def _parse_content(value: Any) -> Content:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [ContentPart.from_obj(part) for part in value]
    return str(value)


@dataclass
class OpenAIMessage:
    role: str
    content: Content

    @classmethod
    def from_dict(cls, data: Any) -> "OpenAIMessage":
        if not isinstance(data, dict):
            raise ValueError("each message must be an object")
        role = data.get("role")
        if role not in _OPENAI_ROLES:
            raise ValueError(f"invalid message role: {role!r}")
        return cls(role=role, content=_parse_content(data.get("content")))


@dataclass
class OpenAIChatRequest:
    model: str
    messages: list[OpenAIMessage]
    stream: bool = False
    temperature: float | None = None
    user: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "OpenAIChatRequest":
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        model = data.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("'model' is required")
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ValueError("'messages' must be a non-empty list")
        return cls(
            model=model,
            messages=[OpenAIMessage.from_dict(m) for m in raw_messages],
            stream=bool(data.get("stream", False)),
            temperature=data.get("temperature"),
            user=data.get("user"),
        )


@dataclass
class AnthropicMessage:
    role: str
    content: Content

    @classmethod
    def from_dict(cls, data: Any) -> "AnthropicMessage":
        if not isinstance(data, dict):
            raise ValueError("each message must be an object")
        role = data.get("role")
        if role not in _ANTHROPIC_ROLES:
            raise ValueError(f"invalid message role: {role!r}")
        return cls(role=role, content=_parse_content(data.get("content")))


@dataclass
class AnthropicMessagesRequest:
    model: str
    messages: list[AnthropicMessage]
    system: Content | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "AnthropicMessagesRequest":
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        model = data.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("'model' is required")
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ValueError("'messages' must be a non-empty list")
        system = data.get("system")
        return cls(
            model=model,
            messages=[AnthropicMessage.from_dict(m) for m in raw_messages],
            system=_parse_content(system) if system is not None else None,
            stream=bool(data.get("stream", False)),
            max_tokens=data.get("max_tokens"),
            temperature=data.get("temperature"),
        )


@dataclass
class OpenAIResponsesRequest:
    model: str
    input: Any
    instructions: str | None = None
    stream: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> "OpenAIResponsesRequest":
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        model = data.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("'model' is required")
        if "input" not in data:
            raise ValueError("'input' is required")
        return cls(
            model=model,
            input=data.get("input"),
            instructions=data.get("instructions"),
            stream=bool(data.get("stream", False)),
        )


@dataclass
class TranslatedRequest:
    prompt: str
    additional_context: list[str] = field(default_factory=list)
