from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account

from orchestrator.config import Settings
from orchestrator.services.auth import AuthSessionService
from orchestrator.services.identity_auth import IDENTITY_SCOPES, IdentityAuthService, _canonical
from orchestrator.storage import IdentityLoginSession, sessions_for


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def sign_json(private_key: Ed25519PrivateKey, value: dict) -> str:
    return b64url(private_key.sign(_canonical(value).encode("utf-8")))


def make_identity(directory: Path) -> tuple[str, str, dict]:
    did = "did:yeying:wid_12345678901234567890123456789012"
    wallet = Account.create().address
    identity_key = Ed25519PrivateKey.generate()
    issuer_key = Ed25519PrivateKey.generate()
    identity_public = b64url(identity_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    issuer_public = b64url(issuer_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    document_unsigned = {
        "version": 1,
        "id": did,
        "walletIdentityId": "wid_test",
        "controllers": [{
            "controllerId": "key-1",
            "kind": "ed25519",
            "publicKey": identity_public,
            "algorithm": "Ed25519",
            "purposes": ["manage", "authentication"],
            "status": "active",
        }],
    }
    document = {
        **document_unsigned,
        "proof": {
            "type": "YeyingIdentityDocumentProofV1",
            "verificationMethod": f"{did}#key-1",
            "purpose": "manage",
            "proofValue": sign_json(identity_key, document_unsigned),
        },
    }

    now = datetime.now(UTC)
    issuer = "https://node.example/identity"

    def credential(kind: str, subject: dict) -> str:
        header = b64url(json.dumps({"alg": "EdDSA", "kid": "issuer-1"}, separators=(",", ":")).encode())
        claims = {
            "iss": issuer,
            "sub": did,
            "jti": f"credential-{kind}",
            "nbf": int(now.timestamp()) - 5,
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "vc": {"type": ["VerifiableCredential", kind], "credentialSubject": {"id": did, **subject}},
        }
        body = b64url(json.dumps(claims, separators=(",", ":")).encode())
        signing_input = f"{header}.{body}"
        return f"{signing_input}.{b64url(issuer_key.sign(signing_input.encode('ascii')))}"

    tokens = [
        credential("WalletAccountCredential", {"address": wallet, "chainKey": "eip155:1"}),
        credential("UsernameCredential", {"username": "alice"}),
        credential("EmailCredential", {"email": "alice@example.test"}),
    ]
    presentation_unsigned = {
        "version": 1,
        "holder": did,
        "audience": "https://orchestrator.example.test",
        "nonce": "nonce-test",
        "issuedAt": now.isoformat(),
        "expiresAt": (now + timedelta(minutes=2)).isoformat(),
        "scopes": IDENTITY_SCOPES,
        "identityDocument": document,
        "walletProof": {"address": wallet, "chainKey": "eip155:1"},
        "credentials": tokens,
    }
    presentation = {
        **presentation_unsigned,
        "proof": {
            "type": "YeyingIdentityPresentationProofV1",
            "verificationMethod": f"{did}#key-1",
            "purpose": "authentication",
            "proofValue": sign_json(identity_key, presentation_unsigned),
        },
    }

    metadata = json.dumps({"issuer": issuer}, separators=(",", ":")).encode()
    jwks = json.dumps({"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "issuer-1", "x": issuer_public}]}, separators=(",", ":")).encode()
    (directory / "issuer-metadata.json").write_bytes(metadata)
    (directory / "jwks.json").write_bytes(jwks)
    manifest = {
        "issuer": issuer,
        "metadataSha256": hashlib.sha256(metadata).hexdigest(),
        "jwksSha256": hashlib.sha256(jwks).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    exchange = {
        "did": did,
        "walletAddress": wallet,
        "scopes": IDENTITY_SCOPES,
        "credentials": [{"type": kind, "credential": token} for kind, token in zip(
            ("WalletAccountCredential", "UsernameCredential", "EmailCredential"), tokens, strict=True
        )],
    }
    return did, wallet, {"presentation": presentation, "exchange": exchange, "identity_key": identity_key}


class IdentityAuthServiceTest(unittest.TestCase):
    def build_service(self, root: Path, trust_dir: Path) -> IdentityAuthService:
        database_url = f"sqlite:///{root / 'identity.db'}"
        from orchestrator.db import init_database

        init_database(database_url)
        settings = Settings(
            _env_file=None,
            HUB_DATABASE_URL=database_url,
            HUB_SESSION_SECRET="test-secret",
            HUB_SESSION_TTL_SECONDS=3600,
            HUB_CHALLENGE_TTL_SECONDS=300,
            IDENTITY_NODE_URL="https://node.example",
            IDENTITY_APP_ID="orchestrator-test",
            IDENTITY_TRUST_DIR=trust_dir,
            IDENTITY_REDIRECT_URI="https://orchestrator.example.test/api/v1/public/auth/passport/callback",
        )
        auth = AuthSessionService(
            secret=settings.session_secret,
            ttl_seconds=settings.session_ttl_seconds,
            challenge_ttl_seconds=settings.challenge_ttl_seconds,
            nonce_store_path=root / "consumed.json",
            public_base_url=None,
            secure_cookie_mode="never",
            database_url=database_url,
        )
        return IdentityAuthService(settings, auth)

    def test_wallet_identity_session_verifies_presentation_and_is_one_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trust_dir = root / "trust"
            trust_dir.mkdir()
            did, wallet, identity = make_identity(trust_dir)
            service = self.build_service(root, trust_dir)
            created = service.create_wallet_session("https://orchestrator.example.test")
            presentation = {**identity["presentation"], "audience": created["audience"], "nonce": created["nonce"]}
            unsigned = {key: value for key, value in presentation.items() if key != "proof"}
            presentation["proof"] = {
                **presentation["proof"],
                "proofValue": sign_json(identity["identity_key"], unsigned),
            }
            session, resolved_did = service.verify_wallet_session(created["session_id"], presentation)
            self.assertEqual(resolved_did, did)
            self.assertEqual(session.wallet_id.lower(), wallet.lower())
            with self.assertRaisesRegex(ValueError, "无效或已过期"):
                service.verify_wallet_session(created["session_id"], presentation)

    def test_passkey_callback_exchanges_pkce_code_and_returns_wallet_did(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trust_dir = root / "trust"
            trust_dir.mkdir()
            did, wallet, identity = make_identity(trust_dir)
            service = self.build_service(root, trust_dir)
            created_data = {
                "requestId": "request-1",
                "verifyUrl": "https://node.example/identity/authorize?requestId=request-1",
                "audience": "https://orchestrator.example.test",
                "nonce": "nonce-node",
                "scopes": IDENTITY_SCOPES,
                "expiresAt": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            }
            with patch("orchestrator.services.identity_auth._node_post", return_value=created_data) as node_post:
                created = service.create_passkey_session("https://orchestrator.example.test/api/v1/public/auth/passport/callback")
            self.assertNotIn("code_verifier", created)
            self.assertEqual(node_post.call_args.args[1], "/api/v1/public/identity/authorize/request")
            with sessions_for(service.settings.database_url).begin() as db:
                row = db.get(IdentityLoginSession, created["session_id"])
                state = row.state
                verifier = row.code_verifier
            service.callback(state or "", "single-use-code")
            with self.assertRaisesRegex(ValueError, "无效或已过期"):
                service.callback(state or "", "replayed-code")
            exchange = {**identity["exchange"], "state": state}
            with patch("orchestrator.services.identity_auth._node_post", return_value=exchange) as node_post:
                result, session = service.poll_passkey_session(created["session_id"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["did"], did)
            self.assertEqual(result["walletAddress"], wallet)
            self.assertIsNotNone(session)
            self.assertEqual(node_post.call_args.args[1], "/api/v1/public/identity/authorize/exchange")
            self.assertEqual(node_post.call_args.args[2]["codeVerifier"], verifier)
            self.assertEqual(node_post.call_args.args[2]["redirectUri"], service.settings.identity_redirect_uri)


if __name__ == "__main__":
    unittest.main()
