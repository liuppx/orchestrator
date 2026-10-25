from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests


DEFAULT_TOKEN_TTL_SECONDS = 6 * 24 * 60 * 60


@dataclass
class IfindSettings:
    base_url: str
    access_token: str | None
    refresh_token: str | None
    timeout_ms: int


class IfindClient:
    def __init__(self, settings: IfindSettings) -> None:
        self.settings = settings
        self._cached_token = settings.access_token
        self._cached_token_expires_at = 0.0

    def _post(self, endpoint: str, payload: dict[str, Any] | None, headers: dict[str, str]) -> Any:
        response = requests.post(
            f"{self.settings.base_url}/{endpoint}",
            json=payload,
            headers=headers,
            timeout=self.settings.timeout_ms / 1000.0,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _extract_access_token(response: Any) -> str | None:
        if not isinstance(response, dict):
            return None
        data = response.get("data")
        if isinstance(data, dict):
            return data.get("access_token") or data.get("accessToken")
        return response.get("access_token") or response.get("accessToken")

    def get_access_token(self, force_refresh: bool = False) -> str:
        if self.settings.access_token and not force_refresh:
            return self.settings.access_token

        if self._cached_token and datetime.now().timestamp() < self._cached_token_expires_at and not force_refresh:
            return self._cached_token

        if not self.settings.refresh_token:
            raise RuntimeError("IFIND_ACCESS_TOKEN or IFIND_REFRESH_TOKEN is required")

        response = self._post(
            "get_access_token",
            None,
            headers={"refresh_token": self.settings.refresh_token},
        )
        access_token = self._extract_access_token(response)
        if not access_token:
            raise RuntimeError(f"Failed to extract access token from response: {response!r}")
        self._cached_token = access_token
        self._cached_token_expires_at = datetime.now().timestamp() + DEFAULT_TOKEN_TTL_SECONDS
        return access_token

    def call_endpoint(self, endpoint: str, payload: dict[str, Any]) -> Any:
        token = self.get_access_token()
        try:
            return self._post(endpoint, payload, headers={"access_token": token})
        except requests.HTTPError as exc:
            message = exc.response.text if exc.response is not None else str(exc)
            if any(word in message.lower() for word in ("token", "auth", "expired", "invalid")):
                token = self.get_access_token(force_refresh=True)
                return self._post(endpoint, payload, headers={"access_token": token})
            raise

    def query_realtime_quotes(self, symbol: str, indicators: str = "latest,preClose,open,high,low") -> Any:
        return self.call_endpoint(
            "real_time_quotation",
            {
                "codes": symbol,
                "indicators": indicators,
            },
        )

    def query_history_quotes(
        self,
        symbol: str,
        startdate: str,
        enddate: str,
        indicators: str = "open,high,low,close",
    ) -> Any:
        return self.call_endpoint(
            "cmd_history_quotation",
            {
                "codes": symbol,
                "indicators": indicators,
                "startdate": startdate,
                "enddate": enddate,
            },
        )


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_latest_price(payload: Any) -> float | None:
    if isinstance(payload, dict):
        table = payload.get("table")
        if isinstance(table, dict):
            for key in ("latest", "close", "price", "last", "preClose"):
                if key in table:
                    value = table.get(key)
                    if isinstance(value, list) and value:
                        number = _coerce_float(value[0])
                        if number is not None:
                            return number
                    number = _coerce_float(value)
                    if number is not None:
                        return number
        for key in ("tables", "data", "result", "results"):
            value = payload.get(key)
            price = extract_latest_price(value)
            if price is not None:
                return price
        for key in ("latest", "close", "price", "last", "preClose"):
            if key in payload:
                number = _coerce_float(payload.get(key))
                if number is not None:
                    return number
    elif isinstance(payload, list):
        for item in payload:
            price = extract_latest_price(item)
            if price is not None:
                return price
    return None


def extract_pre_close(payload: Any) -> float | None:
    if isinstance(payload, dict):
        table = payload.get("table")
        if isinstance(table, dict):
            value = table.get("preClose") or table.get("preclose")
            if isinstance(value, list) and value:
                return _coerce_float(value[0])
            return _coerce_float(value)
        for key in ("tables", "data", "result", "results"):
            extracted = extract_pre_close(payload.get(key))
            if extracted is not None:
                return extracted
        if "preClose" in payload or "preclose" in payload:
            return _coerce_float(payload.get("preClose") or payload.get("preclose"))
    elif isinstance(payload, list):
        for item in payload:
            extracted = extract_pre_close(item)
            if extracted is not None:
                return extracted
    return None


def extract_observed_at(payload: Any) -> datetime | None:
    if isinstance(payload, dict):
        if "time" in payload and isinstance(payload["time"], list) and payload["time"]:
            raw = payload["time"][0]
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(raw, fmt)
                except ValueError:
                    pass
        for key in ("tables", "data", "result", "results"):
            extracted = extract_observed_at(payload.get(key))
            if extracted is not None:
                return extracted
    elif isinstance(payload, list):
        for item in payload:
            extracted = extract_observed_at(item)
            if extracted is not None:
                return extracted
    return None


def extract_close_series(payload: Any) -> list[float]:
    values: list[float] = []
    if isinstance(payload, dict):
        table = payload.get("table")
        if isinstance(table, dict):
            for key in ("close", "CLOSE", "latest"):
                value = table.get(key)
                if isinstance(value, list):
                    for item in value:
                        number = _coerce_float(item)
                        if number is not None:
                            values.append(number)
        for key in ("tables", "data", "result", "results", "rows", "items"):
            inner = payload.get(key)
            values.extend(extract_close_series(inner))
        for key in ("close", "CLOSE", "latest"):
            if key in payload:
                number = _coerce_float(payload.get(key))
                if number is not None:
                    values.append(number)
    elif isinstance(payload, list):
        for item in payload:
            values.extend(extract_close_series(item))
    return values
