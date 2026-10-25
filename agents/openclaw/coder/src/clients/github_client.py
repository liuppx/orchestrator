from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.utils.helpers import tail_text


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_app_jwt(config: dict[str, Any]) -> str:
    private_key_path = Path(config["github_private_key_path"])
    private_key = serialization.load_pem_private_key(
        private_key_path.read_bytes(),
        password=None,
    )
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": now - 60,
        "exp": now + 540,
        "iss": config["github_app_id"],
    }
    signing_input = (
        f"{base64url(json.dumps(header, separators=(',', ':')).encode('utf-8'))}."
        f"{base64url(json.dumps(payload, separators=(',', ':')).encode('utf-8'))}"
    )
    signature = private_key.sign(
        signing_input.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{signing_input}.{base64url(signature)}"


def github_request(
    config: dict[str, Any],
    method: str,
    path: str,
    *,
    token: str | None = None,
    jwt_token: str | None = None,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> requests.Response:
    url = f"{config['github_api_url']}{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "coder-issue-bot",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if jwt_token:
        headers["Authorization"] = f"Bearer {jwt_token}"
    if extra_headers:
        headers.update(extra_headers)
    response = requests.request(
        method,
        url,
        headers=headers,
        json=json_body,
        params=params,
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = tail_text(response.text or "", 2000)
        message = str(exc)
        if detail:
            message = f"{message}\n{detail}"
        raise requests.HTTPError(message, response=response) from None
    return response


def get_installation_token(config: dict[str, Any], installation_id: str | None = None) -> str:
    install_id = installation_id or config["github_installation_id"]
    jwt_token = build_app_jwt(config)
    response = github_request(
        config,
        "POST",
        f"/app/installations/{install_id}/access_tokens",
        jwt_token=jwt_token,
        json_body={},
    )
    return response.json()["token"]


def get_repo_info(config: dict[str, Any], token: str, owner: str, repo: str) -> dict[str, Any]:
    return github_request(config, "GET", f"/repos/{owner}/{repo}", token=token).json()


def get_repo_info_optional(
    config: dict[str, Any], token: str, owner: str, repo: str
) -> dict[str, Any] | None:
    try:
        return get_repo_info(config, token, owner, repo)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise


def list_open_issues(
    config: dict[str, Any],
    token: str,
    owner: str,
    repo: str,
    *,
    since: str | None = None,
    etag: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    issue_scan_limit = int(config.get("issue_scan_limit") or 30)
    params = {
        "state": "open",
        "sort": "updated",
        "direction": "desc",
        "per_page": issue_scan_limit,
    }
    if since:
        params["since"] = since

    first_page_headers = {"If-None-Match": etag} if etag else None
    first_response = github_request(
        config,
        "GET",
        f"/repos/{owner}/{repo}/issues",
        token=token,
        params={**params, "page": 1},
        extra_headers=first_page_headers,
    )
    if first_response.status_code == 304:
        return [], etag, True

    issues = list(first_response.json())
    if not since:
        return issues, first_response.headers.get("ETag"), False

    page = 2
    while True:
        response = github_request(
            config,
            "GET",
            f"/repos/{owner}/{repo}/issues",
            token=token,
            params={**params, "page": page},
        )
        batch = response.json()
        if not batch:
            break
        issues.extend(batch)
        if len(batch) < issue_scan_limit:
            break
        page += 1

    return issues, first_response.headers.get("ETag"), False


def list_issue_comments(
    config: dict[str, Any],
    token: str,
    owner: str,
    repo: str,
    issue_number: int,
    *,
    since: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"per_page": 100}
    if since:
        params["since"] = since

    comments: list[dict[str, Any]] = []
    page = 1
    while True:
        response = github_request(
            config,
            "GET",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            token=token,
            params={**params, "page": page},
        )
        batch = response.json()
        if not batch:
            break
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return comments


def create_fork(config: dict[str, Any], token: str, owner: str, repo: str) -> None:
    github_request(
        config,
        "POST",
        f"/repos/{owner}/{repo}/forks",
        token=token,
        json_body={},
    )


def list_pull_requests(
    config: dict[str, Any],
    token: str,
    owner: str,
    repo: str,
    *,
    head: str,
    base: str,
    state: str = "open",
) -> list[dict[str, Any]]:
    response = github_request(
        config,
        "GET",
        f"/repos/{owner}/{repo}/pulls",
        token=token,
        params={"head": head, "base": base, "state": state, "per_page": 10},
    )
    return response.json()


def create_pull_request(
    config: dict[str, Any],
    token: str,
    owner: str,
    repo: str,
    *,
    title: str,
    body: str,
    head: str,
    base: str,
) -> dict[str, Any]:
    return github_request(
        config,
        "POST",
        f"/repos/{owner}/{repo}/pulls",
        token=token,
        json_body={
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "maintainer_can_modify": False,
        },
    ).json()


def comment_issue(
    config: dict[str, Any],
    token: str,
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
) -> None:
    github_request(
        config,
        "POST",
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        token=token,
        json_body={"body": body},
    )


STATUS_LABEL_DEFS = [
    ("issue_label_accepted_name", "issue_label_accepted_color"),
    ("issue_label_in_progress_name", "issue_label_in_progress_color"),
    ("issue_label_pr_ready_name", "issue_label_pr_ready_color"),
]


def ensure_label(
    config: dict[str, Any],
    token: str,
    owner: str,
    repo: str,
    name: str,
    color: str,
) -> None:
    try:
        github_request(
            config,
            "GET",
            f"/repos/{owner}/{repo}/labels/{requests.utils.quote(name, safe='')}",
            token=token,
        )
        return
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            pass
        else:
            raise
    github_request(
        config,
        "POST",
        f"/repos/{owner}/{repo}/labels",
        token=token,
        json_body={"name": name, "color": color},
    )


def set_issue_status_label(
    config: dict[str, Any],
    token: str,
    owner: str,
    repo: str,
    issue_number: int,
    active_label_config_key: str,
) -> None:
    """Set one status label on an issue and remove the other mutually exclusive ones. Non-fatal."""
    try:
        active_name = config.get(active_label_config_key, "")
        active_color = ""
        for name_key, color_key in STATUS_LABEL_DEFS:
            if name_key == active_label_config_key:
                active_color = config.get(color_key, "cccccc")
                break
        if not active_name:
            return

        ensure_label(config, token, owner, repo, active_name, active_color)

        current_labels = github_request(
            config,
            "GET",
            f"/repos/{owner}/{repo}/issues/{issue_number}",
            token=token,
        ).json().get("labels", [])
        current_label_names = {
            str(lbl["name"]) for lbl in current_labels if isinstance(lbl, dict)
        }

        if active_name not in current_label_names:
            github_request(
                config,
                "POST",
                f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
                token=token,
                json_body={"labels": [active_name]},
            )

        for name_key, _ in STATUS_LABEL_DEFS:
            if name_key == active_label_config_key:
                continue
            candidate = config.get(name_key, "")
            if candidate and candidate in current_label_names:
                try:
                    github_request(
                        config,
                        "DELETE",
                        f"/repos/{owner}/{repo}/issues/{issue_number}/labels/{requests.utils.quote(candidate, safe='')}",
                        token=token,
                    )
                except requests.HTTPError:
                    pass
    except Exception as exc:
        print(f"warning: failed to sync issue status label for {owner}/{repo}#{issue_number}: {exc}")
