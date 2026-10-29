import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import Fixture
from pydantic import ValidationError

from pydantic_ai_decision_inbox import CreditArgs, Policy
from pydantic_ai_decision_inbox.agent import (
    naive_duplicate_resume,
    reconcile,
    resume,
    retry_uncertain,
)
from pydantic_ai_decision_inbox.executor import LedgerExecutor
from pydantic_ai_decision_inbox.models import (
    BindingMismatch,
    Busy,
    Conflict,
    Expired,
    NotAuthorized,
    ReconciliationUnavailable,
    UnknownOutcome,
)


def test_sdk_pause_and_guarded_resume_are_separate_runs(fixture: Fixture) -> None:
    assert fixture.request.decision == "pending"
    assert fixture.request.execution == "none" and fixture.executor.effect_count() == 0
    fixture.approve()
    assert fixture.inbox.get("request-1").execution == "queued"
    result = resume(fixture.inbox, fixture.executor, "request-1")
    assert result.execution == "succeeded" and result.receipt is not None
    assert result.output == "Tool result received by the agent."
    assert result.resume_run_id and result.resume_run_id != result.pause_run_id
    assert result.binding.conversation_id == fixture.request.binding.conversation_id
    assert fixture.executor.effect_count() == 1
    assert resume(fixture.inbox, fixture.executor, "request-1") == result
    assert fixture.executor.effect_count() == 1


def test_negative_control_same_approval_reenters_unprotected_tool(
    tmp_path: Path,
) -> None:
    executor = LedgerExecutor(tmp_path / "unsafe.sqlite", idempotent=False)
    assert naive_duplicate_resume(executor) == 2


def test_denial_resumes_without_a_tool_effect(fixture: Fixture) -> None:
    fixture.inbox.decide(
        "request-1",
        principal="alice",
        key="deny",
        expected_digest=fixture.request.binding.digest(),
        approve=False,
        policy=Policy(),
    )
    result = resume(fixture.inbox, fixture.executor, "request-1")
    assert result.decision == "denied" and result.execution == "none"
    assert result.receipt is None and result.output is not None
    assert fixture.executor.effect_count() == 0


def test_pending_request_cannot_execute(fixture: Fixture) -> None:
    with pytest.raises(Conflict):
        resume(fixture.inbox, fixture.executor, "request-1")
    assert fixture.executor.effect_count() == 0


def test_same_decision_key_is_readonly_replay(fixture: Fixture) -> None:
    original = fixture.approve()
    assert fixture.approve() == original
    with fixture.inbox.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM decision_keys").fetchone()[0] == 1
    assert fixture.executor.effect_count() == 0


@pytest.mark.parametrize(
    "key,principal,approve",
    [
        ("decision-1", "alice", False),
        ("decision-1", "bob", True),
        ("different", "alice", True),
        ("different", "bob", False),
    ],
)
def test_conflicting_decisions_do_not_change_the_winner(
    fixture: Fixture,
    key: str,
    principal: str,
    approve: bool,
) -> None:
    original = fixture.approve()
    with pytest.raises(Conflict):
        fixture.inbox.decide(
            "request-1",
            principal=principal,
            key=key,
            expected_digest=fixture.request.binding.digest(),
            approve=approve,
            policy=Policy(),
        )
    assert fixture.inbox.get("request-1") == original


@pytest.mark.parametrize("principal", ["mallory", "", "ALICE"])
def test_unauthorized_principal_rejected(fixture: Fixture, principal: str) -> None:
    with pytest.raises(NotAuthorized):
        fixture.approve(principal=principal)
    assert fixture.inbox.get("request-1").decision == "pending"


def test_changed_digest_does_not_inherit_approval(fixture: Fixture) -> None:
    changed = fixture.request.binding.model_copy(
        update={"args": CreditArgs(account="demo-account", amount=6)}
    )
    with pytest.raises(BindingMismatch):
        fixture.inbox.decide(
            "request-1",
            principal="alice",
            key="key",
            expected_digest=changed.digest(),
            approve=True,
            policy=Policy(),
        )
    assert fixture.inbox.get("request-1").decision == "pending"


