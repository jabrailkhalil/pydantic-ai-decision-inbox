"""Executable, model-free scenarios; abrupt child exits are real process deaths."""

from __future__ import annotations

import multiprocessing as mp
import os
import subprocess
import sys
from dataclasses import dataclass
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Barrier
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic_ai import models

from .agent import (
    CRASH_EXIT,
    Fault,
    naive_duplicate_resume,
    pause,
    reconcile,
    resume,
    retry_uncertain,
)
from .executor import LedgerExecutor
from .models import (
    BindingMismatch,
    Conflict,
    CreditArgs,
    Expired,
    Policy,
    UnknownOutcome,
)
from .store import Inbox

SCENARIOS = {
    "happy": "Approval and duplicate delivery",
    "denied": "A denied call has no effect",
    "after_decision": "Crash after the decision commits",
    "after_claim": "Crash before dispatch; reclaim with a new token",
    "after_dispatch": "Dispatch uncertainty before an observed effect",
    "after_effect_opaque": "Effect committed; no trusted reconciliation",
    "after_effect_reconcile": "Recover a committed effect through a trusted receipt",
    "after_receipt": "Receipt committed; agent continuation interrupted",
    "stale_args": "Changed arguments cannot inherit approval",
    "stale_policy": "A changed policy blocks dispatch",
    "expired": "An expired request cannot be approved",
    "reviewer_race": "Two reviewers; one committed decision",
    "negative_control": "Negative control: approval alone repeats the effect",
}


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str
    decision: str
    execution: str
    has_receipt: bool
    agent_finished: bool
    observed_effects: int
    note: str = ""


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    title: str
    mode: Literal["fixture-idempotent", "fixture-opaque"]
    sdk_version: str = "2.63.0"
    steps: list[Step]
    checks: dict[str, bool]
    crash_exit_codes: list[int]
    assumptions: list[str] = [
        "FunctionModel: no provider requests.",
        "Two separate local SQLite databases stand in for the inbox and executor.",
        "Lease time uses a controlled logical clock; injected process exits are real.",
        "Effect counts come from the test observer, not inbox execution receipts.",
        "Executor idempotency and reconciliation are fixture guarantees only.",
    ]


@dataclass
class Clock:
    now: int = 10

    def __call__(self) -> int:
        return self.now


def decision_worker(
    path: str,
    digest: str,
    actor: str,
    approve: bool,
    barrier: Barrier,
    results: Queue[str],
) -> None:
    inbox = Inbox(Path(path), clock=lambda: 10)
    barrier.wait(timeout=15)
    try:
        result = inbox.decide(
            "request-1",
            principal=actor,
            key=actor + "-decision",
            expected_digest=digest,
            approve=approve,
            policy=Policy(),
        )
        results.put(result.decision)
    except Conflict:
        results.put("Conflict")


def race_decisions(inbox: Inbox) -> list[str]:
    context = mp.get_context("spawn")
    barrier = context.Barrier(3)
    results: Queue[str] = context.Queue()
    digest = inbox.get("request-1").binding.digest()
    processes = [
        context.Process(
            target=decision_worker,
            args=(str(inbox.path), digest, actor, approve, barrier, results),
        )
        for actor, approve in (("alice", True), ("bob", False))
    ]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=15)
        outcomes = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            if process.exitcode != 0:
                raise RuntimeError("A reviewer process did not complete successfully")
        return outcomes
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()


