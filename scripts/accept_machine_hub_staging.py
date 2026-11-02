#!/usr/bin/env python3
"""Run a read-only two-Hub staging smoke check through the admin API."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4


HUB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
INSTANCE_ID_PATTERN = HUB_ID_PATTERN
FINAL_STATUSES = {"succeeded", "failed", "rejected", "unknown", "expired", "superseded", "poll_failed"}
NON_RETRYABLE_HTTP_STATUSES = {400, 401, 403, 404, 405, 409, 422}
MAX_TIMEOUT_SECONDS = 24 * 60 * 60 - 120


class ApiRequestError(RuntimeError):
    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_backend_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Backend URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Backend URL must not contain credentials, a query, or a fragment")
    if parsed.scheme != "https":
        host = parsed.hostname.lower()
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not is_loopback:
            raise ValueError("HTTPS is required except for a loopback development Backend")
    return value.strip().rstrip("/")


def parse_hub_target(value: str) -> tuple[str, str]:
    hub_id, separator, instance_id = value.partition("=")
    if not separator or not HUB_ID_PATTERN.fullmatch(hub_id) or not INSTANCE_ID_PATTERN.fullmatch(instance_id):
        raise ValueError("each --hub value must be HUB_ID=INSTANCE_ID using protocol-safe IDs")
    return hub_id, instance_id


def _request_json(
    backend_url: str,
    path: str,
    token: str,
    method: str,
    payload: dict | None,
    timeout_seconds: float,
) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{backend_url}{path}",
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
        method=method,
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=timeout_seconds) as response:
            response_body = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise ApiRequestError(f"Backend returned HTTP {exc.code}", http_status=exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ApiRequestError("Backend request failed or timed out") from None
    if len(response_body) > 1024 * 1024:
        raise ApiRequestError("Backend response exceeded 1 MiB")
    try:
        decoded = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiRequestError("Backend returned invalid JSON") from None
    if not isinstance(decoded, dict):
        raise ApiRequestError("Backend returned an unexpected response")
    return decoded


def _issue_inspection(
    backend_url: str,
    token: str,
    hub_id: str,
    instance_id: str,
    timeout_seconds: float,
) -> str:
    command_key = uuid4().hex
    trace_id = f"staging-inspect-{uuid4().hex}"
    expires_at = datetime.now(UTC) + timedelta(seconds=timeout_seconds + 120)
    payload = {
        "target_instance_id": instance_id,
        "runtime_type": "openclaw",
        "operation": "inspect_instance",
        "idempotency_key": f"staging-inspect-{command_key}",
        "desired_revision": 1,
        "trace_id": trace_id,
        "expires_at": expires_at.isoformat(),
    }
    path = f"/api/v1/internal/hubs/{urllib.parse.quote(hub_id, safe='')}/commands"
    response = _request_json(backend_url, path, token, "POST", payload, timeout_seconds=10)
    try:
        return str(UUID(str(response["command_id"])))
    except (KeyError, TypeError, ValueError):
        raise ApiRequestError("Backend returned an invalid command receipt") from None


def _read_command_status(
    backend_url: str,
    token: str,
    hub_id: str,
    command_id: str,
) -> dict:
    path = (
        f"/api/v1/internal/hubs/{urllib.parse.quote(hub_id, safe='')}"
        f"/commands/{urllib.parse.quote(command_id, safe='')}"
    )
    return _request_json(backend_url, path, token, "GET", None, timeout_seconds=10)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", required=True, help="Staging Backend base URL")
    parser.add_argument(
        "--hub",
        action="append",
        required=True,
        metavar="HUB_ID=INSTANCE_ID",
        help="explicit Hub and existing OpenClaw staging instance pair; provide exactly twice",
    )
    parser.add_argument(
        "--admin-token-env",
        default="HUB_ADMIN_TOKEN",
        help="environment variable containing the admin token (default: HUB_ADMIN_TOKEN)",
    )
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        backend_url = validate_backend_url(args.backend_url)
        targets = [parse_hub_target(value) for value in args.hub]
        if len(targets) != 2:
            raise ValueError("exactly two --hub targets are required")
        if len({hub_id for hub_id, _ in targets}) != 2:
            raise ValueError("the two --hub targets must use distinct Hub IDs")
        if not 1 <= args.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"--timeout-seconds must be between 1 and {MAX_TIMEOUT_SECONDS}")
        if not math.isfinite(args.poll_interval_seconds) or args.poll_interval_seconds <= 0:
            raise ValueError("--poll-interval-seconds must be a finite positive number")
        token = os.environ.get(args.admin_token_env, "").strip()
        if not token:
            raise ValueError(f"admin token environment variable {args.admin_token_env!r} is empty or unset")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    started_at = datetime.now(UTC)
    checks = [
        {
            "hub_id": hub_id,
            "instance_id": instance_id,
            "operation": "inspect_instance",
            "status": "issuing",
        }
        for hub_id, instance_id in targets
    ]
    deadline = time.monotonic() + args.timeout_seconds

    for check in checks:
        try:
            command_id = _issue_inspection(
                backend_url,
                token,
                check["hub_id"],
                check["instance_id"],
                args.timeout_seconds,
            )
            check["command_id"] = command_id
            check["status"] = "pending"
        except ApiRequestError as exc:
            check["status"] = "issue_failed"
            check["error"] = str(exc)

    while time.monotonic() < deadline:
        pending = [
            check
            for check in checks
            if check.get("command_id") and check.get("status") not in FINAL_STATUSES
        ]
        if not pending:
            break
        for check in pending:
            try:
                status = _read_command_status(
                    backend_url,
                    token,
                    check["hub_id"],
                    check["command_id"],
                )
            except ApiRequestError as exc:
                if exc.http_status in NON_RETRYABLE_HTTP_STATUSES:
                    check["status"] = "poll_failed"
                    check["error"] = str(exc)
                    check.pop("poll_error", None)
                else:
                    check["poll_error"] = str(exc)
                continue
            status_name = status.get("status")
            if not isinstance(status_name, str):
                check["poll_error"] = "Backend returned a status without a status name"
                continue
            check["status"] = status_name
            check["attempt"] = status.get("attempt")
            check["trace_id"] = status.get("trace_id")
            check["updated_at"] = status.get("updated_at")
            event = status.get("event", {})
            allowed_event_fields = {
                "sequence",
                "occurred_at",
                "phase",
                "progress_percent",
                "observed_state",
                "error_code",
            }
            check["event"] = {
                key: event[key]
                for key in allowed_event_fields
                if isinstance(event, dict) and key in event
            }
            check.pop("poll_error", None)
        if any(
            check.get("command_id") and check.get("status") not in FINAL_STATUSES
            for check in checks
        ):
            time.sleep(min(args.poll_interval_seconds, max(0.0, deadline - time.monotonic())))

    for check in checks:
        if check.get("command_id") and check.get("status") not in FINAL_STATUSES:
            check["status"] = "timeout"
            check["error"] = check.pop("poll_error", "command did not reach a terminal state before timeout")
        elif "poll_error" in check:
            check["error"] = check.pop("poll_error")
        check["success"] = check.get("status") == "succeeded"

    report = {
        "backend_url": backend_url,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "checks": checks,
    }
    print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if all(check["success"] for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
