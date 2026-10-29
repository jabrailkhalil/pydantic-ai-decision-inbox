from __future__ import annotations

import multiprocessing as mp
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event
from pathlib import Path

import pytest
from conftest import Fixture

from pydantic_ai_decision_inbox import Inbox
from pydantic_ai_decision_inbox.agent import reconcile, resume, retry_uncertain
from pydantic_ai_decision_inbox.executor import LedgerExecutor
from pydantic_ai_decision_inbox.models import Binding, Receipt, UnknownOutcome


def delayed_worker(
    inbox_path: str,
    executor_path: str,
    ready: Event,
    release: Event,
    results: Queue[str],
) -> None:
    class Delayed(LedgerExecutor):
        def execute(self, binding: Binding) -> Receipt:
            ready.set()
            if not release.wait(timeout=15):
                raise RuntimeError("Worker gate timed out")
            return super().execute(binding)

    inbox = Inbox(Path(inbox_path), clock=lambda: 10)
    executor = Delayed(Path(executor_path))
    result = resume(inbox, executor, "request-1")
    results.put(result.execution)


@pytest.mark.subprocess(
    reason="A live worker is held after dispatch across lease expiry"
)
@pytest.mark.parametrize("explicit_retry", [False, True])
def test_late_worker_cannot_duplicate_an_uncertain_effect(
    fixture: Fixture, explicit_retry: bool
) -> None:
    fixture.approve()
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    results: Queue[str] = ctx.Queue()
    process = ctx.Process(
        target=delayed_worker,
        args=(
            str(fixture.inbox.path),
            str(fixture.executor.path),
            ready,
            release,
            results,
        ),
    )
    process.start()
    try:
        assert ready.wait(timeout=15)
        fixture.clock.now += 31
        with pytest.raises(UnknownOutcome):
            resume(fixture.inbox, fixture.executor, "request-1")
        with pytest.raises(UnknownOutcome):
            reconcile(fixture.inbox, fixture.executor, "request-1")
        assert fixture.executor.effect_count() == 0
        if explicit_retry:
            retry_uncertain(fixture.inbox, fixture.executor, "request-1")
            assert fixture.executor.effect_count() == 1
            resume(fixture.inbox, fixture.executor, "request-1")
        release.set()
        assert results.get(timeout=15) == "succeeded"
        process.join(timeout=15)
        assert process.exitcode == 0
        assert fixture.executor.effect_count() == 1
        assert fixture.inbox.get("request-1").execution == "succeeded"
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        results.close()
        results.join_thread()