def run_scenario(identifier: str, directory: Path) -> Report:
    if identifier not in SCENARIOS:
        raise ValueError("Unknown scenario")
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise FileExistsError("Use a fresh, empty scenario directory")
    models.ALLOW_MODEL_REQUESTS = False
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    clock = Clock()
    opaque = identifier in ("after_effect_opaque", "negative_control")
    inbox = Inbox(directory / "inbox.sqlite", clock=clock)
    executor = LedgerExecutor(directory / "executor.sqlite", idempotent=not opaque)
    request = pause(inbox, executor)
    steps: list[Step] = []
    checks: dict[str, bool] = {}
    exit_codes: list[int] = []

    def capture(title: str, note: str = "") -> None:
        current = inbox.get("request-1")
        steps.append(
            Step(
                title=title,
                decision=current.decision,
                execution=current.execution,
                has_receipt=current.receipt is not None,
                agent_finished=current.output is not None,
                observed_effects=executor.effect_count(),
                note=note,
            )
        )

    def approve() -> None:
        inbox.decide(
            "request-1",
            principal="alice",
            key="decision-1",
            expected_digest=request.binding.digest(),
            approve=True,
            policy=Policy(),
        )

    def kill_worker(action: str, fault: Fault) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pydantic_ai_decision_inbox.worker",
                str(directory),
                action,
                fault,
                "opaque" if opaque else "idempotent",
            ],
            capture_output=True,
            timeout=30,
        )
        exit_codes.append(result.returncode)
        if result.returncode != CRASH_EXIT:
            raise RuntimeError(
                "The injected worker did not reach the requested crash point: "
                + result.stderr.decode(errors="replace")[-2000:]
            )

    capture("The SDK returned a pending approval")
    if identifier == "negative_control":
        count = naive_duplicate_resume(executor)
        capture(
            "The same approval was delivered twice to an unprotected tool",
            "This is expected SDK behavior, not a claim of a framework bug.",
        )
        checks["unprotected_effect_was_repeated"] = count == 2
    elif identifier == "reviewer_race":
        outcomes = race_decisions(inbox)
        capture(
            "Concurrent approve/deny requests finished", ", ".join(sorted(outcomes))
        )
        checks["one_winner_one_conflict"] = outcomes.count("Conflict") == 1
        with inbox.connection() as db:
            checks["one_saved_decision"] = (
                db.execute("SELECT COUNT(*) FROM decision_keys").fetchone()[0] == 1
            )
        checks["approval_is_not_execution"] = executor.effect_count() == 0
    elif identifier == "stale_args":
        changed = request.binding.model_copy(
            update={"args": CreditArgs(account="demo-account", amount=6)}
        )
        try:
            inbox.decide(
                "request-1",
                principal="alice",
                key="changed",
                expected_digest=changed.digest(),
                approve=True,
                policy=Policy(),
            )
        except BindingMismatch:
            capture("The changed argument digest was rejected")
        checks["original_call_still_pending"] = (
            inbox.get("request-1").decision == "pending"
        )
        checks["no_effect"] = executor.effect_count() == 0
    elif identifier == "expired":
        clock.now = request.binding.expires_at
        try:
            approve()
        except Expired:
            capture("The approval attempt was rejected at expiry")
        checks["no_effect"] = executor.effect_count() == 0
        checks["decision_still_pending"] = inbox.get("request-1").decision == "pending"
    elif identifier == "denied":
        inbox.decide(
            "request-1",
            principal="alice",
            key="denial",
            expected_digest=request.binding.digest(),
            approve=False,
            policy=Policy(),
        )
        capture("The denial was saved")
        resume(inbox, executor, "request-1")
        capture("The SDK received the denial")
        checks["no_effect_or_receipt"] = (
            executor.effect_count() == 0 and inbox.get("request-1").receipt is None
        )
    elif identifier == "stale_policy":
        approve()
        capture("The original policy allowed the decision")
        try:
            resume(inbox, executor, "request-1", policy=Policy(version="v2"))
        except BindingMismatch:
            capture("The live policy version blocked execution")
        checks["no_effect"] = executor.effect_count() == 0
    else:
        if identifier == "after_decision":
            kill_worker("decide", "after_decision")
        else:
            approve()
        capture("The decision and queued outbox item are committed")
        if identifier == "happy":
            before = inbox.get("request-1")
            approve()
            checks["same_key_replay_did_not_change_state"] = (
                inbox.get("request-1") == before
            )
            capture("The duplicate decision returned the saved record")
        elif identifier != "after_decision":
            fault: Fault = (
                "after_effect"
                if identifier.startswith("after_effect_")
                else "after_claim"
                if identifier == "after_claim"
                else "after_dispatch"
                if identifier == "after_dispatch"
                else "after_receipt"
            )
            kill_worker("resume", fault)
            capture("The worker exited abruptly", f"Injected exit code: {CRASH_EXIT}")
            clock.now += 31
            if inbox.get("request-1").execution == "dispatched":
                try:
                    resume(inbox, executor, "request-1")
                except UnknownOutcome:
                    capture("Expired dispatched attempt: UNKNOWN; blind retry blocked")
                checks["uncertainty_did_not_repeat_effect"] = (
                    executor.effect_count()
                    == (0 if identifier == "after_dispatch" else 1)
                )
                if identifier == "after_effect_opaque":
                    checks["unknown_stays_without_receipt"] = (
                        inbox.get("request-1").receipt is None
                    )
                    checks["one_observed_effect"] = executor.effect_count() == 1
                elif identifier == "after_effect_reconcile":
                    reconcile(inbox, executor, "request-1")
                    capture("The executor supplied a matching committed receipt")
                else:
                    try:
                        reconcile(inbox, executor, "request-1")
                    except UnknownOutcome:
                        capture(
                            "No receipt found; absence alone cannot authorize retry"
                        )
                    retry_uncertain(inbox, executor, "request-1")
                    capture(
                        "An explicit retry used the fixture's atomic idempotency key"
                    )
        if identifier != "after_effect_opaque":
            result = resume(inbox, executor, "request-1")
            capture("A new SDK run received the recorded tool result")
            before_count = executor.effect_count()
            again = resume(inbox, executor, "request-1")
            capture("Repeated delivery returned the saved completed result")
            checks["one_effect"] = before_count == executor.effect_count() == 1
            checks["same_completed_run_on_replay"] = (
                result.resume_run_id == again.resume_run_id
            )
            checks["new_run_same_conversation"] = (
                result.resume_run_id is not None
                and result.resume_run_id != result.pause_run_id
                and result.binding.conversation_id == request.binding.conversation_id
            )
    if not checks or not all(checks.values()):
        raise AssertionError(f"Scenario assertions failed: {checks}")
    return Report(
        id=identifier,
        title=SCENARIOS[identifier],
        mode="fixture-opaque" if opaque else "fixture-idempotent",
        steps=steps,
        checks=checks,
        crash_exit_codes=exit_codes,
    )
