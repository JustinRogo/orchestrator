from __future__ import annotations

import http.client
import json
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from ai_team.chat import ChatConflict, ChatService
from ai_team.chat_providers import ModelResponse, ProviderError, TokenUsage
from ai_team.chat_output import draft
from ai_team.chat_store import ChatStore
from ai_team.collaboration import ACTIVE
from ai_team.server import make_server

from test_chat import GOOGLE, KEYS, OPENAI, api_keys, submission


CLAUDE = {"provider": "anthropic", "model": "claude-sonnet-4-6"}
SYNTHESIS = {"recommendation": "Check the evidence first", "agreement": ["Both see the risk"],
             "disagreement": ["OpenAI and Google weigh the risk differently"],
             "uncertainties": ["No independent benchmark"], "next_steps": ["Run a benchmark"]}


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(api_keys(KEYS))
        self.responses = queue.Queue()
        self.requests = []
        self.chat = ChatService(self.root, self.runner)
        self.addCleanup(self.wait_for_workers)

    def runner(self, provider):
        class Runner:
            def generate(_, request):
                self.requests.append((provider, request))
                try:
                    custom = self.responses.get_nowait()
                except queue.Empty:
                    custom = None
                if isinstance(custom, Exception):
                    raise custom
                if callable(custom):
                    return custom(request)
                if custom is not None:
                    return custom
                if "Return only a JSON object" in request.system_prompt:
                    text = json.dumps(SYNTHESIS)
                elif "Evaluate the supplied" in request.system_prompt:
                    text = f"{provider} review with dissent"
                else:
                    text = f"{provider} independent opinion"
                return ModelResponse(text, TokenUsage(6, 4, 10), request.model)

        return Runner()

    def wait_for_workers(self):
        for thread in threading.enumerate():
            if thread.name.startswith("ai-team-collab-"):
                thread.join(timeout=6)

    def make_session(self, strategy, **settings):
        return self.chat.create({"strategy": strategy, "participants": [OPENAI, GOOGLE], "parallel": False, **settings})["id"]

    def start(self, session_id, **settings):
        payload = {"request_id": uuid.uuid4().hex, "prompt": "Which approach is better?", "context": "Baseline measurements",
                   "share_history": True, **settings}
        return self.chat.send(session_id, payload)["run_id"], payload

    def read(self, session_id, run_id):
        return next(run for run in self.chat.get(session_id)["runs"] if run["id"] == run_id)

    def wait(self, session_id, run_id, status=None):
        for _ in range(180):
            run = self.read(session_id, run_id)
            if (status is None and run["status"] not in ACTIVE) or run["status"] == status:
                return run
            time.sleep(0.015)
        self.fail(f"Run did not settle in {status or 'an idle state'}: {run}")

    def action(self, session_id, run, action, **body):
        return self.chat.control(session_id, run["id"], action, {"revision": run["revision"], **body})

    def test_roundtable_turns_accept_mentions_and_manual_direction(self):
        session = self.make_session("roundtable", roundtable_turns=3)
        with self.assertRaisesRegex(ChatConflict, "Confirm"):
            self.start(session, share_history=False)
        run_id, original = self.start(session, prompt="@Gemini: Explore this approach")
        run = self.wait(session, run_id, "ready")
        self.assertEqual(self.chat.send(session, original)["run_id"], run_id)
        self.assertEqual(run["stages"][0]["targets"], [1])
        self.assertEqual(len(self.requests), 1)
        with self.assertRaisesRegex(ValueError, "Speaker position"):
            self.action(session, run, "next", participant=True)
        self.action(session, run, "next", participant=1, instruction="Concentrate on the edge cases")
        run = self.wait(session, run_id, "ready")
        self.assertEqual(run["stages"][1]["targets"], [1])
        self.assertIn("Concentrate on the edge cases", self.requests[-1][1].messages[0].content)
        self.assertIn("google independent opinion", self.requests[-1][1].messages[0].content)
        self.action(session, run, "next")
        run = self.wait(session, run_id, "complete")
        self.assertEqual([provider for provider, _ in self.requests], ["google", "google", "openai"])
        self.assertIn("Concentrate on the edge cases", self.requests[-1][1].messages[0].content)
        self.assertEqual(run["stages"][2]["replies"][0]["input_reply_ids"], [1, 2])
        self.assertFalse(run["controls"]["next"])
        self.assertFalse(run["incomplete"])

    def test_council_branch_retains_replies_and_dissent_without_relaunching(self):
        session = self.make_session("council", auto_advance=True, max_turns=1)
        run_id, _ = self.start(session)
        run = self.wait(session, run_id, "complete")
        before = len(self.requests)
        branch = self.chat.branch(session, {"checkpoint": run_id, "request_id": uuid.uuid4().hex})
        view = self.chat.get(branch["id"])
        copied = view["runs"][0]
        self.assertNotEqual(copied["id"], run_id)
        self.assertEqual(copied["stages"], run["stages"])
        self.assertTrue(copied["inherited"])
        self.assertFalse(copied["controls"]["expand"])
        with self.assertRaisesRegex(ChatConflict, "new run"):
            self.action(branch["id"], copied, "stop")
        brief = draft(view)
        self.assertIn(SYNTHESIS["disagreement"][0], brief["uncertainties"])
        self.assertIn(SYNTHESIS["recommendation"], brief["decisions"])
        self.assertEqual(len(self.requests), before)
        new_run, _ = self.start(branch["id"], prompt="Investigate the alternative")
        self.wait(branch["id"], new_run, "complete")
        self.assertEqual(len(self.chat.get(session)["runs"]), 1)

    def test_relay_preserves_the_last_usable_handoff_after_a_failure(self):
        participants = [{**OPENAI, "role": "Propose", "instructions": "Draft two options"}, CLAUDE, GOOGLE]
        session = self.make_session("relay", participants=participants, auto_advance=True)
        self.responses.put(ModelResponse("Draft handoff"))
        self.responses.put(ProviderError("Unavailable"))
        run_id, payload = self.start(session)
        run = self.wait(session, run_id, "ready")
        self.assertTrue(run["incomplete"])
        self.assertEqual(len(self.requests), 2)
        self.assertIn("Draft two options", self.requests[0][1].system_prompt)
        self.assertIn("Critique", self.requests[1][1].system_prompt)
        self.assertIn("Draft handoff", self.requests[1][1].messages[0].content)
        self.action(session, run, "next")
        run = self.wait(session, run_id, "complete")
        text = self.requests[-1][1].messages[0].content
        self.assertIn("Draft handoff", text)
        self.assertIn(payload["context"], text)
        self.assertIn("Coverage gaps", text)
        self.assertTrue(run["incomplete"])
        self.assertEqual([provider for provider, _ in self.requests], ["openai", "anthropic", "google"])

    def test_debate_keeps_openings_independent_and_judge_separate(self):
        with self.assertRaisesRegex(ValueError, "3 to 4"):
            self.make_session("debate")
        speakers = [{**OPENAI, "role": "Defend adoption"}, CLAUDE, {**GOOGLE, "role": "Defend adoption"}]
        with self.assertRaisesRegex(ValueError, "distinct viewpoint"):
            self.make_session("debate", participants=speakers, judge=1)
        speakers[-1]["role"] = "Defend delaying"
        session = self.make_session("debate", participants=speakers, judge=1, rebuttal_rounds=2, auto_advance=True)
        run_id, _ = self.start(session)
        run = self.wait(session, run_id, "complete")
        self.assertEqual([stage["kind"] for stage in run["stages"]], ["argument", "rebuttal", "rebuttal", "judgment"])
        self.assertEqual([stage["targets"] for stage in run["stages"]], [[0, 2], [0, 2], [0, 2], [1]])
        inputs = [request for _, request in self.requests]
        self.assertEqual(inputs[0].messages, inputs[1].messages)
        self.assertIn("Defend adoption", inputs[0].system_prompt)
        self.assertIn("Defend delaying", inputs[1].system_prompt)
        self.assertIn("openai independent opinion", inputs[3].messages[0].content)
        self.assertIn("independent debate judge", inputs[-1].system_prompt)
        self.assertEqual(run["stages"][-1]["replies"][0]["synthesis"], SYNTHESIS)
        self.assertEqual(run["stages"][-1]["replies"][0]["input_reply_ids"], [1, 2, 3, 4, 5, 6])
        self.assertFalse(run["incomplete"])

    def test_relay_stops_at_the_budget_without_losing_completed_steps(self):
        session = self.make_session("relay", participants=[OPENAI, CLAUDE, GOOGLE], auto_advance=True, token_budget=10)
        run_id, _ = self.start(session)
        run = self.wait(session, run_id, "limited")
        self.assertTrue(run["incomplete"])
        self.assertEqual(run["budget"]["calls"], 1)
        self.assertFalse(run["controls"]["next"])
        self.assertEqual(run["stages"][0]["replies"][0]["status"], "complete")

    def test_compare_independence_idempotency_selection_and_private_followup(self):
        session = self.make_session("compare", system_prompt="Be concise")
        run_id, payload = self.start(session)
        run = self.wait(session, run_id, "complete")
        self.assertEqual(self.chat.send(session, payload)["run_id"], run_id)
        self.assertEqual(self.read(session, run_id)["revision"], run["revision"])
        self.assertEqual(len(self.requests), 2)
        left, right = (request for _, request in self.requests)
        self.assertEqual(left.messages, right.messages)
        self.assertEqual(left.system_prompt, right.system_prompt)
        self.assertIn(payload["context"], left.messages[0].content)
        self.assertNotIn("independent opinion", right.messages[0].content)
        self.assertEqual([reply["input_reply_ids"] for reply in run["stages"][0]["replies"]], [[], []])
        self.action(session, run, "choose", reply_id=2)
        run = self.read(session, run_id)
        self.assertEqual(run["selected_reply"], 2)
        self.action(session, run, "expand", reply_id=2, instruction="Give an example")
        run = self.wait(session, run_id, "complete")
        provider, request = self.requests[-1]
        self.assertEqual(provider, "google")
        self.assertIn("google independent opinion", request.messages[0].content)
        self.assertIn("Give an example", request.messages[0].content)
        self.assertNotIn("openai independent opinion", request.messages[0].content)
        self.assertEqual(run["stages"][-1]["replies"][0]["input_reply_ids"], [2])
        self.action(session, run, "synthesize", share_history=True)
        synthesized = self.wait(session, run_id, "complete")
        self.assertEqual(synthesized["stages"][-1]["replies"][0]["input_reply_ids"], [1, 2])
        self.assertNotIn("Give an example", self.requests[-1][1].messages[0].content)

    def test_council_manual_rounds_provenance_and_structured_dissent(self):
        session = self.make_session("council", synthesizer=1)
        with self.assertRaisesRegex(ChatConflict, "Confirm"):
            self.start(session, share_history=False)
        run_id, _ = self.start(session)
        first = self.wait(session, run_id, "ready")
        self.assertEqual(len(self.requests), 2)
        self.action(session, first, "next")
        review = self.wait(session, run_id, "ready")
        with self.assertRaisesRegex(ChatConflict, "run changed"):
            self.action(session, first, "next")
        for _, request in self.requests[2:4]:
            content = request.messages[0].content
            self.assertIn("openai/gpt-6.1-terra", content)
            self.assertIn("google/gemini-3.5-flash-lite", content)
            self.assertIn("openai independent opinion", content)
            self.assertIn("google independent opinion", content)
            self.assertNotIn("review with dissent", content)
        self.action(session, review, "next")
        run = self.wait(session, run_id, "complete")
        self.assertEqual([stage["kind"] for stage in run["stages"]], ["answer", "review", "synthesis"])
        self.assertEqual(run["budget"]["used"]["tokens"], 50)
        self.assertEqual(self.requests[-1][0], "google")
        self.assertIn("openai review with dissent", self.requests[-1][1].messages[0].content)
        synthesis = run["stages"][-1]["replies"][0]
        self.assertEqual(synthesis["synthesis"], SYNTHESIS)
        self.assertEqual(synthesis["input_reply_ids"], [1, 2, 3, 4])
        self.assertFalse(run["incomplete"])

    def test_partial_failure_waits_for_input_and_retries_one_model(self):
        session = self.make_session("council", auto_advance=True)
        self.responses.put(ProviderError("OpenAI was unavailable"))
        run_id, _ = self.start(session)
        run = self.wait(session, run_id, "ready")
        self.assertTrue(run["incomplete"])
        self.assertEqual(run["controls"]["retry"], [1])
        self.action(session, run, "retry", reply_id=1)
        run = self.wait(session, run_id, "ready")
        self.assertEqual([provider for provider, _ in self.requests], ["openai", "google", "openai"])
        self.assertEqual([reply["status"] for reply in run["stages"][0]["replies"]], ["failed", "complete", "complete"])
        self.assertFalse(run["incomplete"])
        self.action(session, run, "next", auto_advance=True)
        self.assertEqual(self.wait(session, run_id, "complete")["budget"]["calls"], 6)

    def test_early_synthesis_marks_missing_stage_and_supports_structured_retry(self):
        session = self.make_session("council")
        run_id, _ = self.start(session)
        run = self.wait(session, run_id, "ready")
        self.responses.put(ModelResponse("I did not return usable JSON.", TokenUsage(5, 5, 10)))
        self.action(session, run, "synthesize")
        run = self.wait(session, run_id, "complete")
        self.assertTrue(run["incomplete"])
        self.assertIn("planned peer review rounds were skipped", self.requests[-1][1].messages[0].content)
        self.assertIn("I did not return usable JSON", run["stages"][-1]["replies"][0]["response"])
        self.action(session, run, "retry", reply_id=3)
        run = self.wait(session, run_id, "complete")
        self.assertEqual(run["stages"][-1]["replies"][-1]["synthesis"], SYNTHESIS)
        self.assertTrue(run["incomplete"])

    def test_compare_promotion_reuses_answers_and_does_not_bill_them_again(self):
        source = self.make_session("compare", synthesizer=1)
        source_run, _ = self.start(source)
        run = self.wait(source, source_run, "complete")
        with self.assertRaisesRegex(ChatConflict, "Confirm"):
            self.action(source, run, "synthesize")
        request_id = uuid.uuid4().hex
        target = self.action(source, run, "promote", request_id=request_id, share_history=True)["session_id"]
        self.assertEqual(self.action(source, run, "promote", request_id=request_id, share_history=True)["session_id"], target)
        promoted = self.chat.get(target)["runs"][0]
        self.assertEqual(promoted["source"], {"session_id": source, "run_id": source_run})
        self.assertTrue(promoted["stages"][0]["replies"][0]["imported"])
        self.assertEqual(promoted["budget"]["calls"], 0)
        self.assertEqual(len(self.requests), 2)
        self.action(target, promoted, "next", auto_advance=True)
        final = self.wait(target, promoted["id"], "complete")
        self.assertEqual(final["budget"]["calls"], 3)
        self.assertEqual(len(self.requests), 5)
        self.action(source, self.read(source, source_run), "synthesize", share_history=True)
        self.assertEqual(self.wait(source, source_run, "complete")["stages"][-1]["replies"][0]["synthesis"], SYNTHESIS)

    def test_pause_then_stop_allow_inflight_reply_to_finish_without_later_calls(self):
        session = self.make_session("council", auto_advance=True)

        def block_for(gate):
            def reply(_):
                if not gate.wait(4):
                    raise TimeoutError
                return ModelResponse("Finished in-flight answer", TokenUsage(6, 4, 10))
            return reply

        gate = threading.Event()
        self.addCleanup(gate.set)
        self.responses.put(block_for(gate))
        run_id, _ = self.start(session)
        run = self.wait(session, run_id, "running")
        for _ in range(100):
            if self.requests:
                break
            time.sleep(0.01)
        self.action(session, run, "pause")
        gate.set()
        run = self.wait(session, run_id, "paused")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(run["stages"][0]["replies"][0]["status"], "complete")
        gate2 = threading.Event()
        self.addCleanup(gate2.set)
        self.responses.put(block_for(gate2))
        self.action(session, run, "next", auto_advance=True)
        for _ in range(100):
            if len(self.requests) == 2:
                break
            time.sleep(0.01)
        self.action(session, self.read(session, run_id), "stop")
        gate2.set()
        finished = self.wait(session, run_id, "stopped")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(finished["stages"]), 1)
        self.assertFalse(finished["controls"]["next"])

    def test_limits_apply_before_every_dispatch_and_between_rounds(self):
        session = self.make_session("council", auto_advance=True, token_budget=10)
        run_id, _ = self.start(session)
        run = self.wait(session, run_id, "limited")
        self.assertEqual(run["budget"]["calls"], 1)
        self.assertIn("Token budget reached", run["note"])
        self.assertEqual(len(self.requests), 1)
        session = self.make_session("council", auto_advance=True, max_invocations=2, review_rounds=3)
        run_id, _ = self.start(session)
        run = self.wait(session, run_id, "limited")
        self.assertEqual(len(run["stages"]), 1)
        self.assertEqual(len(self.requests), 3)
        session = self.make_session("council", auto_advance=True, review_rounds=2, max_invocations=7)
        run_id, _ = self.start(session)
        run = self.wait(session, run_id, "complete")
        self.assertEqual([round_["kind"] for round_ in run["stages"]], ["answer", "review", "review", "synthesis"])
        self.assertEqual(run["budget"]["calls"], 7)

    def test_unknown_token_usage_pauses_budgeted_run_without_billing_retries(self):
        session = self.make_session("compare", token_budget=100)
        self.responses.put(ModelResponse("Answer with unknown usage"))
        run_id, _ = self.start(session)
        run = self.wait(session, run_id, "limited")
        self.assertIn("missing or invalid token usage", run["note"])
        self.assertEqual(run["budget"]["calls"], 1)
        self.assertIsNone(run["budget"]["used"]["tokens"])

    def test_expired_run_can_be_resumed_without_late_worker_overwriting_it(self):
        session = self.make_session("council", auto_advance=True)
        called = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def delayed(_):
            called.set()
            release.wait(4)
            return ModelResponse("Late result")

        self.responses.put(delayed)
        run_id, _ = self.start(session)
        self.assertTrue(called.wait(3))
        with closing(ChatStore(self.chat.path)) as store, store.db:
            persisted = store.run(session, run_id)
            persisted["deadline"] = 0
            store.save_run(persisted)
        run = self.wait(session, run_id, "interrupted")
        self.assertEqual(run["stages"][0]["replies"][0]["status"], "interrupted")
        self.action(session, run, "retry", reply_id=1)
        run = self.wait(session, run_id, "paused")
        release.set()
        self.wait_for_workers()
        self.action(session, run, "next")
        run = self.wait(session, run_id, "ready")
        self.assertEqual([reply["status"] for reply in run["stages"][0]["replies"]], ["interrupted", "complete", "complete"])
        self.assertNotIn("Late result", json.dumps(run))

    def test_v1_chat_history_survives_migration(self):
        session = self.chat.create(OPENAI)["id"]
        turn_id = self.chat.send(session, submission("From version one"))["turn_id"]
        for _ in range(100):
            old = self.chat.get(session)["turns"][0]
            if old["status"] == "complete":
                break
            time.sleep(0.015)
        self.assertEqual(old["status"], "complete")
        with closing(sqlite3.connect(self.chat.path)) as db, db:
            db.execute("DROP TABLE chat_handoffs")
            db.execute("DROP TABLE chat_teams")
            db.execute("DROP TABLE chat_runs")
            db.execute("ALTER TABLE chat_sessions DROP COLUMN branch_source")
            db.execute("ALTER TABLE chat_sessions DROP COLUMN strategy")
            db.execute("ALTER TABLE chat_sessions DROP COLUMN strategy_options")
            db.execute("ALTER TABLE chat_turns DROP COLUMN cancelled_at")
            db.execute("PRAGMA user_version = 1")
        updated = self.chat.get(session)
        self.assertEqual(updated["session"]["strategy"], "single")
        self.assertEqual(updated["turns"][0]["id"], turn_id)
        self.assertEqual(updated["turns"][0]["prompt"], "From version one")
        self.assertTrue(self.make_session("compare"))

    def test_clean_restart_keeps_completed_and_interrupted_replies(self):
        session = self.make_session("compare")
        called = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def delayed(_):
            called.set()
            release.wait(4)
            return ModelResponse("Late result")

        self.responses.put(ModelResponse("Saved reply", TokenUsage(6, 4, 10)))
        self.responses.put(delayed)
        run_id, _ = self.start(session)
        self.assertTrue(called.wait(3))
        self.chat.close()
        reopened = ChatService(self.root, self.runner)
        self.chat = reopened
        run = self.wait(session, run_id, "interrupted")
        self.assertEqual([reply["status"] for reply in run["stages"][0]["replies"]], ["complete", "interrupted"])
        self.assertEqual(run["stages"][0]["replies"][0]["response"], "Saved reply")
        self.assertEqual(len(self.requests), 2)
        release.set()
        self.action(session, run, "retry", reply_id=2)
        completed = self.wait(session, run_id, "complete")
        self.assertEqual([provider for provider, _ in self.requests], ["openai", "google", "google"])
        self.assertNotIn("Late result", json.dumps(completed))

    def test_run_api_keeps_token_and_origin_requirements(self):
        server = make_server(self.root, 0, "token", chat_only=True)
        server.app.chat.runner = self.runner
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(path, body, headers):
            with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as client:
                client.request("POST", path, json.dumps(body), {"Content-Type": "application/json", **headers})
                response = client.getresponse()
                return response.status, json.loads(response.read())

        def get(path, headers):
            with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as client:
                client.request("GET", path, headers=headers)
                response = client.getresponse()
                return response.status, json.loads(response.read())

        try:
            session = self.make_session("compare")
            prefix = f"/api/v1/sessions/{session}"
            auth = {"X-AI-Team-Token": "token"}
            status, submitted = post(f"{prefix}/messages", submission("Compare these"), auth)
            self.assertEqual(status, 202)
            run = self.wait(session, submitted["run_id"], "complete")
            action = f"{prefix}/runs/{run['id']}/choose"
            self.assertEqual(post(action, {"reply_id": 1}, {})[0], 403)
            self.assertEqual(post(action, {"reply_id": 1}, {**auth, "Origin": "http://other.invalid"})[0], 403)
            self.assertEqual(post(action, {"reply_id": 1}, auth)[0], 202)
            self.assertEqual(self.read(session, run["id"])["selected_reply"], 1)
            path = "/api/v1/teams"
            self.assertEqual(get(path, {})[0], 403)
            team_body = {"name": "Writing team", "strategy": "relay", "system_prompt": "Use direct language",
                         "participants": [{**OPENAI, "role": "Writer", "instructions": "Outline the draft"}, GOOGLE]}
            self.assertEqual(post(path, team_body, {**auth, "Origin": "http://other.invalid"})[0], 403)
            status, created = post(path, team_body, auth)
            self.assertEqual(status, 201)
            self.assertEqual(post(path, {**team_body, "name": "writing team"}, auth)[0], 409)
            self.assertEqual(get(path, auth), (200, {"teams": [created["team"]]}))
            with closing(ChatService(self.root, self.runner)) as reopened:
                saved = reopened.teams()["teams"][0]
                new = reopened.create({**saved, **saved["strategy_options"]})
            self.assertEqual(new["strategy_options"], saved["strategy_options"])
            self.assertEqual(new["system_prompt"], team_body["system_prompt"])
            self.assertEqual(new["strategy_options"]["participants"][0]["instructions"], "Outline the draft")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
