from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import and_, or_, select

from orchestrator.config import Settings
from orchestrator.storage import OutboxEvent, sessions_for


class DeliveryError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class WarehouseOutboxWorker:
    def __init__(
        self,
        database_url: str,
        warehouse_url: str | None,
        tool_token: str | None,
        worker_id: str | None = None,
        timeout_seconds: int = 10,
        archive_prefix: str = "/services/orchestrator/events",
        lease_seconds: int = 60,
        max_attempts: int = 8,
    ) -> None:
        self.database_url = database_url
        self.warehouse_url = (warehouse_url or "").rstrip("/")
        self.tool_token = (tool_token or "").strip()
        parsed_url = urlsplit(self.warehouse_url)
        if self.warehouse_url and (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.path not in {"", "/"}
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("YEYING_WAREHOUSE_URL must be an HTTP(S) service root without credentials")
        self.worker_id = worker_id or f"outbox-{uuid4().hex}"
        self.timeout_seconds = max(1, min(int(timeout_seconds), 120))
        self.archive_prefix = "/" + archive_prefix.strip("/") if archive_prefix.strip("/") else "/services/orchestrator/events"
        self.lease_seconds = max(5, min(int(lease_seconds), 3600))
        self.max_attempts = max(1, min(int(max_attempts), 100))
        if not any(
            self.archive_prefix == root or self.archive_prefix.startswith(f"{root}/")
            for root in ("/personal", "/apps", "/services")
        ) or any(part in {".", ".."} for part in self.archive_prefix.split("/")):
            raise ValueError("Warehouse archive prefix must be a normalized /personal, /apps, or /services path")

    @property
    def enabled(self) -> bool:
        return bool(self.warehouse_url and self.tool_token)

    def run_once(self, limit: int = 20) -> dict[str, int | bool]:
        if not self.enabled:
            return {"claimed": 0, "delivered": 0, "failed": 0, "disabled": True}
        rows = self._claim(max(1, min(int(limit), 100)))
        delivered = 0
        failed = 0
        for row in rows:
            try:
                etag = self._deliver(row)
            except DeliveryError as exc:
                self._mark_failure(row.id, int(row.attempts or 0), str(exc), exc.retryable)
                failed += 1
            except Exception as exc:  # noqa: BLE001
                self._mark_failure(row.id, int(row.attempts or 0), f"{type(exc).__name__}: {exc}", True)
                failed += 1
            else:
                self._mark_delivered(row.id, int(row.attempts or 0), etag)
                delivered += 1
        return {"claimed": len(rows), "delivered": delivered, "failed": failed, "disabled": False}

    def retry_failed(self, limit: int = 100) -> int:
        now = datetime.now(UTC)
        with sessions_for(self.database_url).begin() as session:
            rows = session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.status == "failed")
                .order_by(OutboxEvent.created_at.asc())
                .limit(max(1, min(int(limit), 1000)))
                .with_for_update(skip_locked=True)
            ).all()
            for row in rows:
                row.status = "pending"
                row.attempts = 0
                row.last_error = None
                row.next_attempt_at = now
            return len(rows)

    def run_forever(self, interval_seconds: int = 5, limit: int = 20) -> None:
        while True:
            result = self.run_once(limit)
            if result["claimed"]:
                print(json.dumps({"event": "outbox_batch", **result}, sort_keys=True), flush=True)
            time.sleep(max(1, min(int(interval_seconds), 60)))

    def _claim(self, limit: int) -> list[OutboxEvent]:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self.lease_seconds)
        with sessions_for(self.database_url).begin() as session:
            rows = session.scalars(
                select(OutboxEvent)
                .where(
                    and_(
                        or_(
                            OutboxEvent.status == "pending",
                            and_(OutboxEvent.status == "leased", OutboxEvent.lease_expires_at <= now),
                        ),
                        or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now),
                    )
                )
                .order_by(OutboxEvent.created_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for row in rows:
                row.status = "leased"
                row.lease_owner = self.worker_id
                row.lease_expires_at = expires
                row.attempts = int(row.attempts or 0) + 1
            return rows

    def _deliver(self, row: OutboxEvent) -> str | None:
        path = row.object_path or f"{self.archive_prefix}/{row.id}.json"
        if path.startswith("/services/orchestrator/events/"):
            path = f"{self.archive_prefix}/{path.rsplit('/', 1)[-1]}"
        content = json.dumps(row.payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > 5 * 1024 * 1024:
            raise DeliveryError("outbox object exceeds the Warehouse 5 MiB write limit", False)
        checksum = hashlib.sha256(content_bytes).hexdigest()
        if row.checksum and row.checksum != checksum:
            raise DeliveryError("outbox checksum mismatch", False)
        arguments: dict[str, Any] = {
            "path": path,
            "content": content,
            "encoding": "utf-8",
            "contentType": "application/json; charset=utf-8",
            "checksumSha256": checksum,
            "overwrite": False,
        }
        body = json.dumps({"name": "warehouse.object.put", "arguments": arguments}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.warehouse_url}/api/v1/public/tools/warehouse/call",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.tool_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Request-ID": row.id,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_body = response.read()
                if not 200 <= response.status < 300:
                    raise DeliveryError(f"warehouse returned HTTP {response.status}", response.status >= 500)
                try:
                    decoded = json.loads(response_body.decode("utf-8")) if response_body else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    decoded = {}
                if not isinstance(decoded, dict) or decoded.get("name") != "warehouse.object.put" or not isinstance(decoded.get("result"), dict):
                    raise DeliveryError("warehouse returned an invalid object.put response", False)
                result = decoded["result"]
                if result.get("path") != path or result.get("checksumSha256") != checksum:
                    raise DeliveryError("warehouse returned mismatched object metadata", False)
                etag = self._etag(decoded)
                if not etag:
                    raise DeliveryError("warehouse response is missing ETag", False)
                return etag
        except urllib.error.HTTPError as exc:
            retryable = exc.code >= 500 or exc.code == 429
            try:
                payload = json.loads(exc.read(512).decode("utf-8", errors="replace"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            error_code = payload.get("code") if isinstance(payload, dict) else None
            suffix = f" ({error_code})" if isinstance(error_code, str) and error_code.isupper() else ""
            raise DeliveryError(f"warehouse returned HTTP {exc.code}{suffix}", retryable) from exc
        except urllib.error.URLError as exc:
            raise DeliveryError(f"warehouse unavailable: {exc.reason}", True) from exc
        except TimeoutError as exc:
            raise DeliveryError("warehouse request timed out", True) from exc

    @staticmethod
    def _etag(decoded: object) -> str | None:
        if not isinstance(decoded, dict):
            return None
        for container in (decoded, decoded.get("data"), decoded.get("result")):
            if isinstance(container, dict):
                for key in ("etag", "eTag", "ETag"):
                    if container.get(key):
                        return str(container[key])[:255]
                metadata = container.get("metadata")
                if isinstance(metadata, dict) and metadata.get("etag"):
                    return str(metadata["etag"])[:255]
        return None

    def _mark_delivered(self, event_id: str, attempt: int, etag: str | None) -> None:
        now = datetime.now(UTC)
        with sessions_for(self.database_url).begin() as session:
            row = session.get(OutboxEvent, event_id, with_for_update=True)
            if not self._owns_current_lease(row, attempt, now):
                return
            row.status = "delivered"
            row.delivered_at = now
            row.remote_etag = etag
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_error = None

    def _mark_failure(self, event_id: str, attempt: int, error: str, retryable: bool) -> None:
        now = datetime.now(UTC)
        with sessions_for(self.database_url).begin() as session:
            row = session.get(OutboxEvent, event_id, with_for_update=True)
            if not self._owns_current_lease(row, attempt, now):
                return
            exhausted = int(row.attempts or 0) >= self.max_attempts
            row.status = "pending" if retryable and not exhausted else "failed"
            row.next_attempt_at = now + timedelta(seconds=min(300, 2 ** min(int(row.attempts or 1), 8))) if row.status == "pending" else None
            row.last_error = error[:2000]
            row.lease_owner = None
            row.lease_expires_at = None

    def _owns_current_lease(self, row: OutboxEvent | None, attempt: int, now: datetime) -> bool:
        if row is None or row.status != "leased" or row.lease_owner != self.worker_id or int(row.attempts or 0) != attempt:
            return False
        expires_at = row.lease_expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at is None or expires_at > now


def main() -> None:
    parser = argparse.ArgumentParser(description="Deliver Orchestrator outbox events to YeYing Warehouse")
    parser.add_argument("--database-url")
    parser.add_argument("--worker-id")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=5)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    settings = Settings()
    database_url = args.database_url or settings.database_url
    if not database_url:
        parser.error("set HUB_DATABASE_URL or pass --database-url")
    worker = WarehouseOutboxWorker(
        database_url,
        settings.warehouse_url,
        settings.warehouse_tool_token,
        worker_id=args.worker_id,
        timeout_seconds=settings.warehouse_request_timeout_seconds,
        archive_prefix=settings.warehouse_archive_prefix,
    )
    if not worker.enabled:
        parser.error("set YEYING_WAREHOUSE_URL and YEYING_WAREHOUSE_TOOL_TOKEN to enable delivery")
    if args.retry_failed:
        print(json.dumps({"requeued": worker.retry_failed(args.limit)}, sort_keys=True))
        if not args.once:
            return
    if args.once:
        print(json.dumps(worker.run_once(args.limit), sort_keys=True))
        return
    worker.run_forever(args.interval_seconds, args.limit)


if __name__ == "__main__":
    main()
