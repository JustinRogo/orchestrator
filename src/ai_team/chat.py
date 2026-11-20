"""Single-model sessions and bounded background dispatch."""
from __future__ import annotations

import re
import threading
import uuid
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .chat_providers import (
    MAX_TEXT_BYTES, PROVIDERS, ApiRunner, ChatMessage, ModelRequest, ModelRunner, ProviderError, credential,
    redact_credentials, safe_preview,
)
from .chat_store import ChatStore
from .collaboration import ACTIVE, GROUP_STRATEGIES, SESSION_SETTINGS, ChatBusy, ChatConflict, Collaboration, options as collaboration_options
from .config import load, team_dir
from .live import Cancellation, Cancelled, LiveUpdates


SETTING_LIMITS = {"max_turns": (30, 1, 100), "max_output_tokens": (4096, 256, 16384), "timeout_seconds": (60, 5, 180)}
HISTORY_TURNS = 12
HISTORY_BYTES = 40_000
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
REQUEST_ID = re.compile(r"[a-f0-9]{32}")


def _text(value: object, name: str, max_bytes: int, *, blank: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text.")
    value = value.strip()
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{name} must be valid Unicode text.") from None
    if length > max_bytes or not (value or blank):
        raise ValueError(f"Enter {name.lower()} of at most {max_bytes:,} UTF-8 bytes.")
    if redact_credentials(value) != value:
        raise ValueError(f"Remove the API key from {name.lower()} before continuing.")
    return value


def _model(body: dict, catalog: dict[str, list[str]]) -> tuple[str, str]:
    provider, model = body.get("provider"), body.get("model")
    if not isinstance(provider, str) or not isinstance(model, str) or model not in catalog.get(provider, []):
        raise ValueError("Choose a configured provider and model.")
    if not credential(provider):
        raise ValueError(f"Set {PROVIDERS[provider][1]} in the server environment and relaunch AI Team.")
    return provider, model


def _settings(body: dict) -> dict:
    result = {"system_prompt": _text(body.get("system_prompt", ""), "System instructions", 4000, blank=True)}
    for name, (default, minimum, maximum) in SETTING_LIMITS.items():
        value = body.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name.replace('_', ' ').capitalize()} must be between {minimum} and {maximum}.")
        result[name] = value
    return result


def _context(turns: list[dict]) -> tuple[list[dict], bool]:
    complete = [turn for turn in turns if turn["status"] == "complete"]
    included = []
    size = 0
    for turn in reversed(complete[-HISTORY_TURNS:]):
        size += len(turn["prompt"].encode("utf-8")) + len(turn["response"].encode("utf-8"))
        if size > HISTORY_BYTES:
            break
        included.append(turn)
    return list(reversed(included)), len(included) != len(complete)


def _request_id(body: dict) -> str:
    value = body.get("request_id")
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise ValueError("Send a unique 32-character hexadecimal request ID.")
    return value


