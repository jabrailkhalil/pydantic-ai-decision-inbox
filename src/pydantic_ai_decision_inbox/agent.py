from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Literal

from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    ToolApproved,
    ToolDenied,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionDef, FunctionModel

from .executor import Executor, LedgerExecutor
from .models import (
    Binding,
    BindingMismatch,
    Conflict,
    CreditArgs,
    Policy,
    Request,
    UnknownOutcome,
)
from .store import Inbox

Fault = Literal[
    "none",
    "after_decision",
    "after_claim",
    "after_dispatch",
    "after_effect",
    "after_receipt",
]
CRASH_EXIT = 73
DEFAULT_POLICY = Policy()


def crash(point: Fault, configured: Fault) -> None:
    if configured == point:
        os._exit(CRASH_EXIT)  # intentional abrupt process death; no finally/cleanup


@dataclass
class Dependencies:
    inbox: Inbox
    executor: Executor
    policy: Policy
    request_id: str
    claim_token: str | None = None
    fault: Fault = "none"


def fixture_model(args: CreditArgs) -> FunctionDef:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(
                            parts=[TextPart("Tool result received by the agent.")]
                        )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "credit_account",
                    args.model_dump(mode="json"),
                    tool_call_id="credit-1",
                )
            ]
        )

    return respond


def make_agent(args: CreditArgs) -> Agent[Dependencies, str | DeferredToolRequests]:
    agent: Agent[Dependencies, str | DeferredToolRequests] = Agent(
        FunctionModel(fixture_model(args)),
        deps_type=Dependencies,
        output_type=[str, DeferredToolRequests],
        retries=0,
    )

    @agent.tool(requires_approval=True)
    def credit_account(ctx: RunContext[Dependencies], account: str, amount: int) -> str:
        effective = CreditArgs(account=account, amount=amount)
        deps = ctx.deps
        request = deps.inbox.get(deps.request_id)
        if (
            request.binding.args != effective
            or request.binding.tool_call_id != ctx.tool_call_id
            or request.binding.conversation_id != ctx.conversation_id
        ):
            raise BindingMismatch("Resumed tool identity or arguments changed")
        if request.execution == "succeeded" and request.receipt is not None:
            request.receipt.check(request.binding)
            return (
                request.receipt.model_dump_json()
            )  # cached evidence; no external dispatch
        if deps.claim_token is None or ctx.tool_call_id is None:
            raise Conflict("No dispatch claim was provided")
        binding = deps.inbox.dispatch(
            deps.request_id, deps.claim_token, effective, ctx.tool_call_id, deps.policy
        )
        crash("after_dispatch", deps.fault)
        try:
            receipt = deps.executor.execute(binding)
        except Exception as exc:
            deps.inbox.mark_unknown(deps.request_id, deps.claim_token)
            raise UnknownOutcome(
                "Executor failed after dispatch; reconcile its outcome"
            ) from exc
        crash("after_effect", deps.fault)
        deps.inbox.record_receipt(deps.request_id, deps.claim_token, receipt)
        crash("after_receipt", deps.fault)
        return receipt.model_dump_json()

    return agent


def pause(
    inbox: Inbox,
    executor: Executor,
    *,
    request_id: str = "request-1",
    args: CreditArgs | None = None,
    policy: Policy = DEFAULT_POLICY,
    conversation_id: str = "conversation-1",
    expires_at: int | None = None,
) -> Request:
    args = args or CreditArgs(account="demo-account", amount=5)
    agent = make_agent(args)
    result = agent.run_sync(
        "Credit the fixture account after approval.",
        conversation_id=conversation_id,
        deps=Dependencies(inbox, executor, policy, request_id),
    )
    if (
        not isinstance(result.output, DeferredToolRequests)
        or len(result.output.approvals) != 1
        or result.output.calls
    ):
        raise ValueError("This prototype supports exactly one pending approval")
    call = result.output.approvals[0]
    if call.tool_name != "credit_account":
        raise BindingMismatch("Unexpected tool name")
    effective = CreditArgs.model_validate(call.args_as_dict())
    binding = Binding(
        request_id=request_id,
        conversation_id=result.conversation_id,
        tool_call_id=call.tool_call_id,
        requester="requester",
        target=effective.account,
        args=effective,
        policy_version=policy.version,
        expires_at=expires_at if expires_at is not None else inbox.clock() + 300,
    )
    return inbox.add(binding, result.all_messages_json().decode(), result.run_id)


