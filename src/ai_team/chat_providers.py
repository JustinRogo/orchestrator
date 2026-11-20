"""Text-only API runners. No provider response bodies or headers are logged."""
from __future__ import annotations

import http.client
import io
import json
import os
import socket
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Callable, Iterator, Literal, Protocol
from urllib.parse import quote

from .live import Cancellation

PROVIDERS = {
    "openai": ("OpenAI", "OPENAI_API_KEY"),
    "anthropic": ("Anthropic", "ANTHROPIC_API_KEY"),
    "google": ("Google", "GEMINI_API_KEY"),
}
MAX_RESPONSE_BYTES = 1_048_576
MAX_TEXT_BYTES = 24_000
MAX_STREAM_BYTES = 4 * MAX_RESPONSE_BYTES


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: list[ChatMessage]
    system_prompt: str
    max_output_tokens: int
    timeout_seconds: int


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    text: str
    usage: TokenUsage = TokenUsage()
    model: str | None = None
    output_limited: bool = False


class ModelRunner(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse: ...


class ProviderError(Exception):
    """A fixed, user-facing error that excludes upstream diagnostics."""


def credential(provider: str) -> str:
    return os.environ.get(PROVIDERS[provider][1], "").strip()


def _keys() -> list[str]:
    return sorted({key for provider in PROVIDERS if (key := credential(provider))}, key=len, reverse=True)


def redact_credentials(text: str) -> str:
    for key in _keys():
        text = text.replace(key, "[API key redacted]")
    return text


def safe_preview(text: str) -> str:
    text = text.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
    keys = _keys()
    # Retain any suffix that might become a complete key in the next event.
    suffix = max((size for key in keys for size in range(min(len(text), len(key) - 1), 0, -1)
                  if text.endswith(key[:size])), default=0)
    if suffix:
        text = text[:-suffix]
    for key in keys:
        text = text.replace(key, "[API key redacted]")
    return text.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")


class _Preview:
    def __init__(self, publish: Callable[[str], None]):
        self.publish = publish
        self.text = ""
        self.size = 0

    def add(self, delta: str) -> None:
        if not delta or self.size >= MAX_TEXT_BYTES:
            return
        data = delta.encode("utf-8")
        self.text += data[:MAX_TEXT_BYTES - self.size].decode("utf-8", errors="ignore")
        self.size += len(data)
        self.publish(self.text)


def _check_status(status: int) -> None:
    if status in {401, 403}:
        raise ProviderError("API access denied. Check the server's API key and model permissions.")
    if status in {402, 429}:
        raise ProviderError("The provider's rate limit or quota was reached. Check your API plan or try again later.")
    if status in {400, 404, 422}:
        raise ProviderError("The provider rejected this request. Check the configured model and session limits.")
    if status != 200:
        raise ProviderError("The provider is unavailable. Try again later.")


def _post(host: str, path: str, headers: dict, payload: dict, timeout: int) -> dict:
    connection = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        connection.request("POST", path, json.dumps(payload).encode("utf-8"),
                           {"Content-Type": "application/json", **headers})
        reply = connection.getresponse()
        try:
            _check_status(reply.status)
            data = reply.read(MAX_RESPONSE_BYTES + 1)
        finally:
            reply.close()
        if len(data) > MAX_RESPONSE_BYTES:
            raise ProviderError("The provider response exceeded the size limit.")
        return json.loads(data)
    except TimeoutError:
        raise ProviderError("The provider request timed out.") from None
    except (OSError, http.client.HTTPException):
        raise ProviderError("Could not connect to the provider. Check your network and API settings.") from None
    finally:
        connection.close()


def _events(reply: http.client.HTTPResponse, check: Callable[[], None]) -> Iterator[dict]:
    frames = []
    size = 0
    while True:
        check()
        line = reply.readline(MAX_RESPONSE_BYTES + 1)
        if not line:
            return
        size += len(line)
        if size > MAX_STREAM_BYTES or len(line) > MAX_RESPONSE_BYTES:
            raise ProviderError("The provider response exceeded the size limit.")
        line = line.rstrip(b"\r\n")
        if line.startswith(b"data:"):
            frames.append(line[5:].removeprefix(b" "))
        elif not line and frames:
            data = b"\n".join(frames)
            frames.clear()
            if data == b"[DONE]":
                return
            event = json.loads(data)
            if not isinstance(event, dict):
                raise ValueError("Expected a JSON event")
            yield event


class _ResponseReader(io.RawIOBase):
    """Keep HTTP buffering intact while checking for cancellation on idle sockets."""

    def __init__(self, connection: socket.socket, signal: Cancellation, deadline: float):
        super().__init__()
        self.connection, self.signal, self.deadline = connection, signal, deadline

    def makefile(self, _mode: str) -> io.BufferedReader:
        return io.BufferedReader(self)

    def readable(self) -> bool:
        return True

    def remaining(self) -> float:
        self.signal.check()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def readinto(self, buffer) -> int:
        while True:
            # On Windows shutdown alone can leave a reader waiting for data.
            self.connection.settimeout(min(self.remaining(), 0.2))
            try:
                return self.connection.recv_into(buffer)
            except TimeoutError:
                continue


def _stream_post(host: str, path: str, headers: dict, payload: dict, timeout: int,
                 signal: Cancellation) -> Iterator[dict]:
    connection = http.client.HTTPSConnection(host, timeout=timeout)
    deadline = time.monotonic() + timeout
    try:
        signal.check()
        connection.connect()
        connection_socket = connection.sock

        def interrupt():
            try:
                connection_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        reader = _ResponseReader(connection_socket, signal, deadline)
        with signal.on_cancel(interrupt):
            connection_socket.settimeout(reader.remaining())
            connection.request("POST", path, json.dumps(payload).encode("utf-8"),
                               {"Content-Type": "application/json", "Accept": "text/event-stream", **headers})
            # The request owns its socket for the entire stream, including connection-close responses.
            with http.client.HTTPResponse(reader, method="POST") as reply:
                reply.begin()
                _check_status(reply.status)
                yield from _events(reply, reader.remaining)
            signal.check()
    except TimeoutError:
        signal.check()
        raise ProviderError("The provider request timed out.") from None
    except (OSError, http.client.HTTPException):
        signal.check()
        raise ProviderError("The provider stream disconnected. Check your network and try again.") from None
    finally:
        connection.close()


def _usage(input_tokens: object, output_tokens: object, total_tokens: object = None) -> TokenUsage:
    def count(value: object) -> int | None:
        return value if type(value) is int and 0 <= value <= 2**63 - 1 else None

    input_count, output_count, total = map(count, (input_tokens, output_tokens, total_tokens))
    if total is None and input_count is not None and output_count is not None:
        total = count(input_count + output_count)
    return TokenUsage(input_count, output_count, total)


class ApiRunner:
    def __init__(self, provider: str):
        self.provider = provider

    def _key(self) -> str:
        key = credential(self.provider)
        if not key:
            raise ProviderError(f"Set {PROVIDERS[self.provider][1]} in the server environment and relaunch AI Team.")
        return key

    def generate(self, request: ModelRequest) -> ModelResponse:
        host, path, headers, payload = self._endpoint(request, self._key())
        decode = {"openai": self._openai, "anthropic": self._anthropic, "google": self._google}[self.provider]
        try:
            return decode(_post(host, path, headers, payload, request.timeout_seconds))
        except (ValueError, KeyError, AttributeError, IndexError, TypeError):
            raise ProviderError("The provider returned an invalid response.") from None

    def stream(self, request: ModelRequest, signal: Cancellation, publish: Callable[[str], None]) -> ModelResponse:
        host, path, headers, payload = self._endpoint(request, self._key(), streaming=True)
        consume = {"openai": self._stream_openai, "anthropic": self._stream_anthropic, "google": self._stream_google}[self.provider]
        try:
            with closing(_stream_post(host, path, headers, payload, request.timeout_seconds, signal)) as events:
                result = consume(events, publish)
            signal.check()
            return result
        except (ValueError, KeyError, AttributeError, IndexError, TypeError):
            raise ProviderError("The provider returned an invalid stream.") from None

    def _endpoint(self, request: ModelRequest, key: str, *, streaming: bool = False) -> tuple:
        messages = [{"role": msg.role, "content": msg.content} for msg in request.messages]
        if self.provider == "openai":
            payload = {"model": request.model, "store": False, "max_output_tokens": request.max_output_tokens, "input": messages}
            if request.system_prompt:
                payload["instructions"] = request.system_prompt
            if streaming:
                payload["stream"] = True
            return "api.openai.com", "/v1/responses", {"Authorization": f"Bearer {key}"}, payload
        if self.provider == "anthropic":
            payload = {"model": request.model, "max_tokens": request.max_output_tokens, "messages": messages}
            if request.system_prompt:
                payload["system"] = request.system_prompt
            if streaming:
                payload["stream"] = True
            return "api.anthropic.com", "/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"}, payload
        payload = {"generationConfig": {"maxOutputTokens": request.max_output_tokens}, "contents": [
            {"role": "model" if msg.role == "assistant" else "user", "parts": [{"text": msg.content}]}
            for msg in request.messages]}
        if request.system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": request.system_prompt}]}
        method = "streamGenerateContent?alt=sse" if streaming else "generateContent"
        path = f"/v1beta/models/{quote(request.model, safe='')}:{method}"
        return "generativelanguage.googleapis.com", path, {"x-goog-api-key": key}, payload

    @staticmethod
    def _openai(data: dict) -> ModelResponse:
        if data.get("error") or data.get("status") not in {"completed", "incomplete"}:
            raise ProviderError("OpenAI could not complete this response.")
        parts = [part.get("text", part.get("refusal", "")) for item in data["output"] if item["type"] == "message"
                 for part in item["content"] if part["type"] in {"output_text", "refusal"}]
        usage = data.get("usage") or {}
        limited = (data.get("incomplete_details") or {}).get("reason") == "max_output_tokens"
        return ModelResponse("\n".join(parts), _usage(usage.get("input_tokens"), usage.get("output_tokens"),
                                                     usage.get("total_tokens")), data.get("model"), limited)

    @staticmethod
    def _anthropic(data: dict) -> ModelResponse:
        usage = data.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        if type(input_tokens) is int:
            input_tokens += usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        parts = [part["text"] for part in data["content"] if part["type"] == "text"]
        return ModelResponse("\n".join(parts), _usage(input_tokens, usage.get("output_tokens")),
                             data.get("model"), data.get("stop_reason") == "max_tokens")

    @staticmethod
    def _google(data: dict) -> ModelResponse:
        candidates = data.get("candidates") or []
        answer = candidates[0] if candidates else {}
        parts = [part["text"] for part in (answer.get("content") or {}).get("parts", [])
                 if "text" in part and not part.get("thought")]
        usage = data.get("usageMetadata") or {}
        output = usage.get("candidatesTokenCount")
        thoughts = usage.get("thoughtsTokenCount")
        if type(thoughts) is int and (output is None or type(output) is int):
            output = (output or 0) + thoughts
        return ModelResponse("\n".join(parts), _usage(usage.get("promptTokenCount"), output, usage.get("totalTokenCount")),
                             data.get("modelVersion"), answer.get("finishReason") == "MAX_TOKENS")

    @staticmethod
    def _stream_openai(events: Iterator[dict], publish: Callable[[str], None]) -> ModelResponse:
        preview = _Preview(publish)
        for event in events:
            kind = event.get("type")
            if kind in {"response.output_text.delta", "response.refusal.delta"}:
                preview.add(event["delta"])
            elif kind in {"response.completed", "response.incomplete"}:
                return ApiRunner._openai(event["response"])
            elif kind in {"response.failed", "error"}:
                raise ProviderError("OpenAI could not complete this stream.")
        raise ProviderError("OpenAI ended the stream before returning a final response.")

    @staticmethod
    def _stream_anthropic(events: Iterator[dict], publish: Callable[[str], None]) -> ModelResponse:
        message = {}
        blocks: dict[int, list[str]] = {}
        usage = {}
        preview = _Preview(publish)
        for event in events:
            kind = event.get("type")
            if kind == "message_start":
                message = event["message"]
                usage.update(message.get("usage") or {})
            elif kind == "content_block_start" and event["content_block"]["type"] == "text":
                initial = event["content_block"].get("text", "")
                preview.add(("\n" if blocks else "") + initial)
                blocks[event["index"]] = [initial]
            elif kind == "content_block_delta" and event["delta"]["type"] == "text_delta":
                delta = event["delta"]["text"]
                blocks[event["index"]].append(delta)
                preview.add(delta)
            elif kind == "message_delta":
                message.update(event["delta"])
                usage.update(event.get("usage") or {})
            elif kind == "message_stop" and message:
                return ApiRunner._anthropic({**message, "usage": usage,
                                             "content": [{"type": "text", "text": "".join(parts)} for parts in blocks.values()]})
            elif kind == "error":
                raise ProviderError("Anthropic could not complete this stream.")
        raise ProviderError("Anthropic ended the stream before returning a final response.")

    @staticmethod
    def _stream_google(events: Iterator[dict], publish: Callable[[str], None]) -> ModelResponse:
        parts = []
        usage = {}
        model = None
        finish = None
        blocked = False
        preview = _Preview(publish)
        for event in events:
            if event.get("error"):
                raise ProviderError("Google could not complete this stream.")
            model = event.get("modelVersion") or model
            usage.update(event.get("usageMetadata") or {})
            for candidate in (event.get("candidates") or [])[:1]:
                finish = candidate.get("finishReason") or finish
                added = [part["text"] for part in (candidate.get("content") or {}).get("parts", [])
                         if "text" in part and not part.get("thought")]
                if added:
                    parts.extend(added)
                    preview.add("".join(added))
            reason = (event.get("promptFeedback") or {}).get("blockReason")
            blocked = blocked or reason not in {None, "BLOCK_REASON_UNSPECIFIED"}
        if not blocked and finish in {None, "FINISH_REASON_UNSPECIFIED"}:
            raise ProviderError("Google ended the stream before returning a final response.")
        return ApiRunner._google({"modelVersion": model, "usageMetadata": usage, "candidates": [
            {"finishReason": finish, "content": {"parts": [{"text": "".join(parts)}]}}]})
