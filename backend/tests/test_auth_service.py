from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from eth_account import Account
from eth_account.messages import encode_defunct

from hub.services.auth import AuthSessionService


class AuthSessionServiceTest(unittest.TestCase):
    def build_service(self, root: Path) -> AuthSessionService:
        return AuthSessionService(
            secret="test-secret",
            ttl_seconds=3600,
            challenge_ttl_seconds=300,
            nonce_store_path=root / "consumed_challenges.json",
            public_base_url=None,
            secure_cookie_mode="auto",
        )

    def build_request(self) -> SimpleNamespace:
        return SimpleNamespace(
            base_url="http://127.0.0.1:3900/",
            headers={"host": "127.0.0.1:3900"},
            url=SimpleNamespace(hostname="127.0.0.1", scheme="http"),
        )

    def test_challenge_verify_and_replay_protection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = self.build_service(Path(tmpdir))
            account = Account.create()
            wallet_id = account.address

            challenge_view = service.create_challenge(
                self.build_request(),
                SimpleNamespace(wallet_id=wallet_id, chain_id="1"),
            )
            signed = Account.sign_message(
                encode_defunct(text=challenge_view.challenge),
                private_key=account.key,
            )

            session = service.verify_wallet_session(
                SimpleNamespace(
                    wallet_id=wallet_id,
                    chain_id="1",
                    challenge=challenge_view.challenge,
                    challenge_token=challenge_view.challenge_token,
                    signature=signed.signature.to_0x_hex(),
                    ucan_session=None,
                    ucan_signature=None,
                )
            )

            self.assertEqual(session.wallet_id, wallet_id)
            self.assertEqual(session.chain_id, "1")
            self.assertIn("Version: 1", challenge_view.challenge)
            self.assertIn("URI: http://127.0.0.1:3900/", challenge_view.challenge)
            self.assertIn("Chain ID: 1", challenge_view.challenge)

            with self.assertRaisesRegex(ValueError, "already used"):
                service.verify_wallet_session(
                    SimpleNamespace(
                        wallet_id=wallet_id,
                        chain_id="1",
                        challenge=challenge_view.challenge,
                        challenge_token=challenge_view.challenge_token,
                        signature=signed.signature.to_0x_hex(),
                        ucan_session=None,
                        ucan_signature=None,
                    )
                )

    def test_public_base_url_and_secure_cookie_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = AuthSessionService(
                secret="test-secret",
                ttl_seconds=3600,
                challenge_ttl_seconds=300,
                nonce_store_path=Path(tmpdir) / "consumed_challenges.json",
                public_base_url="https://hub.example.com",
                secure_cookie_mode="auto",
            )
            request = SimpleNamespace(
                base_url="http://127.0.0.1:3900/",
                headers={"host": "127.0.0.1:3900", "x-forwarded-proto": "http"},
                url=SimpleNamespace(hostname="127.0.0.1", scheme="http"),
            )
            challenge_view = service.create_challenge(
                request,
                SimpleNamespace(wallet_id=Account.create().address, chain_id="56"),
            )
            self.assertIn("hub.example.com wants you to sign in", challenge_view.challenge)
            self.assertIn("URI: https://hub.example.com/", challenge_view.challenge)
            self.assertFalse(service.should_secure_cookie(request))

    def test_rejects_signature_from_other_wallet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = self.build_service(Path(tmpdir))
            owner = Account.create()
            attacker = Account.create()

            challenge_view = service.create_challenge(
                self.build_request(),
                SimpleNamespace(wallet_id=owner.address, chain_id="1"),
            )
            signed = Account.sign_message(
                encode_defunct(text=challenge_view.challenge),
                private_key=attacker.key,
            )

            with self.assertRaisesRegex(ValueError, "does not match wallet"):
                service.verify_wallet_session(
                    SimpleNamespace(
                        wallet_id=owner.address,
                        chain_id="1",
                        challenge=challenge_view.challenge,
                        challenge_token=challenge_view.challenge_token,
                        signature=signed.signature.to_0x_hex(),
                        ucan_session=None,
                        ucan_signature=None,
                    )
                )


if __name__ == "__main__":
    unittest.main()