def saved_history(request: Request) -> list[ModelMessage]:
    if (
        hashlib.sha256(request.history_json.encode()).hexdigest()
        != request.history_digest
    ):
        raise BindingMismatch("Saved message history changed")
    messages = ModelMessagesTypeAdapter.validate_json(request.history_json)
    calls = [
        part
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
        and part.tool_call_id == request.binding.tool_call_id
    ]
    if (
        len(calls) != 1
        or calls[0].tool_name != request.binding.tool_name
        or CreditArgs.model_validate(calls[0].args_as_dict()) != request.binding.args
    ):
        raise BindingMismatch("Saved pending call differs from the approved envelope")
    for message in messages:
        if (
            message.conversation_id is not None
            and message.conversation_id != request.binding.conversation_id
        ):
            raise BindingMismatch("Saved conversation identity changed")
        if isinstance(message, ModelRequest) and any(
            isinstance(part, ToolReturnPart)
            and part.tool_call_id == request.binding.tool_call_id
            for part in message.parts
        ):
            raise BindingMismatch("The stored call already has a result")
    return messages


def resume(
    inbox: Inbox,
    executor: Executor,
    request_id: str,
    *,
    policy: Policy = DEFAULT_POLICY,
    fault: Fault = "none",
    claim_token: str | None = None,
) -> Request:
    request = inbox.get(request_id)
    if request.output is not None:
        return request
    messages = saved_history(request)
    if request.decision == "pending":
        raise Conflict("A decision is still required")
    token = claim_token
    if request.decision == "approved" and request.execution != "succeeded":
        token = token or inbox.claim(request_id, policy)
        crash("after_claim", fault)
    results = DeferredToolResults(
        approvals={
            request.binding.tool_call_id: ToolApproved()
            if request.decision == "approved"
            else ToolDenied("The reviewer denied this call.")
        }
    )
    agent = make_agent(request.binding.args)
    result = agent.run_sync(
        message_history=messages,
        deferred_tool_results=results,
        conversation_id=request.binding.conversation_id,
        deps=Dependencies(inbox, executor, policy, request_id, token, fault),
    )
    if not isinstance(result.output, str):
        raise Conflict("Unexpected additional deferred calls")
    return inbox.finish(request_id, result.run_id, result.output)


def reconcile(inbox: Inbox, executor: Executor, request_id: str) -> Request:
    request = inbox.get(request_id)
    if request.execution != "unknown" or request.claim_token is None:
        raise Conflict("Reconciliation requires an uncertain recorded dispatch")
    receipt = executor.lookup(request.binding)
    if receipt is None:
        raise UnknownOutcome("No trusted receipt was found; retry remains blocked")
    receipt.check(request.binding)
    return inbox.record_receipt(request_id, request.claim_token, receipt)


def retry_uncertain(
    inbox: Inbox, executor: Executor, request_id: str, policy: Policy = DEFAULT_POLICY
) -> Request:
    request = inbox.get(request_id)
    if (
        request.execution != "unknown"
        or request.claim_token is None
        or request.reviewer is None
    ):
        raise Conflict(
            "Explicit idempotent retry requires an uncertain approved dispatch"
        )
    policy.authorize(request.reviewer, request.binding)
    policy.check_call(request.binding, inbox.clock())
    receipt = executor.retry_idempotent(request.binding)
    receipt.check(request.binding)
    return inbox.record_receipt(request_id, request.claim_token, receipt)


def naive_duplicate_resume(executor: LedgerExecutor) -> int:
    """Negative control: SDK approval alone does not deduplicate tool effects."""
    args = CreditArgs(account="demo-account", amount=5)
    agent = Agent(
        FunctionModel(fixture_model(args)), output_type=[str, DeferredToolRequests]
    )
    binding = Binding(
        request_id="naive",
        conversation_id="naive-conversation",
        tool_call_id="credit-1",
        requester="requester",
        target=args.account,
        args=args,
        policy_version="v1",
        expires_at=1000,
    )

    @agent.tool_plain(requires_approval=True)
    def credit_account(account: str, amount: int) -> str:
        return executor.execute(binding).model_dump_json()

    paused = agent.run_sync(
        "Credit the fixture account", conversation_id=binding.conversation_id
    )
    if not isinstance(paused.output, DeferredToolRequests):
        raise RuntimeError("Expected a pending approval")
    for _ in range(2):
        agent.run_sync(
            message_history=paused.all_messages(),
            conversation_id=binding.conversation_id,
            deferred_tool_results=DeferredToolResults(
                approvals={"credit-1": ToolApproved()}
            ),
        )
    return executor.effect_count()
