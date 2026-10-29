import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic_ai import models

from pydantic_ai_decision_inbox import Inbox, Policy
from pydantic_ai_decision_inbox.agent import pause
from pydantic_ai_decision_inbox.executor import LedgerExecutor
from pydantic_ai_decision_inbox.models import Request

os.environ["PYDANTIC_AI_NO_BANNER"] = "1"


@pytest.fixture(autouse=True)
def no_provider_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)


@dataclass
class Clock:
    now: int = 10

    def __call__(self) -> int:
        return self.now


@dataclass
class Fixture:
    inbox: Inbox
    executor: LedgerExecutor
    request: Request
    clock: Clock

    def approve(self, *, key: str = "decision-1", principal: str = "alice") -> Request:
        return self.inbox.decide(
            self.request.binding.request_id,
            principal=principal,
            key=key,
            expected_digest=self.request.binding.digest(),
            approve=True,
            policy=Policy(),
        )


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    clock = Clock()
    inbox = Inbox(tmp_path / "inbox.sqlite", clock=clock)
    executor = LedgerExecutor(tmp_path / "executor.sqlite")
    request = pause(inbox, executor)
    return Fixture(inbox, executor, request, clock)
