from src.clients.github_client import base64url


def test_base64url_strips_padding() -> None:
    assert base64url(b"test") == "dGVzdA"
