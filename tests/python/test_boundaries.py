import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier, Event

import pytest
from conftest import Fixture
from pydantic_ai import DeferredToolResults, ToolApproved

from pydantic_ai_decision_inbox import Policy
from pydantic_ai_decision_inbox.agent import (
    Dependencies,
    make_agent,
    pause,
    reconcile,
    resume,
    retry_uncertain,
    saved_history,
)
from pydantic_ai_decision_inbox.executor import LedgerExecutor
from pydantic_ai_decision_inbox.models import (
    Binding,
    BindingMismatch,
    Busy,
    Conflict,
    Expired,
    Receipt,
    UnknownOutcome,
)


@pytest.mark.parametrize(
    "override",
    [{"account": "other", "amount": 5}, {"account": "demo-account", "amount": 6}],
)
def test_sdk_argument_overrides_cannot_inherit_old_approval(
    fixture: Fixture, override: dict
) -> None:
    fixture.approve()
    token = fixture.inbox.claim("request-1", Policy())
    agent = make_agent(fixture.request.binding.args)
    with pytest.raises(BindingMismatch):
        agent.run_sync(
            message_history=saved_history(fixture.request),
            conversation_id=fixture.request.binding.conversation_id,
            deferred_tool_results=DeferredToolResults(
                approvals={"credit-1": ToolApproved(override_args=override)}
            ),
            deps=Dependencies(
                fixture.inbox, fixture.executor, Policy(), "request-1", token
            ),
        )
    assert fixture.executor.effect_count() == 0
    assert fixture.inbox.get("request-1").execution == "claimed"


def test_policy_changed_after_claim_is_rechecked_at_dispatch(fixture: Fixture) -> None:
    fixture.approve()
    token = fixture.inbox.claim("request-1", Policy())
    with pytest.raises(BindingMismatch):
        resume(
            fixture.inbox,
            fixture.executor,
            "request-1",
            policy=Policy(version="v2"),
            claim_token=token,
        )
    assert fixture.executor.effect_count() == 0


def test_request_expired_inside_a_still_valid_claim(fixture: Fixture) -> None:
    fixture.approve()
    fixture.clock.now = fixture.request.binding.expires_at - 5
    token = fixture.inbox.claim("request-1", Policy())
    fixture.clock.now += 5
    with pytest.raises(Expired):
        resume(fixture.inbox, fixture.executor, "request-1", claim_token=token)
    assert fixture.executor.effect_count() == 0


def test_concurrent_identical_decisions_share_one_queued_item(fixture: Fixture) -> None:
    barrier = Barrier(2)

    def decide() -> str:
        barrier.wait(timeout=5)
        return fixture.approve().decision

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(decide) for _ in range(2)]
        assert [future.result(timeout=10) for future in futures] == [
            "approved",
            "approved",
        ]
    with fixture.inbox.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM decision_keys").fetchone()[0] == 1
    resume(fixture.inbox, fixture.executor, "request-1")
    assert fixture.executor.effect_count() == 1


def test_two_claimants_cannot_both_dispatch(fixture: Fixture) -> None:
    fixture.approve()
    barrier = Barrier(2)

    def claim() -> str:
        barrier.wait(timeout=5)
        try:
            return fixture.inbox.claim("request-1", Policy())
        except Busy:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        outcomes = [future.result(timeout=10) for future in futures]
    assert outcomes.count("busy") == 1
    token = next(value for value in outcomes if value != "busy")
    resume(fixture.inbox, fixture.executor, "request-1", claim_token=token)
    assert fixture.executor.effect_count() == 1


@pytest.mark.parametrize("committed", [False, True])
def test_executor_error_is_uncertain_whether_or_not_it_committed(
    fixture: Fixture, committed: bool
) -> None:
    class Failing(LedgerExecutor):
        def execute(self, binding: Binding) -> Receipt:
            if committed:
                super().execute(binding)
            raise OSError("Simulated lost response")

    failing = Failing(fixture.executor.path)
    fixture.approve()
    with pytest.raises(UnknownOutcome):
        resume(fixture.inbox, failing, "request-1")
    assert fixture.inbox.get("request-1").execution == "unknown"
    assert fixture.executor.effect_count() == int(committed)
    with pytest.raises(UnknownOutcome):
        resume(fixture.inbox, fixture.executor, "request-1")
    if committed:
        reconcile(fixture.inbox, fixture.executor, "request-1")
    else:
        with pytest.raises(UnknownOutcome):
            reconcile(fixture.inbox, fixture.executor, "request-1")
        retry_uncertain(fixture.inbox, fixture.executor, "request-1")
    resume(fixture.inbox, fixture.executor, "request-1")
    assert fixture.executor.effect_count() == 1


def test_idempotent_executor_reuses_the_same_receipt(fixture: Fixture) -> None:
    first = fixture.executor.execute(fixture.request.binding)
    second = fixture.executor.execute(fixture.request.binding)
    assert first == second and fixture.executor.effect_count() == 1


