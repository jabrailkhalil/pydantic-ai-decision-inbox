"""Private crash worker used only by the deterministic local lab."""

import argparse
import os
from pathlib import Path

from pydantic_ai import models

from .agent import Fault, crash, resume
from .executor import LedgerExecutor
from .models import Policy
from .store import Inbox


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("action", choices=["decide", "resume"])
    parser.add_argument(
        "fault",
        choices=[
            "after_decision",
            "after_claim",
            "after_dispatch",
            "after_effect",
            "after_receipt",
        ],
    )
    parser.add_argument("mode", choices=["opaque", "idempotent"])
    args = parser.parse_args()
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    models.ALLOW_MODEL_REQUESTS = False
    inbox = Inbox(args.directory / "inbox.sqlite", clock=lambda: 10)
    executor = LedgerExecutor(
        args.directory / "executor.sqlite", idempotent=args.mode == "idempotent"
    )
    fault: Fault = args.fault
    if args.action == "decide":
        request = inbox.get("request-1")
        inbox.decide(
            "request-1",
            principal="alice",
            key="decision-1",
            expected_digest=request.binding.digest(),
            approve=True,
            policy=Policy(),
        )
        crash("after_decision", fault)
    else:
        resume(inbox, executor, "request-1", fault=fault)


if __name__ == "__main__":
    main()
