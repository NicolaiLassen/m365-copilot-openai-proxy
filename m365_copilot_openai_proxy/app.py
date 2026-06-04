"""HTTP application: routing and request handling without FastAPI.

``create_app`` returns an ``App`` whose ``handle(method, path, headers, body)``
returns a ``Response``. The standard-library server in ``server.py`` adapts
that to sockets; tests can call ``handle`` directly without any network.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator

from .config import Settings
from .models import (
    AnthropicMessagesRequest,
    OpenAIChatRequest,
    OpenAIResponsesRequest,
)
from .session_store import PersistentSession, PersistentSessionStore
from .substrate_client import SubstrateCopilotClient, SubstrateCopilotError
from .token_store import AccessTokenStore
from .translator import (
    translate_anthropic_request,
    translate_openai_request,
    translate_responses_request,
)

_PERSIST_MODEL_SUFFIX = ":persist"
_SESSION_ID_HEADER = "x-m365-session-id"


class HTTPError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Response:
    """Either a buffered JSON response or a streaming (SSE) response."""

    def __init__(
        self,
        status: int,
        *,
        json_body: object | None = None,
        stream: Iterator[str] | None = None,
        content_type: str | None = None,
    ):
        self.status = status
        self.stream = stream
        if stream is not None:
            self.content_type = content_type or "text/event-stream"
            self.body = b""
        else:
            self.content_type = content_type or "application/json"
            self.body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")


class App:
    def __init__(
        self,
        settings: Settings,
        copilot_client_factory: Callable[[], SubstrateCopilotClient] | None = None,
    ):
        self.settings = settings
        self.token_store = AccessTokenStore(settings.access_token)
        self.session_store = PersistentSessionStore()
        self.copilot_client_factory = copilot_client_factory or (
            lambda: SubstrateCopilotClient(self.token_store.get(), self.settings.time_zone)
        )

    # -- dispatch -----------------------------------------------------------

    def handle(self, method: str, path: str, headers: dict, body: bytes) -> Response:
        path = path.split("?", 1)[0].rstrip("/") or "/"
        try:
            return self._route(method, path, headers, body)
        except HTTPError as exc:
            return Response(exc.status, json_body={"detail": exc.detail})

    def _route(self, method: str, path: str, headers: dict, body: bytes) -> Response:
        routes = {
            ("GET", "/healthz"): self._healthz,
            ("GET", "/v1/token/status"): self._token_status,
            ("GET", "/v1/models"): self._models,
            ("POST", "/v1/chat/completions"): self._chat_completions,
            ("POST", "/v1/responses"): self._responses,
            ("POST", "/v1/messages"): self._anthropic_messages,
        }
        handler = routes.get((method, path))
        if handler is None:
            raise HTTPError(404, f"not found: {method} {path}")
        if method == "POST":
            return handler(headers, _parse_json(body))
        return handler()

    # -- GET endpoints ------------------------------------------------------

    def _healthz(self) -> Response:
        return Response(200, json_body={"status": "ok", "token": self.token_store.status()})

    def _token_status(self) -> Response:
        return Response(200, json_body=self.token_store.status())

    def _models(self) -> Response:
        return Response(200, json_body={
            "object": "list",
            "data": [{
                "id": self.settings.model_alias,
                "object": "model",
                "owned_by": "microsoft-365-copilot",
            }],
        })

    # -- POST endpoints -----------------------------------------------------

    def _chat_completions(self, headers: dict, data: dict) -> Response:
        try:
            request = OpenAIChatRequest.from_dict(data)
            translated = translate_openai_request(request)
            session = self._persistent_session(headers, request.model, request.user)
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from exc

        client = self._client()
        if request.stream:
            return Response(200, stream=_openai_stream(
                self.settings.model_alias, client,
                translated.prompt, translated.additional_context, session,
            ))
        text = self._chat(client, translated.prompt, translated.additional_context, session)
        return Response(200, json_body={
            "id": f"chatcmpl_{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.settings.model_alias,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
        })

    def _responses(self, headers: dict, data: dict) -> Response:
        try:
            request = OpenAIResponsesRequest.from_dict(data)
            translated = translate_responses_request(request)
            session = self._persistent_session(headers, request.model)
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from exc

        client = self._client()
        if request.stream:
            return Response(200, stream=_responses_stream(
                self.settings.model_alias, client,
                translated.prompt, translated.additional_context, session,
            ))
        text = self._chat(client, translated.prompt, translated.additional_context, session)
        return Response(200, json_body={
            "id": f"resp_{uuid.uuid4().hex}",
            "object": "response",
            "created_at": int(time.time()),
            "model": self.settings.model_alias,
            "output": [{
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        })

    def _anthropic_messages(self, headers: dict, data: dict) -> Response:
        try:
            request = AnthropicMessagesRequest.from_dict(data)
            translated = translate_anthropic_request(request)
            session = self._persistent_session(headers, request.model)
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from exc

        client = self._client()
        if request.stream:
            return Response(200, stream=_anthropic_stream(
                self.settings.model_alias, client,
                translated.prompt, translated.additional_context, session,
            ))
        text = self._chat(client, translated.prompt, translated.additional_context, session)
        return Response(200, json_body={
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": self.settings.model_alias,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })

    # -- helpers ------------------------------------------------------------

    def _client(self) -> SubstrateCopilotClient:
        try:
            return self.copilot_client_factory()
        except SubstrateCopilotError as exc:
            raise HTTPError(502, str(exc)) from exc

    @staticmethod
    def _chat(client, prompt, additional_context, session) -> str:
        try:
            return client.chat(prompt, additional_context, session)
        except SubstrateCopilotError as exc:
            raise HTTPError(502, str(exc)) from exc

    def _persistent_session(
        self,
        headers: dict,
        model: str,
        fallback_key: str | None = None,
    ) -> PersistentSession | None:
        header_key = (headers.get(_SESSION_ID_HEADER) or "").strip()
        if header_key:
            return self.session_store.get(f"header:{header_key}")
        if model.endswith(_PERSIST_MODEL_SUFFIX):
            return self.session_store.get(f"model:{fallback_key or 'default'}")
        return None


def create_app(
    settings: Settings | None = None,
    copilot_client_factory: Callable[[], SubstrateCopilotClient] | None = None,
) -> App:
    return App(settings or Settings(), copilot_client_factory)


def _parse_json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPError(400, f"invalid JSON body: {exc}") from exc


# --- streaming generators (Server-Sent Events) -----------------------------

def _openai_stream(model_alias, client, prompt, additional_context, session=None) -> Iterator[str]:
    completion_id = f"chatcmpl_{uuid.uuid4().hex}"
    created = int(time.time())

    def chunk(delta: dict, finish_reason):
        return {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_alias,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    yield f"data: {json.dumps(chunk({'role': 'assistant'}, None))}\n\n"
    try:
        for delta in client.chat_stream(prompt, additional_context, session):
            yield f"data: {json.dumps(chunk({'content': delta}, None))}\n\n"
    except SubstrateCopilotError as exc:
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        yield "data: [DONE]\n\n"
        return
    yield f"data: {json.dumps(chunk({}, 'stop'))}\n\n"
    yield "data: [DONE]\n\n"


def _responses_stream(model_alias, client, prompt, additional_context, session=None) -> Iterator[str]:
    resp_id = f"resp_{uuid.uuid4().hex}"
    item_id = f"msg_{uuid.uuid4().hex}"
    created = int(time.time())

    yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model_alias, 'status': 'in_progress', 'output': []}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': item_id, 'type': 'message', 'role': 'assistant', 'content': []}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.content_part.added', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n"

    full_text = ""
    try:
        for delta in client.chat_stream(prompt, additional_context, session):
            full_text += delta
            yield f"data: {json.dumps({'type': 'response.output_text.delta', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'delta': delta})}\n\n"
    except SubstrateCopilotError as exc:
        yield f"data: {json.dumps({'type': 'error', 'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        return

    yield f"data: {json.dumps({'type': 'response.output_text.done', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'text': full_text})}\n\n"
    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model_alias, 'status': 'completed', 'output': [{'id': item_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': full_text}]}], 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}})}\n\n"


def _anthropic_stream(model_alias, client, prompt, additional_context, session=None) -> Iterator[str]:
    msg_id = f"msg_{uuid.uuid4().hex}"

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield sse("message_start", {"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [], "model": model_alias, "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
    yield sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    yield sse("ping", {"type": "ping"})

    try:
        for delta in client.chat_stream(prompt, additional_context, session):
            yield sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta}})
    except SubstrateCopilotError as exc:
        yield sse("error", {"type": "error", "error": {"type": "upstream_error", "message": str(exc)}})
        return

    yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}})
    yield sse("message_stop", {"type": "message_stop"})
