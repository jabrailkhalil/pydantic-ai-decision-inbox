from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Protocol

from .models import Binding, BindingMismatch, Receipt, ReconciliationUnavailable


class Executor(Protocol):
    """Deployment-owned adapter. Its receipts and lookup must be trustworthy."""

    def execute(self, binding: Binding) -> Receipt: ...
    def lookup(self, binding: Binding) -> Receipt | None: ...
    def retry_idempotent(self, binding: Binding) -> Receipt: ...


class LedgerExecutor:
    """Separate SQLite database standing in for a remote side-effect service.

    In idempotent mode the effect and operation receipt commit atomically under
    a unique operation key. This guarantee belongs to this fixture, not SQLite
    approval storage or arbitrary external providers.
    """

    def __init__(self, path: Path, *, idempotent: bool = True) -> None:
        self.path = path
        self.idempotent = idempotent
        with closing(self.connect()) as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS effects (
                id INTEGER PRIMARY KEY, operation_key TEXT NOT NULL,
                digest TEXT NOT NULL, account TEXT NOT NULL, amount INTEGER NOT NULL
              );
              CREATE TABLE IF NOT EXISTS receipts (
                operation_key TEXT PRIMARY KEY, receipt TEXT NOT NULL
              );
            """)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def execute(self, binding: Binding) -> Receipt:
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if self.idempotent:
                    row = db.execute(
                        "SELECT receipt FROM receipts WHERE operation_key=?",
                        (binding.operation_key(),),
                    ).fetchone()
                    if row:
                        receipt = Receipt.model_validate_json(row[0])
                        receipt.check(binding)
                        db.commit()
                        return receipt
                cursor = db.execute(
                    "INSERT INTO effects(operation_key,digest,account,amount) "
                    "VALUES (?,?,?,?)",
                    (
                        binding.operation_key(),
                        binding.digest(),
                        binding.args.account,
                        binding.args.amount,
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("The executor did not return an effect ID")
                receipt = Receipt(
                    operation_key=binding.operation_key(),
                    binding_digest=binding.digest(),
                    effect_id=cursor.lastrowid,
                    account=binding.args.account,
                    amount=binding.args.amount,
                )
                if self.idempotent:
                    db.execute(
                        "INSERT INTO receipts VALUES (?,?)",
                        (binding.operation_key(), receipt.model_dump_json()),
                    )
                db.commit()
                return receipt
            except BaseException:
                db.rollback()
                raise

    def lookup(self, binding: Binding) -> Receipt | None:
        if not self.idempotent:
            raise ReconciliationUnavailable("This fixture exposes no trusted lookup")
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT receipt FROM receipts WHERE operation_key=?",
                (binding.operation_key(),),
            ).fetchone()
            if row is None:
                return None
            receipt = Receipt.model_validate_json(row[0])
            receipt.check(binding)
            effect = db.execute(
                "SELECT operation_key,digest,account,amount FROM effects WHERE id=?",
                (receipt.effect_id,),
            ).fetchone()
            if effect != (
                binding.operation_key(),
                binding.digest(),
                binding.args.account,
                binding.args.amount,
            ):
                raise BindingMismatch("Executor ledger does not support the receipt")
            return receipt

    def effect_count(self) -> int:
        """Test-observer view, deliberately outside the inbox's trusted API."""
        with closing(self.connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM effects").fetchone()[0])

    def retry_idempotent(self, binding: Binding) -> Receipt:
        if not self.idempotent:
            raise ReconciliationUnavailable(
                "This executor cannot guarantee idempotent retry"
            )
        return self.execute(binding)