def test_changed_policy_rejected_before_decision(fixture: Fixture) -> None:
    with pytest.raises(BindingMismatch):
        fixture.inbox.decide(
            "request-1",
            principal="alice",
            key="key",
            expected_digest=fixture.request.binding.digest(),
            approve=True,
            policy=Policy(version="v2"),
        )


@pytest.mark.parametrize(
    "change", ["policy", "reviewer", "requester", "target", "expiry"]
)
def test_authorization_rechecked_at_execution(fixture: Fixture, change: str) -> None:
    fixture.approve()
    policy = Policy()
    if change == "policy":
        policy = replace(policy, version="v2")
    if change == "reviewer":
        policy = replace(policy, reviewers=frozenset({"bob"}))
    if change == "requester":
        policy = replace(policy, requesters=frozenset())
    if change == "target":
        policy = replace(policy, targets=frozenset())
    if change == "expiry":
        fixture.clock.now = fixture.request.binding.expires_at
    with pytest.raises((BindingMismatch, NotAuthorized, Expired)):
        resume(fixture.inbox, fixture.executor, "request-1", policy=policy)
    assert fixture.executor.effect_count() == 0


def test_expiry_and_historical_replay_are_not_new_authorization(
    fixture: Fixture,
) -> None:
    fixture.approve()
    fixture.clock.now = fixture.request.binding.expires_at
    assert fixture.approve().decision == "approved"  # read-only historical decision
    with pytest.raises(Expired):
        fixture.inbox.claim("request-1", Policy())
    assert fixture.executor.effect_count() == 0


def test_expired_pending_request_rejected(fixture: Fixture) -> None:
    fixture.clock.now = fixture.request.binding.expires_at
    with pytest.raises(Expired):
        fixture.approve()
    assert fixture.inbox.get("request-1").decision == "pending"


def test_two_request_ids_cannot_duplicate_one_pending_call(fixture: Fixture) -> None:
    binding = fixture.request.binding.model_copy(update={"request_id": "second"})
    with pytest.raises(Conflict):
        fixture.inbox.add(binding, fixture.request.history_json, "another-run")


def test_lease_before_dispatch_can_be_reclaimed_and_old_worker_is_fenced(
    fixture: Fixture,
) -> None:
    fixture.approve()
    old = fixture.inbox.claim("request-1", Policy())
    with pytest.raises(Busy):
        fixture.inbox.claim("request-1", Policy())
    fixture.clock.now += 30
    new = fixture.inbox.claim("request-1", Policy())
    assert old != new
    with pytest.raises(Conflict):
        resume(fixture.inbox, fixture.executor, "request-1", claim_token=old)
    assert fixture.executor.effect_count() == 0
    resume(fixture.inbox, fixture.executor, "request-1", claim_token=new)
    assert fixture.executor.effect_count() == 1


def dispatch(fixture: Fixture) -> str:
    fixture.approve()
    token = fixture.inbox.claim("request-1", Policy())
    fixture.inbox.dispatch(
        "request-1",
        token,
        fixture.request.binding.args,
        fixture.request.binding.tool_call_id,
        Policy(),
    )
    return token


def test_dispatch_without_receipt_becomes_unknown_even_after_expiry(
    fixture: Fixture,
) -> None:
    dispatch(fixture)
    fixture.clock.now = fixture.request.binding.expires_at + 1
    with pytest.raises(UnknownOutcome):
        fixture.inbox.claim("request-1", Policy())
    assert fixture.inbox.get("request-1").execution == "unknown"
    assert fixture.executor.effect_count() == 0
    with pytest.raises(UnknownOutcome):
        reconcile(fixture.inbox, fixture.executor, "request-1")


