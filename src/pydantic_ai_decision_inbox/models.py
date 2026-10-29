from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class CreditArgs(Frozen):
    account: str = Field(min_length=1, max_length=100)
    amount: int = Field(gt=0, le=100)


class Binding(Frozen):
    request_id: str = Field(min_length=1, max_length=100)
    conversation_id: str = Field(min_length=1, max_length=100)
    tool_call_id: str = Field(min_length=1, max_length=100)
    tool_name: Literal["credit_account"] = "credit_account"
    requester: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=100)
    args: CreditArgs
    policy_version: str = Field(min_length=1, max_length=100)
    expires_at: int = Field(ge=0)

    def digest(self) -> str:
        raw = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def operation_key(self) -> str:
        return "inbox:" + self.digest()


class Receipt(Frozen):
    operation_key: str
    binding_digest: str
    effect_id: int = Field(gt=0)
    account: str
    amount: int

    def check(self, binding: Binding) -> None:
        if (
            self.operation_key != binding.operation_key()
            or self.binding_digest != binding.digest()
            or self.account != binding.args.account
            or self.amount != binding.args.amount
        ):
            raise BindingMismatch("Executor receipt does not match the effective call")


Decision = Literal["pending", "approved", "denied"]
Execution = Literal["none", "queued", "claimed", "dispatched", "unknown", "succeeded"]


class Request(Frozen):
    binding: Binding
    history_json: str
    history_digest: str
    decision: Decision
    reviewer: str | None
    execution: Execution
    claim_token: str | None
    lease_until: int | None
    receipt: Receipt | None
    output: str | None
    pause_run_id: str
    resume_run_id: str | None


class InboxError(Exception):
    """A rejected operation; no permission to repeat an external effect."""


class BindingMismatch(InboxError):
    pass


class NotAuthorized(InboxError):
    pass


class Conflict(InboxError):
    pass


class Expired(InboxError):
    pass


class Busy(InboxError):
    pass


class UnknownOutcome(InboxError):
    pass


class ReconciliationUnavailable(InboxError):
    pass


@dataclass(frozen=True)
class Policy:
    version: str = "v1"
    reviewers: frozenset[str] = frozenset({"alice", "bob"})
    requesters: frozenset[str] = frozenset({"requester"})
    targets: frozenset[str] = frozenset({"demo-account"})

    def authorize(self, principal: str, binding: Binding) -> None:
        # The caller must obtain principal from its authenticated transport.
        # This allowlist is application policy, not an authentication service.
        if (
            principal not in self.reviewers
            or binding.requester not in self.requesters
            or binding.target not in self.targets
        ):
            raise NotAuthorized("Principal, requester or target is not allowed")

    def check_call(self, binding: Binding, now: int) -> None:
        if binding.policy_version != self.version:
            raise BindingMismatch("The live policy version has changed")
        if binding.target != binding.args.account:
            raise BindingMismatch("Target and effective arguments disagree")
        if binding.expires_at <= now:
            raise Expired("The request has expired")
