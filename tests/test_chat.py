from __future__ import annotations

import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from ai_team.chat import ChatConflict, ChatService
from ai_team.chat_providers import ApiRunner, ChatMessage, ModelRequest, ModelResponse, ProviderError, TokenUsage
from ai_team.cli import main
from ai_team.config import DEFAULT_CONFIG
from ai_team.server import make_server
from ai_team.store import Store


KEYS = {"OPENAI_API_KEY": "fake-openai-secret", "ANTHROPIC_API_KEY": "fake-anthropic-secret",
        "GEMINI_API_KEY": "fake-gemini-secret"}
OPENAI = {"provider": "openai", "model": "gpt-6.1-terra"}
GOOGLE = {"provider": "google", "model": "gemini-3.5-flash-lite"}


def submission(prompt, model=OPENAI, **options):
    return {**model, "prompt": prompt, "request_id": uuid.uuid4().hex, **options}


@contextmanager
def api_keys(keys):
    previous = {name: os.environ.get(name) for name in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(api_keys(KEYS))
        self.runner = Mock()
        self.runner.generate.return_value = ModelResponse("Model answer", TokenUsage(5, 3, 8), "actual-model")
        self.factory = Mock(return_value=self.runner)
        self.chat = ChatService(self.root, self.factory)

    def wait_for_turn(self, session_id, turn_id):
        for _ in range(100):
            for turn in self.chat.get(session_id)["turns"]:
                if turn["id"] == turn_id and turn["status"] != "pending":
                    return turn
            time.sleep(0.02)
        self.fail("Chat turn did not finish")

    def test_switch_preserves_context_attribution_settings_and_history_after_reopen(self):
        session = self.chat.create({**OPENAI, "system_prompt": "Be brief", "max_turns": 2})
        session_id = session["id"]
        first = submission("Remember this")
        first_id = self.chat.send(session_id, first)["turn_id"]
        self.assertEqual(self.wait_for_turn(session_id, first_id)["total_tokens"], 8)
        self.assertEqual(self.chat.send(session_id, first)["turn_id"], first_id)
        with self.assertRaisesRegex(ChatConflict, "another message"):
            self.chat.send(session_id, {**first, "prompt": "Different content"})
        second = submission("What did I ask?", GOOGLE)
        with self.assertRaisesRegex(ChatConflict, "send recent conversation history to Google"):
            self.chat.send(session_id, second)
        second_id = self.chat.send(session_id, {**second, "share_history": True})["turn_id"]
        self.wait_for_turn(session_id, second_id)
        request = self.runner.generate.call_args.args[0]
        self.assertEqual(request.messages, [ChatMessage("user", "Remember this"), ChatMessage("assistant", "Model answer"),
                                            ChatMessage("user", "What did I ask?")])
        self.assertEqual(request.system_prompt, "Be brief")
        self.assertEqual(self.runner.generate.call_count, 2)
        self.chat.close()
        reopened = ChatService(self.root, self.factory)
        session_view = reopened.get(session_id)
        self.assertEqual(reopened.list()["sessions"][0]["title"], "Remember this")
        self.assertEqual(session_view["session"]["provider"], "google")
        self.assertEqual(session_view["session"]["max_turns"], 2)
        self.assertEqual([t["provider"] for t in session_view["turns"]], ["openai", "google"])
        self.assertEqual([t["response_model"] for t in session_view["turns"]], ["actual-model", "actual-model"])
        with self.assertRaisesRegex(ChatConflict, "turn limit"):
            reopened.send(session_id, submission("One more", GOOGLE, share_history=True))
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / ".ai-team" / "worktrees").exists())

    def test_failed_prompt_remains_visible_but_is_not_replayed(self):
        session_id = self.chat.create(OPENAI)["id"]
        self.runner.generate.side_effect = [TimeoutError("sensitive diagnostic"),
                                            ModelResponse("", TokenUsage(9, 0, 9)),
                                            ModelResponse(f"Echo {KEYS['OPENAI_API_KEY']}")]
        with self.assertRaisesRegex(ValueError, "Remove the API key"):
            self.chat.send(session_id, submission(KEYS["GEMINI_API_KEY"]))
        for prompt, expected in (("First", "failed"), ("Second", "failed"), ("Third", "complete")):
            turn = self.wait_for_turn(session_id, self.chat.send(session_id, submission(prompt))["turn_id"])
            self.assertEqual(turn["status"], expected)
        turns = self.chat.get(session_id)["turns"]
        self.assertEqual(turns[0]["error"], "The provider request timed out.")
        self.assertEqual(turns[1]["total_tokens"], 9)
        self.assertEqual(turns[2]["response"], "Echo [API key redacted]")
        self.assertIsNone(turns[2]["total_tokens"])
        self.assertEqual(self.runner.generate.call_args.args[0].messages, [ChatMessage("user", "Third")])
        for key in KEYS.values():
            self.assertNotIn(key.encode(), self.chat.path.read_bytes())

    def test_branch_copies_the_selected_prefix_and_gets_its_own_prompt_limit(self):
        session_id = self.chat.create({**OPENAI, "max_turns": 2})["id"]
        first = self.wait_for_turn(session_id, self.chat.send(session_id, submission("Shared opening"))["turn_id"])
        self.wait_for_turn(session_id, self.chat.send(session_id, submission("Original continuation"))["turn_id"])
        body = {"checkpoint": first["id"], "request_id": uuid.uuid4().hex}
        branch = self.chat.branch(session_id, body)
        self.assertEqual(self.chat.branch(session_id, body), branch)
        self.assertEqual(self.runner.generate.call_count, 2)
        view = self.chat.get(branch["id"])
        self.assertEqual(view["session"]["branch_source"]["checkpoint"], first["id"])
        self.assertEqual([turn["prompt"] for turn in view["turns"]], ["Shared opening"])
        self.assertNotEqual(view["turns"][0]["id"], first["id"])
        for prompt in ("Alternative continuation", "Second branch prompt"):
            self.wait_for_turn(branch["id"], self.chat.send(branch["id"], submission(prompt))["turn_id"])
        with self.assertRaisesRegex(ChatConflict, "turn limit"):
            self.chat.send(branch["id"], submission("Over the limit"))
        messages = self.runner.generate.call_args_list[2].args[0].messages
        self.assertEqual([message.content for message in messages], ["Shared opening", "Model answer", "Alternative continuation"])
        self.assertEqual(len(self.chat.get(session_id)["turns"]), 2)

    def test_concurrent_duplicate_and_clean_shutdown_keep_one_interrupted_turn(self):
        gate = threading.Event()
        self.runner.generate.side_effect = lambda request: (gate.wait(4), ModelResponse("Late response"))[1]
        session_id = self.chat.create(OPENAI)["id"]
        body = submission("One in flight")
        try:
            first = self.chat.send(session_id, body)
            # Separate server objects share the database and must honor the same reservation.
            second = ChatService(self.root, self.factory)
            self.assertEqual(second.send(session_id, body), first)
            with self.assertRaisesRegex(ChatConflict, "current response"):
                second.send(session_id, submission("Another prompt"))
            self.chat.close()
            turn = second.get(session_id)["turns"][0]
            self.assertEqual(turn["status"], "interrupted")
            self.assertIsNone(turn["response"])
        finally:
            gate.set()
            worker = next((thread for thread in threading.enumerate() if thread.name == "ai-team-chat-1"), None)
            if worker:
                worker.join(timeout=2)
        self.assertEqual(self.runner.generate.call_count, 1)
        self.assertEqual(second.get(session_id)["turns"][0]["status"], "interrupted")

    def test_expired_pending_turn_recovers_without_redispatch(self):
        session_id = self.chat.create(OPENAI)["id"]
        with closing(sqlite3.connect(self.chat.path)) as db, db:
            db.execute("""INSERT INTO chat_turns (session_id, request_id, provider, model, prompt, status,
                          created_at, owner, deadline, context_turns, context_truncated)
                          VALUES (?, ?, ?, ?, ?, 'pending', ?, 'old-server', 1, 0, 0)""",
                       (session_id, uuid.uuid4().hex, OPENAI["provider"], OPENAI["model"], "Old turn", "2026-11-20"))
        self.assertEqual(self.chat.get(session_id)["turns"][0]["status"], "interrupted")
        self.runner.generate.assert_not_called()

    def test_large_history_is_truncated_by_complete_exchange(self):
        self.runner.generate.return_value = ModelResponse("a" * 21_000)
        session_id = self.chat.create(OPENAI)["id"]
        for prompt in ("First", "Second", "Third"):
            turn = self.wait_for_turn(session_id, self.chat.send(session_id, submission(prompt))["turn_id"])
        self.assertEqual(turn["context_turns"], 1)
        self.assertTrue(turn["context_truncated"])
        self.assertEqual(self.runner.generate.call_args.args[0].messages[0], ChatMessage("user", "Second"))
        self.assertEqual(len(self.chat.get(session_id)["turns"]), 3)

    def test_migration_preserves_legacy_tables_and_is_repeatable(self):
        legacy = Store(self.chat.path)
        task = legacy.create_task("Legacy task", DEFAULT_CONFIG)
        legacy.save_quota("codex", {"source": "example"})
        expected = legacy.db.execute("SELECT budget FROM tasks").fetchone()[0]
        legacy.close()
        self.chat.create(OPENAI)
        self.assertEqual(len(self.chat.list()["sessions"]), 1)
        upgraded = Store(self.chat.path)
        try:
            self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(upgraded.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(upgraded.get_task(task["id"])["title"], "Legacy task")
            self.assertEqual(upgraded.messages(task["id"])[0]["content"], "Legacy task")
            self.assertEqual(upgraded.db.execute("SELECT budget FROM tasks").fetchone()[0], expected)
            self.assertEqual(upgraded.agent_quotas(), {"codex": {"source": "example"}})
        finally:
            upgraded.close()

    def test_catalog_override_keeps_other_providers_and_marks_missing_key(self):
        config = self.root / ".ai-team" / "config.yaml"
        config.parent.mkdir()
        config.write_text("chat:\n  models:\n    openai: [alternate-model]\n    anthropic: []\n", encoding="utf-8")
        with api_keys({"OPENAI_API_KEY": ""}):
            models = {p["id"]: p for p in self.chat.models()["providers"]}
            self.assertEqual(models["openai"]["models"], ["alternate-model"])
            self.assertFalse(models["openai"]["configured"])
            self.assertEqual(models["anthropic"]["models"], [])
            self.assertEqual(models["google"]["models"], [GOOGLE["model"]])
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                self.chat.create({**OPENAI, "model": "alternate-model"})
            self.assertTrue(self.chat.create(GOOGLE)["id"])

    def test_chat_only_cli_skips_git_discovery(self):
        with patch("ai_team.cli.project_root", side_effect=AssertionError("Git called")), patch("ai_team.server.run_ui") as ui:
            self.assertEqual(main(["--project", str(self.root), "ui", "--chat-only", "--no-browser"]), 0)
        ui.assert_called_once_with(self.root.resolve(), 8765, False, chat_only=True)

    def test_local_http_contract_without_repository_or_live_provider(self):
        server = make_server(self.root, 0, "chat-token", chat_only=True)
        server.app.chat.runner = self.factory
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, data=None, *, authorized=True, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            request_headers = {"X-AI-Team-Token": "chat-token"} if authorized else {}
            request_headers.update(headers or {})
            if data is not None:
                request_headers["Content-Type"] = "application/json"
            try:
                connection.request(method, path, None if data is None else json.dumps(data), request_headers)
                response = connection.getresponse()
                return response.status, response.read().decode("utf-8")
            finally:
                connection.close()

        try:
            path = "/api/v1/sessions"
            self.assertEqual(request("GET", path, authorized=False)[0], 403)
            self.assertEqual(request("GET", "/chat", headers={"Host": "other.invalid"})[0], 403)
            self.assertEqual(request("POST", path, OPENAI, headers={"Origin": "http://other.invalid"})[0], 403)
            status, page = request("GET", "/classic/chat", authorized=False)
            self.assertEqual(status, 200)
            self.assertIn('<a href="/classic" hidden>Code Mode</a>', page)
            self.assertEqual(request("GET", "/api/overview")[0], 404)
            self.assertEqual(request("GET", f"{path}/{'f' * 32}")[0], 404)
            status, models = request("GET", "/api/v1/models")
            self.assertEqual(status, 200)
            self.assertTrue(all(key not in models for key in KEYS.values()))
            status, body = request("POST", path, OPENAI)
            self.assertEqual(status, 201)
            session = json.loads(body)["session"]
            url = f"{path}/{session['id']}"
            model_body = submission("Hello, chat")
            self.assertEqual(request("POST", f"{url}/messages", {**model_body, "prompt": "x" * 33000})[0], 413)
            status, body = request("POST", f"{url}/messages", model_body)
            self.assertEqual(status, 202)
            turn = self.wait_for_turn(session["id"], json.loads(body)["turn_id"])
            self.assertEqual(turn["response"], "Model answer")
            status, body = request("GET", url)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["turns"][0]["model"], OPENAI["model"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(api_keys(KEYS))
        self.request = ModelRequest("provider-model", [ChatMessage("user", "First"), ChatMessage("assistant", "Reply"),
                                                        ChatMessage("user", "Follow-up")], "Use plain text", 512, 20)

    def test_openai_responses_payload_usage_and_author(self):
        reply = {"status": "completed", "output": [{"type": "reasoning"},
                 {"type": "message", "content": [{"type": "output_text", "text": "GPT answer"}]}],
                 "model": "snapshot", "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}
        with patch("ai_team.chat_providers._post", return_value=reply) as post:
            model_response = ApiRunner("openai").generate(self.request)
        host, path, headers, body, timeout = post.call_args.args
        self.assertEqual((host, path, timeout), ("api.openai.com", "/v1/responses", 20))
        self.assertEqual(headers["Authorization"], f"Bearer {KEYS['OPENAI_API_KEY']}")
        self.assertFalse(body["store"])
        self.assertEqual(body["max_output_tokens"], 512)
        self.assertEqual(body["instructions"], "Use plain text")
        self.assertEqual(body["input"][1], {"role": "assistant", "content": "Reply"})
        self.assertEqual(model_response, ModelResponse("GPT answer", TokenUsage(10, 2, 12), "snapshot"))

    def test_anthropic_messages_payload_and_cache_usage(self):
        reply = {"content": [{"type": "text", "text": "Claude answer"}], "stop_reason": "max_tokens",
                 "usage": {"input_tokens": 10, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 3, "output_tokens": 2}}
        with patch("ai_team.chat_providers._post", return_value=reply) as post:
            model_response = ApiRunner("anthropic").generate(self.request)
        host, path, headers, body, timeout = post.call_args.args
        self.assertEqual((host, path, timeout), ("api.anthropic.com", "/v1/messages", 20))
        self.assertEqual(headers["x-api-key"], KEYS["ANTHROPIC_API_KEY"])
        self.assertEqual(body["max_tokens"], 512)
        self.assertEqual(body["system"], "Use plain text")
        self.assertEqual(body["messages"][1]["role"], "assistant")
        self.assertTrue(model_response.output_limited)
        self.assertEqual(model_response.usage, TokenUsage(17, 2, 19))

    def test_google_generate_content_payload_and_thought_usage(self):
        reply = {"candidates": [{"content": {"parts": [{"text": "Private thought", "thought": True},
                                                          {"text": "Gemini answer"}]}}], "modelVersion": "snapshot",
                 "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 2, "thoughtsTokenCount": 3, "totalTokenCount": 13}}
        with patch("ai_team.chat_providers._post", return_value=reply) as post:
            model_response = ApiRunner("google").generate(self.request)
        host, path, headers, body, timeout = post.call_args.args
        self.assertEqual((host, path, timeout), ("generativelanguage.googleapis.com", "/v1beta/models/provider-model:generateContent", 20))
        self.assertEqual(headers["x-goog-api-key"], KEYS["GEMINI_API_KEY"])
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 512)
        self.assertEqual(body["systemInstruction"], {"parts": [{"text": "Use plain text"}]})
        self.assertEqual(body["contents"][1], {"role": "model", "parts": [{"text": "Reply"}]})
        self.assertEqual(model_response, ModelResponse("Gemini answer", TokenUsage(8, 5, 13), "snapshot"))

    def test_transport_does_not_read_provider_error_body_and_closes(self):
        with patch("ai_team.chat_providers.http.client.HTTPSConnection") as connection:
            transport = connection.return_value
            reply = transport.getresponse.return_value
            for status, error in ((401, "access denied"), (404, "configured model"), (429, "rate limit"), (503, "unavailable")):
                reply.status = status
                with self.assertRaisesRegex(ProviderError, error):
                    ApiRunner("openai").generate(self.request)
            reply.read.assert_not_called()
            transport.request.side_effect = TimeoutError("secret diagnostic")
            with self.assertRaisesRegex(ProviderError, "timed out"):
                ApiRunner("openai").generate(self.request)
            self.assertEqual(transport.close.call_count, 5)

    def test_malformed_provider_payload_produces_safe_error(self):
        with patch("ai_team.chat_providers._post", return_value={"status": "completed", "output": None}):
            with self.assertRaisesRegex(ProviderError, "invalid response"):
                ApiRunner("openai").generate(self.request)
