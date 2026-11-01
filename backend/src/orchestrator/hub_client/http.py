from __future__ import annotations

import json
import ipaddress
import urllib.error
import urllib.parse
import urllib.request
from typing import TypeVar

from pydantic import BaseModel

from orchestrator.protocols.hub_v1 import (
    HubCommandLeaseRenewalRequestV1,
    HubCommandLeaseV1,
    HubCommandPollResponseV1,
    HubEventBatchAckV1,
    HubEventBatchRequestV1,
    HubHeartbeatRequestV1,
    HubHeartbeatResponseV1,
    HubInventoryAckV1,
    HubInventorySnapshotV1,
)


M = TypeVar("M", bound=BaseModel)


class HubTransportError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class HttpHubTransport:
    """Synchronous HTTPS transport for the versioned internal Hub API."""

    def __init__(self, base_url: str, hub_id: str, token: str, timeout_seconds: float = 15.0):
        parsed_url = urllib.parse.urlparse(base_url)
        if parsed_url.scheme not in {"https", "http"} or not parsed_url.hostname:
            raise ValueError("Hub backend URL must use http:// or https://")
        if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise ValueError("Hub backend URL must not contain user info, query, or fragment components")
        if parsed_url.scheme == "http":
            try:
                is_loopback = ipaddress.ip_address(parsed_url.hostname).is_loopback
            except ValueError:
                is_loopback = parsed_url.hostname == "localhost"
            if not is_loopback:
                raise ValueError("Hub credentials may only be sent over HTTPS except to loopback development URLs")
        if len(token) < 32 or "\n" in token or "\r" in token:
            raise ValueError("Hub credential is empty or malformed")
        self.base_url = base_url.rstrip("/")
        self.hub_id = urllib.parse.quote(hub_id, safe="")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def _request(self, path: str, method: str, payload: BaseModel | None, response_type: type[M]) -> M:
        body = None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        if payload is not None:
            body = json.dumps(payload.model_dump(mode="json"), separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:512]
            raise HubTransportError(f"Hub API returned HTTP {exc.code}: {detail}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HubTransportError(f"Hub API request failed: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HubTransportError("Hub API response was not valid JSON") from exc
        try:
            return response_type.model_validate(parsed)
        except Exception as exc:  # noqa: BLE001
            raise HubTransportError(f"Hub API response did not match protocol: {exc}") from exc

    def heartbeat(self, payload: HubHeartbeatRequestV1) -> HubHeartbeatResponseV1:
        return self._request(f"/api/v1/internal/hubs/{self.hub_id}/heartbeat", "POST", payload, HubHeartbeatResponseV1)

    def inventory(self, payload: HubInventorySnapshotV1) -> HubInventoryAckV1:
        return self._request(f"/api/v1/internal/hubs/{self.hub_id}/inventory", "PUT", payload, HubInventoryAckV1)

    def poll_commands(self, limit: int = 10) -> HubCommandPollResponseV1:
        return self._request(
            f"/api/v1/internal/hubs/{self.hub_id}/commands?limit={limit}",
            "GET",
            None,
            HubCommandPollResponseV1,
        )

    def renew_command_lease(self, command_id: str, attempt: int) -> HubCommandLeaseV1:
        payload = HubCommandLeaseRenewalRequestV1(attempt=attempt)
        return self._request(
            f"/api/v1/internal/hubs/{self.hub_id}/commands/{urllib.parse.quote(command_id, safe='')}/lease",
            "POST",
            payload,
            HubCommandLeaseV1,
        )

    def post_events(self, payload: HubEventBatchRequestV1) -> HubEventBatchAckV1:
        return self._request(f"/api/v1/internal/hubs/{self.hub_id}/events", "POST", payload, HubEventBatchAckV1)