class ChatService:
    def __init__(self, root: Path, runner: Callable[[str], ModelRunner] = ApiRunner):
        self.root = root
        self.path = team_dir(root) / "state.sqlite3"
        self.runner = runner
        self.owner = uuid.uuid4().hex
        self.slots = threading.BoundedSemaphore(4)
        self.lock = threading.Lock()
        self.closed = False
        self.submitted = False
        self.live = LiveUpdates()
        self.collaboration = Collaboration(self.path, self.owner, self.slots, self.live, self._generate)

    def _catalog(self) -> dict[str, list[str]]:
        catalog = load(self.root, allow_missing=True)["chat"]["models"]
        if not isinstance(catalog, dict) or catalog.keys() - PROVIDERS.keys():
            raise ValueError("chat.models must map supported provider IDs to model lists.")
        for models in catalog.values():
            if not isinstance(models, list) or any(not isinstance(model, str) or not MODEL_ID.fullmatch(model)
                                                  or redact_credentials(model) != model for model in models):
                raise ValueError("Each chat.models entry must be a list of provider model IDs.")
        return catalog

    def models(self) -> dict:
        providers = []
        for provider, models in self._catalog().items():
            name, env = PROVIDERS[provider]
            providers.append({"id": provider, "name": name, "credential_variable": env,
                              "configured": bool(credential(provider)), "models": list(dict.fromkeys(models))})
        return {"providers": providers,
                "defaults": {name: limits[0] for name, limits in SETTING_LIMITS.items()}}

    def list(self) -> dict:
        with closing(ChatStore(self.path)) as store:
            return {"sessions": store.list_sessions()}

    def _group_options(self, strategy: str, body: dict, catalog: dict[str, list[str]]) -> dict:
        if not isinstance(strategy, str) or strategy not in GROUP_STRATEGIES:
            raise ValueError("Choose a multi-model collaboration mode.")
        return collaboration_options(strategy, body, lambda participant: _model(participant, catalog), _text)

    def teams(self) -> dict:
        with closing(ChatStore(self.path)) as store:
            return {"teams": store.list_teams()}

    def save_team(self, body: dict) -> dict:
        name = _text(body.get("name"), "Team name", 120)
        strategy = body.get("strategy")
        options = self._group_options(strategy, body, self._catalog())
        configuration = {"strategy": strategy, "strategy_options": options, **_settings(body)}
        with closing(ChatStore(self.path)) as store, store.db:
            team = store.create_team(name, configuration)
            if team is None:
                raise ChatConflict("A team already uses that name. Enter a different name.")
        return team

    def create(self, body: dict) -> dict:
        strategy = body.get("strategy", "single")
        catalog = self._catalog()
        options = None
        if strategy == "single":
            provider, model = _model(body, catalog)
        else:
            options = self._group_options(strategy, body, catalog)
            first = options["participants"][0]
            provider, model = first["provider"], first["model"]
        settings = _settings(body)
        with closing(ChatStore(self.path)) as store, store.db:
            return store.create(provider, model, settings, strategy=strategy, strategy_options=options)

    def get(self, session_id: str) -> dict:
        with closing(ChatStore(self.path)) as store, store.db:
            store.db.execute("BEGIN IMMEDIATE")
            session = store.session(session_id)
            shared = {"session": session, "handoffs": store.handoffs(session_id)}
            if session["strategy"] != "single":
                return {**shared, "runs": self.collaboration.view(store, session)}
            store.expire_turns()
            turns = store.turns(session_id)
        history, _ = _context(turns)
        return {**shared, "turns": turns, "context_providers": sorted({turn["provider"] for turn in history})}

    def branch(self, session_id: str, body: dict) -> dict:
        request_id = _request_id(body)
        checkpoint = body.get("checkpoint")
        with closing(ChatStore(self.path)) as store, store.db:
            store.db.execute("BEGIN IMMEDIATE")
            source = store.session(session_id)
            single = source["strategy"] == "single"
            if not (type(checkpoint) is int if single else isinstance(checkpoint, str) and REQUEST_ID.fullmatch(checkpoint)):
                raise ValueError("Choose a saved turn or run to branch from.")
            try:
                branch = store.session(request_id)
            except KeyError:
                branch = None
            if branch is not None:
                origin = branch["branch_source"] or {}
                if origin.get("session_id") == session_id and origin.get("checkpoint") == checkpoint:
                    return branch
                raise ChatConflict("This request ID already belongs to another conversation.")
            if single:
                store.expire_turns()
                history = [turn for turn in store.turns(session_id) if turn["id"] <= checkpoint]
                if not history or history[-1]["id"] != checkpoint or any(turn["status"] == "pending" for turn in history):
                    raise ChatConflict("Choose a saved turn that has finished.")
                provider, model = history[-1]["provider"], history[-1]["model"]
            else:
                self.collaboration.view(store, source)
                history = []
                for run in store.runs(session_id):
                    history.append(run)
                    if run["id"] == checkpoint:
                        break
                if not history or history[-1]["id"] != checkpoint or any(run["status"] in ACTIVE for run in history):
                    raise ChatConflict("Choose a saved run with no active stages in its history.")
                provider, model = source["provider"], source["model"]
            origin = {"session_id": session_id, "title": source["title"], "checkpoint": checkpoint,
                      "history_count": len(history)}
            settings = {key: source[key] for key in SESSION_SETTINGS}
            branch = store.create(provider, model, settings, strategy=source["strategy"],
                                  strategy_options=source["strategy_options"], branch_source=origin, session_id=request_id)
            if single:
                store.copy_turns(session_id, branch["id"], checkpoint)
            else:
                for run in history:
                    run.update(id=uuid.uuid4().hex, session_id=branch["id"], request_id=uuid.uuid4().hex, owner=None,
                               job=None, deadline=None, inherited=True)
                    run.pop("promotions", None)
                    store.save_run(run)
            return branch

    def send(self, session_id: str, body: dict) -> dict:
        prompt = _text(body.get("prompt"), "Message", 12_000)
        request_id = _request_id(body)
        with self.lock:
            if self.closed:
                raise ChatBusy("AI Team is shutting down.")
            with closing(ChatStore(self.path)) as store:
                session = store.session(session_id)
            if session["strategy"] != "single":
                context = _text(body.get("context", ""), "Shared context", 12_000, blank=True)
                return self.collaboration.submit(session_id, request_id, prompt, context,
                                                 body.get("share_history") is True, body.get("participant"))
            return self._submit(session_id, request_id, prompt, body)

    def events(self, session_id: str, after: int | None) -> dict:
        with closing(ChatStore(self.path)) as store:
            store.session(session_id)
        if not self.live.listeners.acquire(blocking=False):
            raise ChatBusy("Too many live dashboard listeners. Try again shortly.")
        try:
            return self.live.wait(session_id, after)
        finally:
            self.live.listeners.release()

    def cancel(self, session_id: str, body: dict) -> dict:
        turn_id = body.get("turn_id")
        if type(turn_id) is not int or not 0 < turn_id < 2**63:
            raise ValueError("Choose the current chat reply to cancel.")
        with self.lock:
            with closing(ChatStore(self.path)) as store, store.db:
                store.db.execute("BEGIN IMMEDIATE")
                turn = store.turn_claim(session_id, turn_id)
                if turn["cancelled_at"]:
                    return {"turn_id": turn_id}
                if turn["status"] != "pending":
                    raise ChatConflict("The reply has already finished.")
                if turn["owner"] != self.owner:
                    raise ChatConflict("Cancel this reply from the dashboard that started it.")
                store.cancel_turn(turn_id, "Cancelled locally. Check provider usage before retrying.")
            self.live.cancel(f"turn:{turn_id}")
            self.live.changed(session_id)
        return {"turn_id": turn_id}

    def control(self, session_id: str, run_id: str, action: str, body: dict) -> dict:
        with self.lock:
            if self.closed:
                raise ChatBusy("AI Team is shutting down.")
            if action == "expand":
                body = {**body, "instruction": _text(body.get("instruction"), "Follow-up", 4_000)}
            if action == "next":
                body = {**body, "instruction": _text(body.get("instruction", ""), "Turn direction", 4_000, blank=True)}
            if action == "promote":
                with closing(ChatStore(self.path)) as store:
                    session = store.session(session_id)
                if session["strategy"] != "compare":
                    raise ChatConflict("Only Compare runs can start a Council this way.")
                participants = session["strategy_options"]["participants"]
                settings = collaboration_options("council", {
                    **session["strategy_options"], "review_rounds": 1, **body, "participants": participants,
                }, lambda participant: (participant["provider"], participant["model"]), _text)
                return self.collaboration.promote(session_id, run_id, _request_id(body), settings,
                                                  body.get("share_history") is True)
            return self.collaboration.control(session_id, run_id, action, body)

    def _submit(self, session_id: str, request_id: str, prompt: str, body: dict) -> dict:
        reserved = False
        launched = False
        try:
            with closing(ChatStore(self.path)) as store, store.db:
                store.db.execute("BEGIN IMMEDIATE")
                store.expire_turns()
                session = store.session(session_id)
                if existing := store.find_request(session_id, request_id):
                    if (existing["prompt"], existing["provider"], existing["model"]) != (prompt, body.get("provider"), body.get("model")):
                        raise ChatConflict("This request ID already belongs to another message.")
                    return {"turn_id": existing["id"]}
                provider, model = _model(body, self._catalog())
                turns = store.turns(session_id)
                if turns and turns[-1]["status"] == "pending":
                    raise ChatConflict("Wait for this conversation's current response.")
                if len(turns) - (session["branch_source"] or {}).get("history_count", 0) >= session["max_turns"]:
                    raise ChatConflict("This conversation has reached its turn limit. Start a new conversation to continue.")
                history, truncated = _context(turns)
                if any(turn["provider"] != provider for turn in history) and body.get("share_history") is not True:
                    raise ChatConflict(f"Confirm that you want to send recent conversation history to {PROVIDERS[provider][0]}.")
                if not self.slots.acquire(blocking=False):
                    raise ChatBusy("Four chat turns are already running. Wait for a response and try again.")
                reserved = True
                messages = [message for turn in history for message in
                            (ChatMessage("user", turn["prompt"]), ChatMessage("assistant", turn["response"]))]
                messages.append(ChatMessage("user", prompt))
                request = ModelRequest(model, messages, session["system_prompt"], session["max_output_tokens"],
                                       session["timeout_seconds"])
                turn_id = store.add_turn(session, request_id, provider, model, prompt, self.owner, len(history), truncated)
            self.submitted = True
            job = f"turn:{turn_id}"
            signal = self.live.begin(session_id, job)
            self.live.changed(session_id)
            try:
                threading.Thread(target=self._run, args=(turn_id, provider, request, job, signal), daemon=True,
                                 name=f"ai-team-chat-{turn_id}").start()
                launched = True
            except RuntimeError:
                with closing(ChatStore(self.path)) as store:
                    store.finish_turn(turn_id, error="AI Team could not start this turn.")
                self.live.end(job)
            return {"turn_id": turn_id}
        finally:
            if reserved and not launched:
                self.slots.release()

    def _generate(self, provider: str, request: ModelRequest, signal: Cancellation, job: str, target: str) -> dict:
        try:
            signal.check()
            runner = self.runner(provider)
            if callable(getattr(type(runner), "stream", None)):
                result = runner.stream(request, signal, lambda text: self.live.preview(job, target, safe_preview(text)))
            else:
                result = runner.generate(request)
            signal.check()
            text = redact_credentials(result.text)
            response_model = result.model if isinstance(result.model, str) and MODEL_ID.fullmatch(result.model) else None
            if response_model and redact_credentials(response_model) != response_model:
                response_model = None
            encoded = text.encode("utf-8")
            values = {"response": encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore"), "response_model": response_model,
                      "output_limited": result.output_limited or len(encoded) > MAX_TEXT_BYTES, **asdict(result.usage)}
            if not text.strip():
                values["response"] = None
                values["error"] = ("The model reached its output limit before returning text. Use a higher limit in a new conversation."
                                   if result.output_limited else "The provider did not return text. The prompt may have been blocked.")
            return values
        except Cancelled:
            return {"error": "Cancelled locally."}
        except ProviderError as error:
            return {"error": redact_credentials(str(error))}
        except TimeoutError:
            return {"error": "The provider request timed out."}
        except Exception:
            return {"error": "AI Team could not complete this turn."}

    def _run(self, turn_id: int, provider: str, request: ModelRequest, job: str, signal: Cancellation) -> None:
        try:
            values = self._generate(provider, request, signal, job, job)
            with closing(ChatStore(self.path)) as store:
                store.finish_turn(turn_id, **values)
        finally:
            self.live.end(job)
            self.slots.release()

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.collaboration.close()
            if self.submitted:
                with closing(ChatStore(self.path)) as store:
                    store.interrupt(self.owner)
            self.live.close()