def test_delayed_lookup_stays_unknown_until_receipt_is_visible(
    fixture: Fixture,
) -> None:
    visible = Event()

    class DelayedLookup(LedgerExecutor):
        execute_calls = 0
        lookup_calls = 0

        def execute(self, binding: Binding) -> Receipt:
            self.execute_calls += 1
            super().execute(binding)
            raise OSError("Response lost after the provider committed")

        def lookup(self, binding: Binding) -> Receipt | None:
            self.lookup_calls += 1
            if not visible.is_set():
                return None
            return super().lookup(binding)

    executor = DelayedLookup(fixture.executor.path)
    fixture.approve()
    with pytest.raises(UnknownOutcome):
        resume(fixture.inbox, executor, "request-1")
    assert executor.execute_calls == 1
    assert fixture.executor.effect_count() == 1
    committed_receipt = fixture.executor.lookup(fixture.request.binding)
    assert committed_receipt is not None

    # The provider has committed, but its status endpoint cannot see the receipt.
    # An absent lookup must not grant another dispatch or establish an outcome.
    for _ in range(2):
        with pytest.raises(UnknownOutcome):
            reconcile(fixture.inbox, executor, "request-1")
        with pytest.raises(UnknownOutcome):
            resume(fixture.inbox, executor, "request-1")
        request = fixture.inbox.get("request-1")
        assert request.execution == "unknown" and request.receipt is None
        assert executor.execute_calls == 1
        assert fixture.executor.effect_count() == 1
    assert executor.lookup_calls == 2

    visible.set()
    reconciled = reconcile(fixture.inbox, executor, "request-1")
    assert reconciled.execution == "succeeded"
    assert reconciled.receipt == committed_receipt
    completed = resume(fixture.inbox, executor, "request-1")
    assert completed.output == "Tool result received by the agent."
    assert executor.execute_calls == 1 and executor.lookup_calls == 3
    assert fixture.executor.effect_count() == 1


def test_model_text_cannot_establish_a_tool_outcome(fixture: Fixture) -> None:
    fixture.approve()
    with pytest.raises(Conflict):
        fixture.inbox.finish("request-1", "new-run", "Done!")
    assert fixture.inbox.get("request-1").receipt is None


def test_decision_key_cannot_be_reused_for_a_different_request(
    fixture: Fixture,
) -> None:
    fixture.approve()
    second = pause(
        fixture.inbox,
        fixture.executor,
        request_id="request-2",
        conversation_id="conversation-2",
    )
    with pytest.raises(Conflict):
        fixture.inbox.decide(
            "request-2",
            principal="alice",
            key="decision-1",
            expected_digest=second.binding.digest(),
            approve=True,
            policy=Policy(),
        )
    assert fixture.inbox.get("request-2").decision == "pending"


@pytest.mark.parametrize("changed", ["policy", "expiry"])
def test_explicit_retry_rechecks_fresh_execution_authorization(
    fixture: Fixture, changed: str
) -> None:
    fixture.approve()
    token = fixture.inbox.claim("request-1", Policy())
    fixture.inbox.dispatch(
        "request-1", token, fixture.request.binding.args, "credit-1", Policy()
    )
    fixture.inbox.mark_unknown("request-1", token)
    policy = Policy(version="v2") if changed == "policy" else Policy()
    if changed == "expiry":
        fixture.clock.now = fixture.request.binding.expires_at
    with pytest.raises((BindingMismatch, Expired)):
        retry_uncertain(fixture.inbox, fixture.executor, "request-1", policy)
    assert fixture.executor.effect_count() == 0


def test_reconciliation_requires_evidence_in_executor_ledger(fixture: Fixture) -> None:
    receipt = fixture.executor.execute(fixture.request.binding)
    with closing(fixture.executor.connect()) as db:
        db.execute("UPDATE effects SET amount=99 WHERE id=?", (receipt.effect_id,))
    with pytest.raises(BindingMismatch):
        fixture.executor.lookup(fixture.request.binding)


def test_executor_receipt_failure_rolls_back_the_effect(fixture: Fixture) -> None:
    with closing(fixture.executor.connect()) as db:
        db.execute(
            "CREATE TRIGGER reject_receipt BEFORE INSERT ON receipts "
            "BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        fixture.executor.execute(fixture.request.binding)
    assert fixture.executor.effect_count() == 0
    assert fixture.executor.lookup(fixture.request.binding) is None
    with closing(fixture.executor.connect()) as db:
        db.execute("DROP TRIGGER reject_receipt")
    fixture.executor.execute(fixture.request.binding)
    assert fixture.executor.effect_count() == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("tool_name", "different"),
        ("conversation_id", "other-conversation"),
        ("tool_call_id", "other-call"),
    ],
)
def test_binding_digest_covers_each_identity_field(
    fixture: Fixture, field: str, value: str
) -> None:
    original = fixture.request.binding.model_dump(mode="json")
    altered = {**original, field: value}
    # Independent digest over a concrete serialized envelope, not object identity.
    raw = json.dumps(altered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(raw.encode()).hexdigest() != fixture.request.binding.digest()
