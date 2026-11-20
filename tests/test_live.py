from __future__ import annotations

import http.client
import io
import json
import socket
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ai_team.chat import ChatService
from ai_team.chat_providers import ApiRunner, ChatMessage, ModelRequest, ModelResponse, ProviderError, TokenUsage, _events
from ai_team.live import Cancellation, Cancelled
from ai_team.server import make_server

from test_chat import GOOGLE, KEYS, OPENAI, api_keys, submission


class ProviderStreamTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(api_keys(KEYS))
        self.request = ModelRequest("test-model", [ChatMessage("user", "Hello")], "", 512, 5)

    def stream(self, provider, events):
        previews = []
        with patch("ai_team.chat_providers._stream_post", return_value=(event for event in events)) as post:
            response = ApiRunner(provider).stream(self.request, Cancellation(), previews.append)
        return response, previews, post.call_args.args

    def test_openai_uses_authoritative_final_text_and_usage(self):
        response, previews, endpoint = self.stream("openai", [
            {"type": "response.reasoning_summary_text.delta", "delta": "Withheld reasoning"},
            {"type": "response.output_text.delta", "delta": "First"},
            {"type": "response.output_text.delta", "delta": " answer"},
            {"type": "response.completed", "response": {"status": "completed", "model": "snapshot", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "First answer"}]}],
                "usage": {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}}},
        ])
        self.assertEqual(previews, ["First", "First answer"])
        self.assertEqual(response, ModelResponse("First answer", TokenUsage(7, 2, 9), "snapshot"))
        self.assertEqual(endpoint[1], "/v1/responses")
        self.assertTrue(endpoint[3]["stream"])
        self.assertFalse(endpoint[3]["store"])

    def test_anthropic_merges_usage_and_streams_only_text_blocks(self):
        response, previews, endpoint = self.stream("anthropic", [
            {"type": "message_start", "message": {"model": "claude-id", "usage": {
                "input_tokens": 7, "output_tokens": 1, "cache_read_input_tokens": 3}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Private"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Claude"}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        ])
        self.assertEqual(previews, ["Claude"])
        self.assertEqual(response, ModelResponse("Claude", TokenUsage(10, 2, 12), "claude-id", True))
        self.assertTrue(endpoint[3]["stream"])

    def test_gemini_parses_final_and_usage_only_events(self):
        response, previews, endpoint = self.stream("google", [
            {"modelVersion": "gemini-id", "candidates": [{"content": {"parts": [{"text": "Private", "thought": True}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "Gemini"}]}}], "usageMetadata": {"promptTokenCount": 7}},
            {"candidates": [{"content": {"parts": [{"text": " reply"}]}, "finishReason": "STOP"}]},
            {"usageMetadata": {"candidatesTokenCount": 2, "thoughtsTokenCount": 1, "totalTokenCount": 10}},
        ])
        self.assertEqual(previews, ["Gemini", "Gemini reply"])
        self.assertEqual(response, ModelResponse("Gemini reply", TokenUsage(7, 3, 10), "gemini-id"))
        self.assertEqual(endpoint[1], "/v1beta/models/test-model:streamGenerateContent?alt=sse")

    def test_sse_framing_and_incomplete_terminal_event(self):
        frames = io.BytesIO(b': comment\r\nevent: sample\r\ndata: {"value":\r\ndata: 1}\r\n\r\n'
                           b'data: {"incomplete":true}')
        self.assertEqual(list(_events(frames, lambda: None)), [{"value": 1}])
        with self.assertRaisesRegex(ProviderError, "before returning a final response"):
            self.stream("openai", [{"type": "response.output_text.delta", "delta": "Unfinished text"}])

    def test_transport_cancellation_unblocks_an_idle_reader(self):
        client, peer = socket.socketpair()
        with closing(client), closing(peer):
            ready = threading.Event()
            next_frame = threading.Event()
            finished = threading.Event()
            failures = []
            signal = Cancellation()

            class Connection:
                sock = client

                def __init__(self, *_args, **_kwargs):
                    pass

                def connect(self):
                    pass

                def request(self, *_args):
                    pass

                def close(self):
                    client.close()

            def run():
                try:
                    def publish(text):
                        (next_frame if text == "Live again" else ready).set()

                    ApiRunner("openai").stream(self.request, signal, publish)
                except Exception as error:
                    failures.append(error)
                finally:
                    finished.set()

            def chunk(delta):
                frame = f'data: {{"type":"response.output_text.delta","delta":"{delta}"}}\n\n'.encode()
                return f"{len(frame):x}\r\n".encode() + frame + b"\r\n"

            peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
                         + chunk("Live"))
            with patch("ai_team.chat_providers.http.client.HTTPSConnection", Connection):
                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                try:
                    self.assertTrue(ready.wait(2))
                    self.assertFalse(finished.wait(0.3))
                    peer.sendall(chunk(" again"))
                    self.assertTrue(next_frame.wait(2))
                    signal.cancel()
                    self.assertTrue(finished.wait(2))
                    self.assertIsInstance(failures[0], Cancelled)
                    peer.settimeout(1)
                    self.assertEqual(peer.recv(16), b"")
                finally:
                    signal.cancel()
                    worker.join(2)


class LiveSessionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(api_keys(KEYS))
        self.server = make_server(self.root, 0, "live-token", chat_only=True)
        self.chat: ChatService = self.server.app.chat
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        for thread in threading.enumerate():
            if thread.name.startswith(("ai-team-chat-", "ai-team-collab-", "ai-team-reply-")):
                thread.join(3)

    def request(self, method, path, *, body=None, token=True, headers=None):
        headers = {**({"X-AI-Team-Token": "live-token"} if token else {}), **(headers or {})}
        if body is not None:
            headers["Content-Type"] = "application/json"
        with closing(http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)) as connection:
            connection.request(method, path, None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())

    def wait(self, predicate):
        for _ in range(180):
            if value := predicate():
                return value
            time.sleep(0.012)
        self.fail("Live state did not reach the expected condition")

    def test_events_reconnect_with_current_draft_then_persist_one_final_reply(self):
        published = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        secret = KEYS["OPENAI_API_KEY"]

        class Runner:
            def stream(self, request, signal, preview):
                preview(f"Prefix {secret[:-2]}")
                published.set()
                release.wait(3)
                preview(f"Prefix {secret} then text")
                return ModelResponse(f"Prefix {secret} then text", TokenUsage(7, 3, 10))

        self.chat.runner = lambda _: Runner()
        id_ = self.chat.create(OPENAI)["id"]
        endpoint = f"/api/v1/sessions/{id_}/events"
        self.assertEqual(self.request("GET", endpoint, token=False)[0], 403)
        self.assertEqual(self.request("GET", endpoint, headers={"Origin": "http://different.invalid"})[0], 403)
        self.assertEqual(self.request("GET", endpoint + "?after=bad")[0], 400)
        turn = self.chat.send(id_, submission("Hello"))["turn_id"]
        self.assertTrue(published.wait(2))
        status, first = self.request("GET", endpoint)
        self.assertEqual(status, 200)
        self.assertEqual(first["drafts"], {f"turn:{turn}": "Prefix "})
        self.assertEqual(self.request("GET", endpoint)[1], first)
        self.assertIsNone(self.chat.get(id_)["turns"][0]["response"])
        release.set()

        def committed():
            persisted = self.chat.get(id_)["turns"]
            return persisted if persisted[0]["status"] == "complete" else None

        persisted = self.wait(committed)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["response"], "Prefix [API key redacted] then text")
        def cleared():
            event = self.chat.live.wait(id_, None)
            return event if not event["drafts"] else None

        final = self.wait(cleared)
        self.assertGreater(final["state_sequence"], first["state_sequence"])
        self.assertEqual(self.request("GET", endpoint + f"?after={first['sequence']}")[1], final)

    def test_cancel_pending_turn_persists_status_and_discards_partial_text(self):
        published = threading.Event()
        closed = threading.Event()

        class Runner:
            def stream(self, request, signal, preview):
                with signal.on_cancel(closed.set):
                    preview("Temporary draft")
                    published.set()
                    closed.wait(3)
                    signal.check()

        self.chat.runner = lambda _: Runner()
        id_ = self.chat.create(OPENAI)["id"]
        turn = self.chat.send(id_, submission("Cancel this reply"))["turn_id"]
        self.assertTrue(published.wait(2))
        endpoint = f"/api/v1/sessions/{id_}/cancel"
        self.assertEqual(self.request("POST", endpoint, body={"turn_id": turn}, token=False)[0], 403)
        self.assertEqual(self.request("POST", endpoint, body={"turn_id": turn})[0], 202)
        self.assertTrue(closed.wait(1))
        self.assertEqual(self.request("POST", endpoint, body={"turn_id": turn})[0], 202)
        stored = ChatService(self.root).get(id_)["turns"]
        self.assertEqual([item["status"] for item in stored], ["cancelled"])
        self.assertIsNone(stored[0]["response"])
        self.assertEqual(self.chat.live.wait(id_, None)["drafts"], {})

    def test_failed_stream_keeps_failure_without_committing_a_draft(self):
        class Runner:
            def stream(self, request, signal, preview):
                preview("Temporary text")
                raise ProviderError("The stream ended early.")

        self.chat.runner = lambda _: Runner()
        id_ = self.chat.create(OPENAI)["id"]
        self.chat.send(id_, submission("Prompt"))
        failed = self.wait(lambda: next((turn for turn in self.chat.get(id_)["turns"] if turn["status"] == "failed"), None))
        self.assertEqual(failed["error"], "The stream ended early.")
        self.assertIsNone(failed["response"])

    def test_compare_dispatches_independent_replies_concurrently(self):
        entered = threading.Barrier(2)
        received = {}

        def runner(provider):
            class Runner:
                def generate(self, request):
                    received[provider] = request.messages
                    entered.wait(2)
                    return ModelResponse(f"Answer from {provider}", TokenUsage(7, 3, 10))
            return Runner()

        self.chat.runner = runner
        session = self.chat.create({"strategy": "compare", "participants": [OPENAI, GOOGLE], "max_invocations": 2})["id"]
        self.chat.send(session, submission("Independent task", context="Same context"))
        run = self.wait(lambda: next((run for run in self.chat.get(session)["runs"] if run["status"] == "complete"), None))
        self.assertEqual(received["openai"], received["google"])
        self.assertEqual([reply["input_reply_ids"] for reply in run["stages"][0]["replies"]], [[], []])
        self.assertEqual([reply["status"] for reply in run["stages"][0]["replies"]], ["complete", "complete"])
        self.assertEqual(run["budget"]["calls"], 2)

    def test_cancel_run_interrupts_all_active_streams_and_blocks_later_stages(self):
        started = {provider: threading.Event() for provider in ("openai", "google")}
        stopped = {provider: threading.Event() for provider in started}
        calls = []

        def runner(provider):
            class Runner:
                def stream(self, request, signal, preview):
                    calls.append(provider)
                    with signal.on_cancel(stopped[provider].set):
                        preview(f"Partial from {provider}")
                        started[provider].set()
                        stopped[provider].wait(3)
                        signal.check()
            return Runner()

        self.chat.runner = runner
        session = self.chat.create({"strategy": "council", "participants": [OPENAI, GOOGLE], "auto_advance": True})["id"]
        run_id = self.chat.send(session, {"prompt": "Independent task", "request_id": uuid.uuid4().hex,
                                         "share_history": True})["run_id"]
        self.assertTrue(all(event.wait(2) for event in started.values()))
        self.assertEqual(len(self.chat.live.wait(session, None)["drafts"]), 2)
        self.assertEqual(self.request("POST", f"/api/v1/sessions/{session}/runs/{run_id}/cancel", body={})[0], 202)
        self.assertTrue(all(event.wait(1) for event in stopped.values()))
        run = self.chat.get(session)["runs"][0]
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual([reply["status"] for reply in run["stages"][0]["replies"]], ["cancelled", "cancelled"])
        self.assertEqual(len(run["stages"]), 1)
        self.assertCountEqual(calls, ["openai", "google"])
        self.assertEqual(self.chat.live.wait(session, None)["drafts"], {})
