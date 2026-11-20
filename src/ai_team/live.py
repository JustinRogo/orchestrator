"""Bounded, process-local live previews and cooperative cancellation."""
from __future__ import annotations

import threading
from collections import OrderedDict
from contextlib import contextmanager
from typing import Callable


class Cancelled(Exception):
    pass


class Cancellation:
    def __init__(self):
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._callbacks: set[Callable[[], None]] = set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.requested:
            raise Cancelled

    def cancel(self) -> None:
        with self._lock:
            self._event.set()
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            callback()

    @contextmanager
    def on_cancel(self, callback: Callable[[], None]):
        with self._lock:
            self.check()
            self._callbacks.add(callback)
        try:
            yield
        finally:
            with self._lock:
                self._callbacks.discard(callback)


class LiveUpdates:
    def __init__(self):
        self._condition = threading.Condition()
        self._sequence = 0
        self._versions: OrderedDict[str, tuple[int, int]] = OrderedDict()
        self._jobs: dict[str, tuple[str, Cancellation]] = {}
        self._drafts: dict[tuple[str, str], str] = {}
        self.listeners = threading.BoundedSemaphore(16)

    def begin(self, session_id: str, job: str) -> Cancellation:
        signal = Cancellation()
        with self._condition:
            self._jobs[job] = (session_id, signal)
        return signal

    def _touch(self, session_id: str, *, durable: bool = False) -> None:
        self._sequence += 1
        previous = self._versions.get(session_id, (0, 0))[1]
        self._versions[session_id] = (self._sequence, self._sequence if durable else previous)
        self._versions.move_to_end(session_id)
        if len(self._versions) > 256:
            self._versions.popitem(last=False)
        self._condition.notify_all()

    def changed(self, session_id: str) -> None:
        with self._condition:
            self._touch(session_id, durable=True)

    def preview(self, job: str, target: str, text: str) -> None:
        with self._condition:
            entry = self._jobs.get(job)
            if entry and not entry[1].requested and self._drafts.get((job, target), "") != text:
                self._drafts[job, target] = text
                self._touch(entry[0])

    def clear(self, job: str, target: str) -> None:
        with self._condition:
            self._drafts.pop((job, target), None)
            if job in self._jobs:
                self._touch(self._jobs[job][0], durable=True)

    def end(self, job: str) -> None:
        with self._condition:
            entry = self._jobs.pop(job, None)
            for key in tuple(self._drafts):
                if key[0] == job:
                    self._drafts.pop(key)
            if entry:
                self._touch(entry[0], durable=True)

    def cancel(self, job: str) -> None:
        with self._condition:
            entry = self._jobs.get(job)
        if entry:
            entry[1].cancel()
            self.end(job)

    def close(self) -> None:
        with self._condition:
            jobs = tuple(self._jobs)
        for job in jobs:
            self.cancel(job)

    def wait(self, session_id: str, after: int | None, timeout: float = 8) -> dict:
        with self._condition:
            def version():
                return self._versions.get(session_id, (0, 0))

            if after is not None:
                self._condition.wait_for(lambda: version()[0] != after, timeout=timeout)
            sequence, durable = version()
            drafts = {target: text for (job, target), text in self._drafts.items()
                      if self._jobs[job][0] == session_id}
            return {"sequence": sequence, "state_sequence": durable, "drafts": drafts}