def test_trusted_reconciliation_records_existing_effect_without_resending(
    fixture: Fixture,
) -> None:
    dispatch(fixture)
    receipt = fixture.executor.execute(fixture.request.binding)
    fixture.clock.now += 31
    with pytest.raises(UnknownOutcome):
        resume(fixture.inbox, fixture.executor, "request-1")
    result = reconcile(fixture.inbox, fixture.executor, "request-1")
    assert result.receipt == receipt and result.output is None
    assert resume(fixture.inbox, fixture.executor, "request-1").output is not None
    assert fixture.executor.effect_count() == 1


def test_explicit_idempotent_retry_after_no_receipt(fixture: Fixture) -> None:
    dispatch(fixture)
    fixture.clock.now += 31
    with pytest.raises(UnknownOutcome):
        fixture.inbox.claim("request-1", Policy())
    retry_uncertain(fixture.inbox, fixture.executor, "request-1")
    assert fixture.executor.effect_count() == 1
    assert fixture.inbox.get("request-1").receipt is not None


def test_nonidempotent_executor_does_not_authorize_uncertain_retry(
    fixture: Fixture,
) -> None:
    dispatch(fixture)
    fixture.clock.now += 31
    with pytest.raises(UnknownOutcome):
        fixture.inbox.claim("request-1", Policy())
    unsafe = LedgerExecutor(fixture.executor.path, idempotent=False)
    with pytest.raises(ReconciliationUnavailable):
        retry_uncertain(fixture.inbox, unsafe, "request-1")
    with pytest.raises(ReconciliationUnavailable):
        reconcile(fixture.inbox, unsafe, "request-1")
    assert fixture.executor.effect_count() == 0


def test_receipt_is_bound_to_effective_call(fixture: Fixture) -> None:
    token = dispatch(fixture)
    receipt = fixture.executor.execute(fixture.request.binding)
    wrong = receipt.model_copy(update={"amount": 99})
    with pytest.raises(BindingMismatch):
        fixture.inbox.record_receipt("request-1", token, wrong)
    fixture.inbox.record_receipt("request-1", token, receipt)
    assert fixture.inbox.record_receipt("request-1", token, receipt).receipt == receipt
    with pytest.raises(Conflict):
        fixture.inbox.record_receipt(
            "request-1", token, receipt.model_copy(update={"effect_id": 999})
        )


def test_changed_saved_history_rejected_before_effect(fixture: Fixture) -> None:
    fixture.approve()
    with fixture.inbox.transaction() as db:
        db.execute("UPDATE requests SET history=history||' ' WHERE id='request-1'")
    with pytest.raises(BindingMismatch):
        resume(fixture.inbox, fixture.executor, "request-1")
    assert fixture.executor.effect_count() == 0


def test_changed_arguments_in_history_do_not_inherit_approval(fixture: Fixture) -> None:
    fixture.approve()
    history = fixture.request.history_json.replace('"amount":5', '"amount":6')
    assert history != fixture.request.history_json
    with fixture.inbox.transaction() as db:
        db.execute(
            "UPDATE requests SET history=?,history_digest=? WHERE id='request-1'",
            (history, hashlib.sha256(history.encode()).hexdigest()),
        )
    with pytest.raises(BindingMismatch):
        resume(fixture.inbox, fixture.executor, "request-1")
    assert fixture.executor.effect_count() == 0


@pytest.mark.parametrize("amount", [0, -1, 101, "5", True, 1.5])
def test_invalid_tool_arguments(amount: object) -> None:
    with pytest.raises(ValidationError):
        CreditArgs(account="demo-account", amount=amount)


@pytest.mark.parametrize("key,approve", [("", True), ("x" * 201, True), ("key", 1)])
def test_invalid_decision_input(fixture: Fixture, key: str, approve: bool) -> None:
    with pytest.raises(ValueError):
        fixture.inbox.decide(
            "request-1",
            principal="alice",
            key=key,
            expected_digest=fixture.request.binding.digest(),
            approve=approve,
            policy=Policy(),
        )
