from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from eth_utils import to_checksum_address
from sqlalchemy import select, update

from orchestrator.config import Settings
from orchestrator.models import AuthSessionView
from orchestrator.services.auth import AuthSessionService
from orchestrator.storage import IdentityLoginSession, sessions_for

IDENTITY_SCOPES = ["identity.basic", "identity.wallet", "identity.username", "identity.email"]
IDENTITY_SCOPES_WITH_AVATAR = [*IDENTITY_SCOPES, "identity.avatar"]
LOGIN_TTL_SECONDS = 300


def _now() -> datetime:
    return datetime.now(UTC)


def _expires_at(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical(value: Any) -> str:
    if value is None or isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{json.dumps(key, ensure_ascii=False)}:{_canonical(value[key])}"
            for key in sorted(value)
        ) + "}"
    raise ValueError("identity presentation contains unsupported data")


def _verify_ed25519(public_key: str, signature: str, payload: Any) -> None:
    try:
        key = Ed25519PublicKey.from_public_bytes(_b64decode(public_key))
        key.verify(_b64decode(signature), _canonical(payload).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("IDENTITY_PRESENTATION_PROOF_INVALID") from exc


def _verify_document(document: dict[str, Any], did: str) -> None:
    if document.get("version") != 1 or document.get("id") != did or not isinstance(document.get("controllers"), list):
        raise ValueError("IDENTITY_DOCUMENT_INVALID")
    proof = document.get("proof") or {}
    if not isinstance(proof, dict):
        raise ValueError("IDENTITY_DOCUMENT_PROOF_INVALID")
    if proof.get("type") != "YeyingIdentityDocumentProofV1" or proof.get("purpose") != "manage":
        raise ValueError("IDENTITY_DOCUMENT_PROOF_INVALID")
    controller = _controller(document, proof.get("verificationMethod"), did, "manage")
    unsigned = {key: value for key, value in document.items() if key != "proof"}
    _verify_ed25519(controller["publicKey"], proof.get("proofValue", ""), unsigned)


def _controller(document: dict[str, Any], method: str, did: str, purpose: str) -> dict[str, Any]:
    for controller in document.get("controllers", []):
        if not isinstance(controller, dict):
            continue
        if (
            method == f"{did}#{controller.get('controllerId')}"
            and controller.get("status") == "active"
            and isinstance(controller.get("purposes"), list)
            and purpose in controller.get("purposes", [])
        ):
            return controller
    raise ValueError("IDENTITY_CONTROLLER_NOT_AUTHORIZED")


def _load_trust_bundle(directory: Path | None) -> tuple[str, dict[str, Any]]:
    if directory is None:
        raise ValueError("identity_trust_directory_not_configured")
    try:
        metadata_bytes = (directory / "issuer-metadata.json").read_bytes()
        jwks_bytes = (directory / "jwks.json").read_bytes()
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        metadata = json.loads(metadata_bytes)
        jwks = json.loads(jwks_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("identity_trust_bundle_unavailable") from exc
    if not isinstance(metadata, dict) or not isinstance(jwks, dict) or not isinstance(manifest, dict):
        raise ValueError("identity_trust_bundle_invalid")
    if hashlib.sha256(metadata_bytes).hexdigest() != manifest.get("metadataSha256"):
        raise ValueError("identity_trust_bundle_checksum_mismatch")
    if hashlib.sha256(jwks_bytes).hexdigest() != manifest.get("jwksSha256"):
        raise ValueError("identity_trust_bundle_checksum_mismatch")
    issuer = str(metadata.get("issuer", "")).strip()
    if not issuer or issuer != manifest.get("issuer") or not isinstance(jwks.get("keys"), list):
        raise ValueError("identity_issuer_invalid")
    return issuer, jwks


def _verify_credential(token: str, did: str, issuer: str, jwks: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    try:
        header_part, payload_part, signature_part = token.split(".")
        header = json.loads(_b64decode(header_part))
        claims = json.loads(_b64decode(payload_part))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("identity_credential_invalid") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") != "EdDSA" or not header.get("kid"):
        raise ValueError("identity_credential_alg_invalid")
    key = next((item for item in jwks["keys"] if isinstance(item, dict) and item.get("kid") == header["kid"] and item.get("kty") == "OKP" and item.get("crv") == "Ed25519"), None)
    if key is None:
        raise ValueError("identity_issuer_jwks_invalid")
    try:
        Ed25519PublicKey.from_public_bytes(_b64decode(key["x"])).verify(
            _b64decode(signature_part), f"{header_part}.{payload_part}".encode("ascii")
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError("identity_credential_invalid") from exc
    now = int(time.time())
    if claims.get("iss") != issuer or claims.get("sub") != did or not claims.get("jti"):
        raise ValueError("identity_credential_subject_mismatch")
    expires_at = claims.get("exp")
    not_before = claims.get("nbf", 0)
    if (
        not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool) or expires_at <= now
        or not isinstance(not_before, (int, float)) or isinstance(not_before, bool) or not_before > now
    ):
        raise ValueError("identity_credential_expired")
    vc = claims.get("vc") or {}
    if not isinstance(vc, dict):
        raise ValueError("identity_credential_type_mismatch")
    types = vc.get("type", [])
    credential_type = next((item for item in types if isinstance(item, str) and item != "VerifiableCredential"), "") if isinstance(types, list) else ""
    subject = vc.get("credentialSubject") or {}
    if not credential_type or not isinstance(subject, dict) or subject.get("id") != did:
        raise ValueError("identity_credential_type_mismatch")
    return credential_type, subject


def _verify_credentials(credentials: list[str], did: str, required_scopes: list[str], trust_dir: Path | None) -> dict[str, dict[str, Any]]:
    issuer, jwks = _load_trust_bundle(trust_dir)
    verified: dict[str, dict[str, Any]] = {}
    for token in credentials:
        kind, subject = _verify_credential(token, did, issuer, jwks)
        verified[kind] = subject
    expected = {
        "identity.wallet": "WalletAccountCredential",
        "identity.username": "UsernameCredential",
        "identity.email": "EmailCredential",
        "identity.avatar": "AvatarCredential",
    }
    for scope in required_scopes:
        credential_type = expected.get(scope)
        if credential_type and credential_type not in verified:
            raise ValueError(f"identity_credential_required:{credential_type}")
    return verified


def verify_presentation(presentation: dict[str, Any], *, audience: str, nonce: str, scopes: list[str], trust_dir: Path | None) -> tuple[str, str, dict[str, dict[str, Any]]]:
    if presentation.get("version") != 1:
        raise ValueError("IDENTITY_PRESENTATION_INVALID")
    did = str(presentation.get("holder", "")).strip()
    if not did.startswith("did:yeying:wid_") or len(did) < 35:
        raise ValueError("IDENTITY_PRESENTATION_INVALID")
    if presentation.get("audience") != audience or presentation.get("nonce") != nonce:
        raise ValueError("IDENTITY_PRESENTATION_CONTEXT_MISMATCH")
    presented_scopes = presentation.get("scopes")
    if not isinstance(presented_scopes, list) or any(scope not in presented_scopes for scope in scopes):
        raise ValueError("IDENTITY_PRESENTATION_SCOPE_MISMATCH")
    try:
        issued_at = datetime.fromisoformat(str(presentation["issuedAt"]).replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(str(presentation["expiresAt"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise ValueError("IDENTITY_PRESENTATION_EXPIRED") from exc
    if issued_at.tzinfo is None or expires_at.tzinfo is None:
        raise ValueError("IDENTITY_PRESENTATION_EXPIRED")
    if issued_at > _now() + timedelta(seconds=60) or expires_at <= _now() - timedelta(seconds=60):
        raise ValueError("IDENTITY_PRESENTATION_EXPIRED")
    document = presentation.get("identityDocument")
    if not isinstance(document, dict):
        raise ValueError("IDENTITY_PRESENTATION_KEY_MISSING")
    _verify_document(document, did)
    proof = presentation.get("proof") or {}
    if not isinstance(proof, dict):
        raise ValueError("IDENTITY_PRESENTATION_INVALID")
    if proof.get("type") != "YeyingIdentityPresentationProofV1" or proof.get("purpose") != "authentication":
        raise ValueError("IDENTITY_PRESENTATION_INVALID")
    controller = _controller(document, proof.get("verificationMethod"), did, "authentication")
    unsigned = {key: value for key, value in presentation.items() if key != "proof"}
    _verify_ed25519(controller["publicKey"], proof.get("proofValue", ""), unsigned)
    credentials = _verify_credentials(presentation.get("credentials") or [], did, scopes, trust_dir)
    wallet = credentials.get("WalletAccountCredential", {})
    address = str(wallet.get("address", "")).strip()
    chain = str(wallet.get("chainKey", "")).strip()
    proof_wallet = presentation.get("walletProof") or {}
    if not isinstance(proof_wallet, dict):
        raise ValueError("identity_wallet_account_invalid")
    try:
        normalized_address = to_checksum_address(address)
    except ValueError as exc:
        raise ValueError("identity_wallet_account_invalid") from exc
    if normalized_address.lower() != str(proof_wallet.get("address", "")).lower() or not chain or chain.lower() != str(proof_wallet.get("chainKey", "")).lower():
        raise ValueError("identity_wallet_account_invalid")
    if not str((credentials.get("UsernameCredential") or {}).get("username", "")).strip():
        raise ValueError("identity_username_credential_invalid")
    if not str((credentials.get("EmailCredential") or {}).get("email", "")).strip():
        raise ValueError("identity_email_credential_invalid")
    return did, normalized_address, credentials


def _node_post(settings: Settings, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not settings.identity_node_url or not settings.identity_app_id:
        raise ValueError("夜莺身份服务尚未配置")
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        settings.identity_node_url.rstrip("/") + path,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json", "X-YeYing-Client": settings.identity_app_id},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            envelope = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            envelope = json.loads(exc.read())
        except (json.JSONDecodeError, UnicodeDecodeError):
            envelope = None
        message = str(envelope.get("message", "")) if isinstance(envelope, dict) else ""
        if "REDIRECT_URI" in message:
            raise ValueError("Node 未授权当前通行证回调地址，请检查应用 redirectUris 配置") from exc
        raise ValueError(message or "夜莺身份服务请求失败") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ValueError("无法连接夜莺身份服务") from exc
    if not isinstance(envelope, dict) or envelope.get("code") != 0:
        raise ValueError(str(envelope.get("message", "夜莺身份服务返回异常")) if isinstance(envelope, dict) else "夜莺身份服务返回异常")
    data = envelope.get("data", envelope)
    if not isinstance(data, dict):
        raise ValueError("夜莺身份服务返回异常")
    return data


def _verify_exchange_identity(data: dict[str, Any], settings: Settings) -> tuple[str, str, dict[str, dict[str, Any]]]:
    did = str(data.get("did", "")).strip()
    if not did.startswith("did:yeying:wid_"):
        raise ValueError("夜莺身份服务未返回有效钱包身份")
    address = str(data.get("walletAddress", data.get("wallet_address", ""))).strip()
    try:
        normalized_address = to_checksum_address(address)
    except ValueError as exc:
        raise ValueError("夜莺身份服务未返回有效钱包账户") from exc
    scopes = data.get("scopes") or []
    if any(scope not in scopes for scope in IDENTITY_SCOPES):
        raise ValueError("夜莺身份授权范围不完整")
    tokens = [str(item.get("credential", "")) for item in data.get("credentials", []) if isinstance(item, dict) and item.get("credential")]
    verified = _verify_credentials(tokens, did, IDENTITY_SCOPES, settings.identity_trust_dir)
    account = verified.get("WalletAccountCredential") or {}
    if str(account.get("address", "")).lower() != normalized_address.lower() or not account.get("chainKey"):
        raise ValueError("夜莺身份账户凭证无效")
    if not str((verified.get("UsernameCredential") or {}).get("username", "")).strip():
        raise ValueError("夜莺身份未返回用户名")
    if not str((verified.get("EmailCredential") or {}).get("email", "")).strip():
        raise ValueError("夜莺身份未返回已验证邮箱")
    return did, normalized_address, verified


def _session_view(did: str, wallet: str, settings: Settings) -> AuthSessionView:
    now = _now()
    return AuthSessionView(
        wallet_id=wallet,
        chain_id=None,
        auth_type="wallet_identity",
        did=did,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=settings.session_ttl_seconds)).isoformat(),
        ucan_session=None,
        ucan_signature=None,
    )


@dataclass
class IdentityAuthService:
    settings: Settings
    auth: AuthSessionService

    def _database(self):
        if not self.settings.database_url:
            raise ValueError("钱包身份登录需要配置 HUB_DATABASE_URL")
        return sessions_for(self.settings.database_url)

    def create_wallet_session(self, audience: str, avatar: bool = False) -> dict[str, Any]:
        session_id = secrets.token_urlsafe(32)
        scopes = IDENTITY_SCOPES_WITH_AVATAR if avatar else IDENTITY_SCOPES
        audience = audience.rstrip("/")
        if not audience.startswith(("http://", "https://")):
            raise ValueError("登录来源地址无效")
        nonce = secrets.token_urlsafe(32)
        expires_at = _now() + timedelta(seconds=LOGIN_TTL_SECONDS)
        with self._database().begin() as db:
            db.add(IdentityLoginSession(
                id=session_id, kind="wallet", audience=audience, nonce=nonce, scopes=scopes,
                status="pending", expires_at=expires_at, created_at=_now(), updated_at=_now(),
            ))
        return {"session_id": session_id, "nonce": nonce, "audience": audience, "scopes": scopes, "expires_at": expires_at.isoformat()}

    def verify_wallet_session(self, session_id: str, presentation: dict[str, Any]) -> tuple[AuthSessionView, str]:
        with self._database().begin() as db:
            row = db.scalar(select(IdentityLoginSession).where(IdentityLoginSession.id == session_id).with_for_update())
            if row is None or row.kind != "wallet" or row.status != "pending" or _expires_at(row.expires_at) <= _now():
                raise ValueError("钱包身份登录会话无效或已过期")
            row.status = "verifying"
            audience, nonce, scopes = row.audience or "", row.nonce or "", row.scopes or []
        try:
            did, wallet, _credentials = verify_presentation(
                presentation, audience=audience, nonce=nonce, scopes=scopes, trust_dir=self.settings.identity_trust_dir
            )
        except ValueError as exc:
            with self._database().begin() as db:
                db.execute(update(IdentityLoginSession).where(IdentityLoginSession.id == session_id).values(status="failed", error_message=str(exc), updated_at=_now()))
            raise
        with self._database().begin() as db:
            row = db.get(IdentityLoginSession, session_id)
            if row is None or row.status != "verifying":
                raise ValueError("钱包身份登录会话已被使用")
            row.status = "completed"
            row.did = did
            row.wallet_address = wallet
            row.updated_at = _now()
        return _session_view(did, wallet, self.settings), did

    def create_passkey_session(self, callback: str) -> dict[str, Any]:
        if not self.settings.identity_node_url or not self.settings.identity_app_id:
            raise ValueError("请配置 IDENTITY_NODE_URL 和 IDENTITY_APP_ID")
        if not callback:
            raise ValueError("请配置 IDENTITY_REDIRECT_URI")
        session_id = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        scopes = list(IDENTITY_SCOPES)
        result = _node_post(self.settings, "/api/v1/public/identity/authorize/request", {
            "appId": self.settings.identity_app_id,
            "redirectUri": callback,
            "state": state,
            "codeChallenge": challenge,
            "codeChallengeMethod": "S256",
            "scopes": scopes,
            "requestTtlMs": LOGIN_TTL_SECONDS * 1000,
        })
        request_id = str(result.get("requestId", "")).strip()
        verify_url = str(result.get("verifyUrl", "")).strip()
        if not request_id or not verify_url:
            raise ValueError("夜莺身份服务未返回授权请求信息")
        expiry = _parse_time(result.get("expiresAt")) or (_now() + timedelta(seconds=LOGIN_TTL_SECONDS))
        with self._database().begin() as db:
            db.add(IdentityLoginSession(
                id=session_id, kind="passkey", request_id=request_id, state=state, code_verifier=verifier,
                redirect_uri=callback, audience=str(result.get("audience", "")), nonce=str(result.get("nonce", "")), scopes=scopes,
                status="pending", expires_at=expiry, created_at=_now(), updated_at=_now(),
            ))
        return {"session_id": session_id, "request_id": request_id, "verify_url": _normalize_verify_url(self.settings.identity_node_url, verify_url), "status": "pending", "expires_at": expiry.isoformat(), "poll_interval": 2}

    def callback(self, state: str, code: str) -> None:
        with self._database().begin() as db:
            row = db.scalar(select(IdentityLoginSession).where(IdentityLoginSession.state == state).with_for_update())
            if row is None or row.kind != "passkey" or row.status != "pending" or _expires_at(row.expires_at) <= _now():
                raise ValueError("通行证登录会话无效或已过期")
            row.authorization_code = code
            row.status = "approved"
            row.updated_at = _now()

    def poll_passkey_session(self, session_id: str) -> tuple[dict[str, Any], AuthSessionView | None]:
        with self._database().begin() as db:
            row = db.scalar(select(IdentityLoginSession).where(IdentityLoginSession.id == session_id).with_for_update())
            if row is None or row.kind != "passkey":
                raise ValueError("通行证登录会话不存在")
            if _expires_at(row.expires_at) <= _now():
                row.status = "expired"
                return {"status": "expired"}, None
            if row.status == "completed":
                did, wallet = row.did or "", row.wallet_address or ""
                view = _session_view(did, wallet, self.settings)
                return self._login_payload(view, did, wallet), view
            if row.status == "pending":
                return {"status": "pending"}, None
            if row.status == "failed":
                return {"status": "failed", "message": row.error_message or "夜莺身份验证失败"}, None
            if row.status != "approved" or not row.authorization_code:
                return {"status": "pending"}, None
            row.status = "exchanging"
            code, verifier, state, request_id = row.authorization_code, row.code_verifier or "", row.state or "", row.request_id or ""
            callback = row.redirect_uri or ""
        try:
            result = _node_post(self.settings, "/api/v1/public/identity/authorize/exchange", {
                "code": code, "appId": self.settings.identity_app_id, "redirectUri": callback,
                "codeVerifier": verifier, "requestId": request_id,
            })
            if result.get("state") != state:
                raise ValueError("夜莺身份授权 state 不匹配")
            did, wallet, _credentials = _verify_exchange_identity(result, self.settings)
        except Exception as exc:  # noqa: BLE001
            with self._database().begin() as db:
                db.execute(update(IdentityLoginSession).where(IdentityLoginSession.id == session_id).values(status="failed", error_message=str(exc)[:1000], authorization_code=None, code_verifier=None, updated_at=_now()))
            message = str(exc) if isinstance(exc, ValueError) else "夜莺身份授权失败"
            return {"status": "failed", "message": message}, None
        with self._database().begin() as db:
            db.execute(update(IdentityLoginSession).where(IdentityLoginSession.id == session_id, IdentityLoginSession.status == "exchanging").values(status="completed", did=did, wallet_address=wallet, authorization_code=None, code_verifier=None, updated_at=_now()))
        view = _session_view(did, wallet, self.settings)
        return {"status": "completed", **self._login_payload(view, did, wallet)}, view

    def _login_payload(self, session: AuthSessionView, did: str, wallet: str) -> dict[str, Any]:
        return {"token": self.auth.encode_session(session), "did": did, "walletAddress": wallet, "wallet_address": wallet,
                "expires_at": session.expires_at, "auth_type": "wallet_identity"}


def _public_origin(settings: Settings) -> str:
    value = (settings.public_base_url or "").strip().rstrip("/")
    if value:
        parsed = urlparse(value)
        return parsed.netloc and f"{parsed.scheme}://{parsed.netloc}" or ""
    return ""


def _normalize_verify_url(node_url: str, verify_url: str) -> str:
    if verify_url.startswith(("http://", "https://")):
        return verify_url
    return node_url.rstrip("/") + "/" + verify_url.lstrip("/")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None
