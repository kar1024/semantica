"""SQLite proposal lifecycle and immutable worker handoffs."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise RuntimeError(
                f"immutable handoff already exists with different content: {path}"
            )
        return
    staging_path = path.parent.parent / f".{path.parent.name}-staging"
    staging_path.mkdir(parents=True, exist_ok=True)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=staging_path, prefix=f".{path.name}.", delete=False
        ) as handle:
            temp_name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise RuntimeError(
                    f"immutable handoff already exists with different content: {path}"
                )
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


class ProposalTransitionError(RuntimeError):
    pass


class AuthoringStore:
    def __init__(self, sqlite_path: Path, receipts_path: Path) -> None:
        self.sqlite_path = sqlite_path
        self.receipts_path = receipts_path
        self._lock = threading.RLock()
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.receipts_path.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.sqlite_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    ontology_iri TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    entity_uri TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    base_revision TEXT NOT NULL,
                    base_semantic_hash TEXT NOT NULL,
                    target_payload_hash TEXT NOT NULL,
                    target_semantic_hash TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reviewer TEXT,
                    before_json TEXT,
                    after_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    term_diffs_json TEXT NOT NULL,
                    consumer_impacts_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    handoff_id TEXT,
                    receipt_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS proposals_document_state
                    ON proposals(document_id, state, created_at);
                CREATE TABLE IF NOT EXISTS versions (
                    proposal_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    target_semantic_hash TEXT NOT NULL,
                    commit_sha TEXT,
                    pushed INTEGER,
                    published_at TEXT NOT NULL,
                    receipt_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    collection TEXT NOT NULL
                        CHECK (collection IN ('vocabulary', 'property')),
                    item_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    keep INTEGER CHECK (keep IS NULL OR keep IN (0, 1)),
                    annotation TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (collection, item_id),
                    CHECK (collection != 'property' OR keep IS NULL)
                );
                """)

    def review(self, collection: str, item_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reviews WHERE collection=? AND item_id=?",
                (collection, item_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def reviews(self, collection: str) -> dict[str, dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reviews WHERE collection=? ORDER BY item_id",
                (collection,),
            ).fetchall()
        return {row["item_id"]: dict(row) for row in rows}

    def save_review(
        self,
        *,
        collection: str,
        item_id: str,
        source_revision: str,
        keep: Optional[bool],
        annotation: str,
        actor: str,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        keep_value = None if keep is None else int(keep)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reviews (
                    collection, item_id, source_revision, keep, annotation,
                    actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(collection, item_id) DO UPDATE SET
                    source_revision=excluded.source_revision,
                    keep=excluded.keep,
                    annotation=excluded.annotation,
                    actor=excluded.actor,
                    updated_at=excluded.updated_at
                """,
                (
                    collection,
                    item_id,
                    source_revision,
                    keep_value,
                    annotation,
                    actor,
                    timestamp,
                    timestamp,
                ),
            )
        row = self.review(collection, item_id)
        if row is None:
            raise RuntimeError(f"review was not persisted: {collection}/{item_id}")
        return row

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        columns = tuple(record)
        placeholders = ",".join("?" for _ in columns)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO proposals ({','.join(columns)}) VALUES ({placeholders})",
                tuple(record[column] for column in columns),
            )
        return self.get(record["proposal_id"])

    def _reconcile_receipt(self, row: sqlite3.Row) -> None:
        with self._lock:
            with self._connect() as connection:
                current = connection.execute(
                    "SELECT * FROM proposals WHERE proposal_id=?", (row["proposal_id"],)
                ).fetchone()
            if current is None:
                raise KeyError(row["proposal_id"])
            row = current
            receipt_path = self.receipts_path / f"{row['proposal_id']}.json"
            if not receipt_path.exists():
                return
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"invalid receipt {receipt_path}: {exc}") from exc
            required = {
                "schema_version",
                "proposal_id",
                "state",
                "pushed",
                "completed_at",
            }
            allowed = required | {"commit_sha", "message"}
            missing = required - set(receipt)
            extra = set(receipt) - allowed
            if missing:
                raise RuntimeError(
                    f"receipt {receipt_path} missing fields: {sorted(missing)}"
                )
            if extra:
                raise RuntimeError(
                    f"receipt {receipt_path} has unsupported fields: {sorted(extra)}"
                )
            if (
                receipt["schema_version"] != 1
                or receipt["proposal_id"] != row["proposal_id"]
            ):
                raise RuntimeError(f"receipt identity mismatch: {receipt_path}")
            if receipt["state"] not in {"published", "error"}:
                raise RuntimeError(
                    f"unsupported receipt state in {receipt_path}: {receipt['state']}"
                )
            if (
                not isinstance(receipt["completed_at"], str)
                or not receipt["completed_at"].strip()
            ):
                raise RuntimeError(f"receipt {receipt_path} has invalid completed_at")
            if receipt["state"] == "published" and (
                not isinstance(receipt.get("commit_sha"), str)
                or not receipt["commit_sha"].strip()
                or receipt.get("pushed") is not True
            ):
                raise RuntimeError(
                    f"published receipt {receipt_path} requires nonblank commit_sha and pushed=true"
                )
            if receipt["state"] == "error" and (
                receipt.get("pushed") is not False
                or not isinstance(receipt.get("message"), str)
                or not receipt["message"].strip()
            ):
                raise RuntimeError(
                    f"error receipt {receipt_path} requires nonblank message and pushed=false"
                )
            if "commit_sha" in receipt and (
                not isinstance(receipt["commit_sha"], str)
                or not receipt["commit_sha"].strip()
            ):
                raise RuntimeError(f"receipt {receipt_path} has invalid commit_sha")
            encoded = json_dump(receipt)
            if row["receipt_json"] is not None:
                if row["receipt_json"] != encoded:
                    raise RuntimeError(f"immutable receipt changed: {receipt_path}")
                return
            if row["state"] != "publish_requested":
                raise RuntimeError(
                    f"receipt state {receipt['state']} cannot reconcile proposal state {row['state']}"
                )
            with self._connect() as connection:
                connection.execute(
                    "UPDATE proposals SET state=?, receipt_json=?, updated_at=? WHERE proposal_id=?",
                    (
                        receipt["state"],
                        encoded,
                        receipt["completed_at"],
                        row["proposal_id"],
                    ),
                )
                if receipt["state"] == "published":
                    connection.execute(
                        """
                        INSERT INTO versions (
                            proposal_id, document_id, source_revision, target_semantic_hash,
                            commit_sha, pushed, published_at, receipt_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["proposal_id"],
                            row["document_id"],
                            row["base_revision"],
                            row["target_semantic_hash"],
                            receipt.get("commit_sha"),
                            int(receipt["pushed"]),
                            receipt["completed_at"],
                            encoded,
                        ),
                    )

    def get(self, proposal_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        self._reconcile_receipt(row)
        with self._connect() as connection:
            refreshed = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        if refreshed is None:
            raise KeyError(proposal_id)
        return dict(refreshed)

    def list(
        self, document_id: Optional[str] = None, state: Optional[str] = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[str] = []
        if document_id is not None:
            clauses.append("document_id=?")
            values.append(document_id)
        if state is not None:
            clauses.append("state=?")
            values.append(state)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM proposals{where} ORDER BY created_at DESC, proposal_id",
                values,
            ).fetchall()
        for row in rows:
            self._reconcile_receipt(row)
        return [self.get(row["proposal_id"]) for row in rows]

    def transition(
        self,
        proposal_id: str,
        expected: str,
        target: str,
        reviewer: Optional[str] = None,
        handoff_id: Optional[str] = None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self._lock, self._connect() as connection:
            result = connection.execute(
                """
                UPDATE proposals SET state=?, reviewer=COALESCE(?, reviewer),
                    handoff_id=COALESCE(?, handoff_id), updated_at=?
                WHERE proposal_id=? AND state=?
                """,
                (target, reviewer, handoff_id, timestamp, proposal_id, expected),
            )
            if result.rowcount != 1:
                row = connection.execute(
                    "SELECT state FROM proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(proposal_id)
                raise ProposalTransitionError(
                    f"proposal {proposal_id} is {row['state']}; expected {expected}"
                )
        return self.get(proposal_id)
