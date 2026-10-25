from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address
from fastapi import Request

from hub.config import Settings
from hub.models import (
    AuthChallengeRequest,
    AuthChallengeView,
    AuthSessionView,
    AuthSessionVerifyRequest,
)

SESSION_COOKIE_NAME = "hub_session"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@dataclass
class AuthSessionService:
    secret: str
    ttl_seconds: int
    challenge_ttl_seconds: int
    nonce_store_path: Path
    public_base_url: str | None
    secure_cookie_mode: str

    def _sign(self, payload_text: str) -> str:
        digest = hmac.new(
            self.secret.encode("utf-8"),
            payload_text.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return _b64_encode(digest)

    def encode_session(self, session: AuthSessionView) -> str:
        payload_text = json.dumps(session.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
        payload_token = _b64_encode(payload_text.encode("utf-8"))
        signature = self._sign(payload_text)
        return f"{payload_token}.{signature}"

    def encode_challenge_token(self, payload: dict[str, str | None]) -> str:
        payload_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        payload_token = _b64_encode(payload_text.encode("utf-8"))
        signature = self._sign(f"challenge:{payload_text}")
        return f"{payload_token}.{signature}"

    def decode_challenge_token(self, token: str) -> dict[str, str | None] | None:
        if "." not in token:
            return None
        payload_token, signature = token.rsplit(".", 1)
        try:
            payload_text = _b64_decode(payload_token).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        expected_signature = self._sign(f"challenge:{payload_text}")
        if not hmac.compare_digest(signature, expected_signature):
            return None
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def decode_session(self, token: str | None) -> AuthSessionView | None:
        if not token or "." not in token:
            return None
        payload_token, signature = token.rsplit(".", 1)
        try:
            payload_text = _b64_decode(payload_token).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        expected_signature = self._sign(payload_text)
        if not hmac.compare_digest(signature, expected_signature):
            return None
        try:
            payload = json.loads(payload_text)
            session = AuthSessionView.model_validate(payload)
        except (json.JSONDecodeError, ValueError):
            return None
        if self.is_expired(session):
            return None
        return session

    def create_session(self, payload: AuthSessionVerifyRequest) -> AuthSessionView:
        issued_at = _now_utc()
        expires_at = issued_at + timedelta(seconds=self.ttl_seconds)
        return AuthSessionView(
            wallet_id=self.normalize_wallet(payload.wallet_id),
            chain_id=payload.chain_id,
            auth_type="wallet_plugin",
            issued_at=issued_at.isoformat(),
            expires_at=expires_at.isoformat(),
            ucan_session=payload.ucan_session,
            ucan_signature=payload.ucan_signature,
        )

    def create_challenge(self, request: Request, payload: AuthChallengeRequest) -> AuthChallengeView:
        issued_at = _now_utc()
        expires_at = issued_at + timedelta(seconds=self.challenge_ttl_seconds)
        wallet_id = self.normalize_wallet(payload.wallet_id)
        chain_id = payload.chain_id
        base_url = self.resolve_base_url(request)
        host = self.resolve_domain(request)
        nonce = secrets.token_urlsafe(18)
        challenge = "\n".join(
            [
                f"{host} wants you to sign in with your Ethereum account:",
                wallet_id,
                "",
                "Sign in to Agent Hub.",
                "",
                f"URI: {base_url}",
                "Version: 1",
                f"Chain ID: {chain_id or '1'}",
                f"Nonce: {nonce}",
                f"Issued At: {issued_at.isoformat()}",
                f"Expiration Time: {expires_at.isoformat()}",
            ]
        )
        token_payload = {
            "wallet_id": wallet_id,
            "chain_id": chain_id,
            "challenge": challenge,
            "nonce": nonce,
            "issued_at": issued_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "host": host,
            "base_url": base_url,
        }
        return AuthChallengeView(
            wallet_id=wallet_id,
            chain_id=chain_id,
            challenge=challenge,
            challenge_token=self.encode_challenge_token(token_payload),
            issued_at=issued_at.isoformat(),
            expires_at=expires_at.isoformat(),
        )

    def verify_wallet_session(self, payload: AuthSessionVerifyRequest) -> AuthSessionView:
        challenge_payload = self.decode_challenge_token(payload.challenge_token)
        if challenge_payload is None:
            raise ValueError("invalid challenge token")
        if challenge_payload.get("challenge") != payload.challenge:
            raise ValueError("challenge mismatch")
        if self.is_timestamp_expired(challenge_payload.get("expires_at")):
            raise ValueError("challenge expired")
        expected_wallet = self.normalize_wallet(challenge_payload.get("wallet_id"))
        actual_wallet = self.normalize_wallet(payload.wallet_id)
        if expected_wallet != actual_wallet:
            raise ValueError("wallet mismatch")
        expected_chain = challenge_payload.get("chain_id")
        if (expected_chain or payload.chain_id) and (expected_chain != payload.chain_id):
            raise ValueError("chain mismatch")
        token_digest = hashlib.sha256(payload.challenge_token.encode("utf-8")).hexdigest()
        if self.is_consumed(token_digest):
            raise ValueError("challenge already used")
        recovered_wallet = self.recover_wallet(payload.challenge, payload.signature)
        if recovered_wallet != actual_wallet:
            raise ValueError("signature does not match wallet")
        self.mark_consumed(token_digest, challenge_payload.get("expires_at"))
        return self.create_session(payload)

    def is_expired(self, session: AuthSessionView) -> bool:
        return self.is_timestamp_expired(session.expires_at)

    def is_timestamp_expired(self, expires_at_text: str | None) -> bool:
        if not expires_at_text:
            return True
        try:
            expires_at = datetime.fromisoformat(expires_at_text)
        except ValueError:
            return True
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= _now_utc()

    def normalize_wallet(self, wallet_id: str | None) -> str:
        if not wallet_id:
            raise ValueError("wallet_id is required")
        try:
            return to_checksum_address(wallet_id)
        except ValueError as exc:
            raise ValueError("invalid wallet_id") from exc

    def recover_wallet(self, challenge: str, signature: str) -> str:
        try:
            recovered = Account.recover_message(encode_defunct(text=challenge), signature=signature)
            return self.normalize_wallet(recovered)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("invalid wallet signature") from exc

    def load_consumed(self) -> dict[str, str]:
        if not self.nonce_store_path.exists():
            return {}
        try:
            payload = json.loads(self.nonce_store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        now = _now_utc()
        cleaned: dict[str, str] = {}
        for digest, expires_at_text in payload.items():
            if not isinstance(digest, str) or not isinstance(expires_at_text, str):
                continue
            try:
                expires_at = datetime.fromisoformat(expires_at_text)
            except ValueError:
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at > now:
                cleaned[digest] = expires_at_text
        return cleaned

    def save_consumed(self, payload: dict[str, str]) -> None:
        self.nonce_store_path.parent.mkdir(parents=True, exist_ok=True)
        self.nonce_store_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def is_consumed(self, digest: str) -> bool:
        return digest in self.load_consumed()

    def mark_consumed(self, digest: str, expires_at_text: str | None) -> None:
        payload = self.load_consumed()
        payload[digest] = expires_at_text or _now_utc().isoformat()
        self.save_consumed(payload)

    def current_session(self, request: Request) -> AuthSessionView | None:
        return self.decode_session(request.cookies.get(SESSION_COOKIE_NAME))

    def resolve_base_url(self, request: Request) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/") + "/"
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.hostname or "localhost"
        return f"{scheme}://{host}/"

    def resolve_domain(self, request: Request) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/").split("://", 1)[-1]
        return request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.hostname or "localhost"

    def should_secure_cookie(self, request: Request) -> bool:
        mode = (self.secure_cookie_mode or "auto").strip().lower()
        if mode == "always":
            return True
        if mode == "never":
            return False
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
        return scheme.lower() == "https"


def auth_service(settings: Settings) -> AuthSessionService:
    return AuthSessionService(
        secret=settings.session_secret,
        ttl_seconds=settings.session_ttl_seconds,
        challenge_ttl_seconds=settings.challenge_ttl_seconds,
        nonce_store_path=settings.resolved_runtime_dir / "auth" / "consumed_challenges.json",
        public_base_url=settings.public_base_url,
        secure_cookie_mode=settings.session_cookie_secure_mode,
    )
