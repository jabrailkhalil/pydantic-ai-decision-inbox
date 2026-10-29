"""Reproduce the unsafe control and exercise one guarded SDK resume."""

import tempfile
from pathlib import Path

from pydantic_ai import models

from pydantic_ai_decision_inbox import Inbox, Policy
from pydantic_ai_decision_inbox.agent import naive_duplicate_resume, pause, resume
from pydantic_ai_decision_inbox.executor import LedgerExecutor

models.ALLOW_MODEL_REQUESTS = False
root = Path(__file__).resolve().parents[1] / "demo-work"
root.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(dir=root) as directory:
    work = Path(directory).resolve()
    assert work.is_relative_to(root.resolve())
    unsafe = LedgerExecutor(work / "unsafe.sqlite", idempotent=False)
    assert naive_duplicate_resume(unsafe) == 2
    print("Negative control: reusing the same approval/history produced 2 effects.")
    inbox = Inbox(work / "inbox.sqlite", clock=lambda: 10)
    executor = LedgerExecutor(work / "executor.sqlite")
    request = pause(inbox, executor)
    assert request.decision == "pending" and executor.effect_count() == 0
    inbox.decide(
        request.binding.request_id,
        principal="alice",
        key="decision-1",
        expected_digest=request.binding.digest(),
        approve=True,
        policy=Policy(),
    )
    result = resume(inbox, executor, request.binding.request_id)
    assert result.execution == "succeeded" and result.output is not None
    assert result.pause_run_id != result.resume_run_id
    again = resume(inbox, executor, request.binding.request_id)
    assert again.resume_run_id == result.resume_run_id and executor.effect_count() == 1
    print("Guarded resume: 1 effect; repeated delivery returned the saved result.")
