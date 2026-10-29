from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import (
    Binding,
    BindingMismatch,
    Busy,
    Conflict,
    CreditArgs,
    Policy,
    Receipt,
    Request,
    UnknownOutcome,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
 binding TEXT NOT NULL, history TEXT NOT NULL,
 history_digest TEXT NOT NULL, pause_run_id TEXT NOT NULL,
 decision TEXT NOT NULL DEFAULT 'pending', reviewer TEXT,
 execution TEXT NOT NULL DEFAULT 'none', token TEXT, lease_until INTEGER,
 receipt TEXT, output TEXT, resume_run_id TEXT,
 UNIQUE(conversation_id,tool_call_id),
 CHECK(decision IN ('pending','approved','denied')),
 CHECK(execution IN ('none','queued','claimed','dispatched',
                     'unknown','succeeded')),
 CHECK(decision='approved' OR execution='none'),
 CHECK(execution!='succeeded' OR receipt IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS decision_keys (
 key TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id),
 principal TEXT NOT NULL, decision TEXT NOT NULL, digest TEXT NOT NULL
);
"""


class Inbox:
    """One SQLite transaction arbitrates decisions and creates the durable outbox.

    The execution columns on requests are the single-item outbox. A claim can
    be reclaimed only before dispatch. Dispatch uncertainty never auto-requeues.
    Database access and the executor adapter are trusted deployment boundaries.
    """

    def __init__(self, path: Path, *, clock: Callable[[], int] | None = None) -> None:
        self.path = path
        self.clock = clock or (lambda: int(time.time()))
        with self.connection() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _read(db: sqlite3.Connection, request_id: str) -> Request:
        row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise KeyError(request_id)
        return Request.model_validate(
            dict(
                binding=Binding.model_validate_json(row["binding"]),
                history_json=row["history"],
                history_digest=row["history_digest"],
                decision=row["decision"],
                reviewer=row["reviewer"],
                execution=row["execution"],
                claim_token=row["token"],
                lease_until=row["lease_until"],
                receipt=Receipt.model_validate_json(row["receipt"])
                if row["receipt"]
                else None,
                output=row["output"],
                pause_run_id=row["pause_run_id"],
                resume_run_id=row["resume_run_id"],
            )
        )

    def get(self, request_id: str) -> Request:
        with self.connection() as db:
            return self._read(db, request_id)

    def add(self, binding: Binding, history_json: str, pause_run_id: str) -> Request:
        if binding.target != binding.args.account:
            raise BindingMismatch("Target and arguments must agree")
        history_digest = hashlib.sha256(history_json.encode()).hexdigest()
        try:
            with self.transaction() as db:
                db.execute(
                    "INSERT INTO requests(id,conversation_id,tool_call_id,binding,"
                    "history,history_digest,pause_run_id) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        binding.request_id,
                        binding.conversation_id,
                        binding.tool_call_id,
                        binding.model_dump_json(),
                        history_json,
                        history_digest,
                        pause_run_id,
                    ),
                )
                return self._read(db, binding.request_id)
        except sqlite3.IntegrityError as exc:
            raise Conflict("Request or pending tool call already exists") from exc

    def decide(
        self,
        request_id: str,
        *,
        principal: str,
        key: str,
        expected_digest: str,
        approve: bool,
        policy: Policy,
    ) -> Request:
        if not key or len(key) > 200 or type(approve) is not bool:
            raise ValueError("A bounded decision key and explicit boolean are required")
        choice = "approved" if approve else "denied"
        with self.transaction() as db:
            request = self._read(db, request_id)
            policy.authorize(principal, request.binding)
            if request.binding.digest() != expected_digest:
                raise BindingMismatch("The reviewed envelope has changed")
            prior = db.execute(
                "SELECT * FROM decision_keys WHERE key=?", (key,)
            ).fetchone()
            if prior:
                if (
                    prior["request_id"],
                    prior["principal"],
                    prior["decision"],
                    prior["digest"],
                ) != (request_id, principal, choice, expected_digest):
                    raise Conflict(
                        "Decision key is already bound to a different operation"
                    )
                return (
                    request  # historical receipt only; never queues another execution
                )
            policy.check_call(request.binding, self.clock())
            if request.decision != "pending":
                raise Conflict("Another decision has already won")
            db.execute(
                "INSERT INTO decision_keys VALUES(?,?,?,?,?)",
                (key, request_id, principal, choice, expected_digest),
            )
            db.execute(
                "UPDATE requests SET decision=?,reviewer=?,execution=? WHERE id=?",
                (choice, principal, "queued" if approve else "none", request_id),
            )
            return self._read(db, request_id)

    @staticmethod
    def _authorize_execution(request: Request, policy: Policy, now: int) -> None:
        if request.decision != "approved" or request.reviewer is None:
            raise Conflict("Execution requires a saved approval")
        policy.authorize(request.reviewer, request.binding)
        policy.check_call(request.binding, now)

    def claim(self, request_id: str, policy: Policy, *, lease_seconds: int = 30) -> str:
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("Lease must be 1–3600 seconds")
        failure: Exception | None = None
        token = uuid.uuid4().hex
        with self.transaction() as db:
            request = self._read(db, request_id)
            now = self.clock()
            if request.execution == "dispatched":
                if request.lease_until is not None and request.lease_until <= now:
                    db.execute(
                        "UPDATE requests SET execution='unknown' WHERE id=?",
                        (request_id,),
                    )
                    failure = UnknownOutcome(
                        "Dispatch may have committed an external effect"
                    )
                else:
                    failure = Busy("A dispatched worker is still within its lease")
            elif (
                request.execution == "claimed"
                and request.lease_until is not None
                and request.lease_until > now
            ):
                failure = Busy("A worker already holds the claim")
            elif request.execution == "unknown":
                failure = UnknownOutcome("Reconcile before any retry")
            elif request.execution not in ("queued", "claimed"):
                failure = Conflict("There is no queued execution to claim")
            if failure is None:
                self._authorize_execution(request, policy, now)
                db.execute(
                    "UPDATE requests SET execution='claimed',token=?,lease_until=? "
                    "WHERE id=?",
                    (token, now + lease_seconds, request_id),
                )
        if failure:
            raise failure
        return token

    def dispatch(
        self,
        request_id: str,
        token: str,
        args: CreditArgs,
        tool_call_id: str,
        policy: Policy,
    ) -> Binding:
        with self.transaction() as db:
            request = self._read(db, request_id)
            now = self.clock()
            self._authorize_execution(request, policy, now)
            if (
                request.binding.args != args
                or request.binding.tool_call_id != tool_call_id
            ):
                raise BindingMismatch(
                    "Resumed effective call differs from the approved call"
                )
            if (
                request.execution != "claimed"
                or request.claim_token != token
                or request.lease_until is None
                or request.lease_until <= now
            ):
                raise Conflict("Stale or expired dispatch claim")
            db.execute(
                "UPDATE requests SET execution='dispatched' WHERE id=?", (request_id,)
            )
            return request.binding

    def mark_unknown(self, request_id: str, token: str) -> Request:
        with self.transaction() as db:
            request = self._read(db, request_id)
            if request.claim_token != token or request.execution != "dispatched":
                raise Conflict("No matching dispatched attempt")
            db.execute(
                "UPDATE requests SET execution='unknown' WHERE id=?", (request_id,)
            )
            return self._read(db, request_id)

    def record_receipt(self, request_id: str, token: str, receipt: Receipt) -> Request:
        with self.transaction() as db:
            request = self._read(db, request_id)
            receipt.check(request.binding)
            if request.receipt is not None:
                if request.receipt != receipt:
                    raise Conflict("An inconsistent executor receipt was supplied")
                return request
            if request.claim_token != token or request.execution not in (
                "dispatched",
                "unknown",
            ):
                raise Conflict("Receipt is not associated with the recorded dispatch")
            db.execute(
                "UPDATE requests SET execution='succeeded',receipt=? WHERE id=?",
                (receipt.model_dump_json(), request_id),
            )
            return self._read(db, request_id)

    def finish(self, request_id: str, run_id: str, output: str) -> Request:
        with self.transaction() as db:
            request = self._read(db, request_id)
            if request.decision != "denied" and request.execution != "succeeded":
                raise Conflict("A model response cannot establish an execution receipt")
            if run_id == request.pause_run_id:
                raise ValueError("A resume must be a new agent run")
            db.execute(
                "UPDATE requests SET output=?,resume_run_id=? "
                "WHERE id=? AND output IS NULL",
                (output, run_id, request_id),
            )
            return self._read(db, request_id)
