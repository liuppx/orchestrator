from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses msvcrt below.
    fcntl = None

from orchestrator.protocols.hub_v1 import HubInventorySnapshotV1, HubRuntimeInstanceV1


T = TypeVar("T")


class HubProcessLock:
    """Keep one active Hub client per local state database."""

    def __init__(self, state_path: Path | str):
        self._file = None
        if str(state_path) == ":memory:":
            return
        self.path = Path(f"{state_path}.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - exercised on Windows CI.
                import msvcrt

                self._file.seek(0)
                self._file.write("0")
                self._file.flush()
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError) as exc:
            self._file.close()
            self._file = None
            raise ValueError("Hub state database is already locked by another process") from exc

    def close(self) -> None:
        if self._file is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            else:  # pragma: no cover - exercised on Windows CI.
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._file.close()
            self._file = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HubStateStore:
    """Durable local inbox/outbox state for one Hub process.

    The store deliberately uses a small SQLite database separate from the
    control-plane database. A machine must be able to queue events while the
    Backend is unavailable, and the Hub must not depend on Backend storage
    being reachable to start or inspect a local Runtime.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        if str(self.path) != ":memory:":
            os.chmod(self.path, 0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS hub_client_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hub_client_commands (
                    command_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hub_client_events (
                    sequence INTEGER PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hub_client_runs (
                    run_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    runtime_type TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    state TEXT NOT NULL,
                    command_id TEXT,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(instance_id, scheduled_for)
                );
                """
            )
            self._connection.executemany(
                "INSERT OR IGNORE INTO hub_client_meta(key, value) VALUES (?, ?)",
                [
                    ("next_sequence", "1"),
                    ("acked_sequence", "0"),
                    ("inventory_revision", "0"),
                ],
            )

    def _meta(self, key: str, default: str = "") -> str:
        row = self._connection.execute("SELECT value FROM hub_client_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def _set_meta(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO hub_client_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            return self._meta(key, default)

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._set_meta(key, value)

    def command(self, command_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT command_id, payload_json, attempt, status, result_json, updated_at "
                "FROM hub_client_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "command_id": row["command_id"],
            "payload": json.loads(row["payload_json"]),
            "attempt": int(row["attempt"]),
            "status": row["status"],
            "result": json.loads(row["result_json"]),
            "updated_at": row["updated_at"],
        }

    def transition_command(
        self,
        payload: dict[str, Any],
        status: str,
        event_builder: Callable[[int], T],
        event_serializer: Callable[[T], dict[str, Any]],
        result: dict[str, Any] | None = None,
    ) -> T:
        """Persist a command transition and its corresponding outbox event atomically."""
        command_id = str(payload["command_id"])
        attempt = int(payload["attempt"])
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT status, attempt FROM hub_client_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            updated_at = _now()
            result_json = json.dumps(result or {}, sort_keys=True, separators=(",", ":"))
            if row is None:
                self._connection.execute(
                    "INSERT INTO hub_client_commands(command_id, payload_json, attempt, status, result_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (command_id, encoded, attempt, status, result_json, updated_at),
                )
            else:
                self._connection.execute(
                    "UPDATE hub_client_commands SET payload_json = ?, attempt = ?, status = ?, result_json = ?, updated_at = ? "
                    "WHERE command_id = ?",
                    (encoded, attempt, status, result_json, updated_at, command_id),
                )
            sequence = int(self._meta("next_sequence", "1"))
            event = event_builder(sequence)
            event_payload = event_serializer(event)
            self._connection.execute(
                "INSERT INTO hub_client_events(sequence, payload_json, created_at) VALUES (?, ?, ?)",
                (sequence, json.dumps(event_payload, sort_keys=True, separators=(",", ":")), updated_at),
            )
            self._set_meta("next_sequence", str(sequence + 1))
        return event

    def pending_events(self, limit: int = 256) -> tuple[int, list[dict[str, Any]]]:
        with self._lock:
            after = int(self._meta("acked_sequence", "0"))
            rows = self._connection.execute(
                "SELECT payload_json FROM hub_client_events WHERE sequence > ? ORDER BY sequence ASC LIMIT ?",
                (after, limit),
            ).fetchall()
        return after, [json.loads(row["payload_json"]) for row in rows]

    def acknowledge_events(self, through_sequence: int) -> None:
        with self._lock, self._connection:
            current = int(self._meta("acked_sequence", "0"))
            if through_sequence < current:
                return
            self._connection.execute(
                "DELETE FROM hub_client_events WHERE sequence <= ?",
                (through_sequence,),
            )
            self._set_meta("acked_sequence", str(through_sequence))

    # --- scheduled runs -------------------------------------------------------------

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "instance_id": row["instance_id"],
            "runtime_type": row["runtime_type"],
            "scheduled_for": row["scheduled_for"],
            "trigger": row["trigger"],
            "state": row["state"],
            "command_id": row["command_id"],
            "result": json.loads(row["result_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def claim_run(
        self,
        run_id: str,
        instance_id: str,
        runtime_type: str,
        scheduled_for: datetime,
        trigger: str,
        command_id: str | None = None,
    ) -> bool:
        """Record a queued run for one schedule slot.

        ``(instance_id, scheduled_for)`` is the idempotency key: a Hub restart or a
        replayed command cannot trigger the same slot twice. Returns ``False`` when
        the slot or the run id already exists.
        """
        if scheduled_for.tzinfo is None:
            raise ValueError("scheduled_for must be timezone-aware")
        slot = scheduled_for.astimezone(timezone.utc).isoformat(timespec="microseconds")
        now = _now()
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    "INSERT INTO hub_client_runs"
                    "(run_id, instance_id, runtime_type, scheduled_for, trigger, state, command_id, result_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'queued', ?, '{}', ?, ?)",
                    (run_id, instance_id, runtime_type, slot, trigger, command_id, now, now),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM hub_client_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row is not None else None

    def runs_in_state(self, state: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM hub_client_runs WHERE state = ? ORDER BY created_at ASC", (state,)
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def last_scheduled_slot(self, instance_id: str) -> datetime | None:
        """Most recent schedule-triggered slot recorded for an instance, if any."""
        with self._lock:
            row = self._connection.execute(
                "SELECT MAX(scheduled_for) AS slot FROM hub_client_runs WHERE instance_id = ? AND trigger = 'schedule'",
                (instance_id,),
            ).fetchone()
        return datetime.fromisoformat(row["slot"]) if row is not None and row["slot"] else None

    def schedule_anchor(self, instance_id: str, now: datetime) -> datetime:
        """Point after which the next slot is due.

        The last schedule-triggered slot when one exists; otherwise the moment this
        Hub first observed the schedule, recorded durably so a schedule never fires
        for slots that predate its arrival on this machine.
        """
        last = self.last_scheduled_slot(instance_id)
        if last is not None:
            return last
        key = f"schedule_anchor:{instance_id}"
        with self._lock, self._connection:
            raw = self._meta(key)
            if not raw:
                self._set_meta(key, now.astimezone(timezone.utc).isoformat(timespec="microseconds"))
                return now
        return datetime.fromisoformat(raw)

    def transition_run(
        self,
        run_id: str,
        state: str,
        event_builder: Callable[[int], T],
        event_serializer: Callable[[T], dict[str, Any]],
        result: dict[str, Any] | None = None,
    ) -> T:
        """Persist a run transition and its outbox event atomically (mirrors transition_command)."""
        with self._lock, self._connection:
            row = self._connection.execute("SELECT run_id FROM hub_client_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            updated_at = _now()
            self._connection.execute(
                "UPDATE hub_client_runs SET state = ?, result_json = ?, updated_at = ? WHERE run_id = ?",
                (state, json.dumps(result or {}, sort_keys=True, separators=(",", ":")), updated_at, run_id),
            )
            sequence = int(self._meta("next_sequence", "1"))
            event = event_builder(sequence)
            self._connection.execute(
                "INSERT INTO hub_client_events(sequence, payload_json, created_at) VALUES (?, ?, ?)",
                (sequence, json.dumps(event_serializer(event), sort_keys=True, separators=(",", ":")), updated_at),
            )
            self._set_meta("next_sequence", str(sequence + 1))
        return event

    def heartbeat_due(self) -> bool:
        raw = self.get_meta("next_heartbeat_at")
        if not raw:
            return True
        try:
            return datetime.fromisoformat(raw) <= datetime.now(timezone.utc)
        except ValueError:
            return True

    def save_heartbeat(self, response: dict[str, Any]) -> None:
        interval = int(response["heartbeat_interval_seconds"])
        next_at = datetime.now(timezone.utc) + timedelta(seconds=interval)
        with self._lock, self._connection:
            self._set_meta("heartbeat_response", json.dumps(response, sort_keys=True, separators=(",", ":")))
            self._set_meta("next_heartbeat_at", next_at.isoformat())

    def cached_heartbeat(self) -> dict[str, Any] | None:
        raw = self.get_meta("heartbeat_response")
        return json.loads(raw) if raw else None

    def inventory_due(self) -> bool:
        raw = self.get_meta("next_inventory_at")
        if not raw:
            return True
        try:
            return datetime.fromisoformat(raw) <= datetime.now(timezone.utc)
        except ValueError:
            return True

    def mark_inventory_due(self) -> None:
        with self._lock, self._connection:
            self._set_meta("next_inventory_at", "1970-01-01T00:00:00+00:00")

    def pending_inventory(self) -> HubInventorySnapshotV1 | None:
        raw = self.get_meta("pending_inventory")
        return HubInventorySnapshotV1.model_validate_json(raw) if raw else None

    def prepare_inventory(self, instances: Iterable[HubRuntimeInstanceV1]) -> HubInventorySnapshotV1:
        instance_list = list(instances)
        with self._lock, self._connection:
            pending_snapshot = self._meta("pending_inventory", "")
            if pending_snapshot:
                snapshot = HubInventorySnapshotV1.model_validate_json(pending_snapshot)
            else:
                revision = int(self._meta("inventory_revision", "0")) + 1
                snapshot = HubInventorySnapshotV1(
                    protocol_version="1.0",
                    revision=revision,
                    captured_at=datetime.now(timezone.utc),
                    instances=instance_list,
                )
                self._set_meta("inventory_revision", str(revision))
            self._set_meta("pending_inventory", snapshot.model_dump_json())
        return snapshot

    def acknowledge_inventory(self, revision: int, interval_seconds: int) -> None:
        next_at = datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)
        with self._lock, self._connection:
            snapshot = self.pending_inventory()
            if snapshot is None or snapshot.revision != revision:
                raise ValueError("acknowledged inventory revision does not match the pending snapshot")
            self._set_meta("pending_inventory", "")
            self._set_meta("next_inventory_at", next_at.isoformat())

    @property
    def latest_inventory_revision(self) -> int:
        return int(self.get_meta("inventory_revision", "0"))

    @property
    def pending_event_count(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM hub_client_events").fetchone()[0])
