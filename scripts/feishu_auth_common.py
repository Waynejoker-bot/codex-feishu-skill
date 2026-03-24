#!/usr/bin/env python3
"""Shared auth helpers for the unified Feishu skill."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://open.feishu.cn/open-apis"
DEFAULT_USER_AUTH_FILE = Path(__file__).resolve().parent.parent / ".user_auth.json"
ACCESS_TOKEN_REFRESH_LEEWAY_SECONDS = 300


class FeishuApiError(RuntimeError):
    """Raised when a Feishu API call fails."""


def load_env_file(env_file: Optional[str]) -> None:
    if not env_file:
        return
    path = Path(env_file).expanduser().resolve()
    if not path.exists():
        raise FeishuApiError(f"Env file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def _parse_response_json(raw: str, path: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FeishuApiError(f"Invalid JSON from {path}: {raw}") from exc
    if parsed.get("code") != 0:
        raise FeishuApiError(
            f"Feishu API error code={parsed.get('code')} msg={parsed.get('msg')} "
            f"path={path} response={json.dumps(parsed, ensure_ascii=False)}"
        )
    return parsed


def request_json(
    method: str,
    path: str,
    token: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    query: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}"
    if query:
        query = {k: v for k, v in query.items() if v is not None}
        if query:
            url = f"{url}?{urlencode(query)}"
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, method=method, headers=headers, data=data)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise FeishuApiError(f"HTTP {exc.code} {path}: {body}") from exc
    except URLError as exc:
        raise FeishuApiError(f"Network error calling {path}: {exc}") from exc
    return _parse_response_json(raw, path)


def get_app_credentials(
    app_id: Optional[str],
    app_secret: Optional[str],
    env_file: Optional[str] = None,
) -> Tuple[str, str]:
    load_env_file(env_file)
    resolved_app_id = app_id or os.environ.get("FEISHU_APP_ID")
    resolved_app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET")
    if not resolved_app_id or not resolved_app_secret:
        raise FeishuApiError(
            "Missing FEISHU_APP_ID / FEISHU_APP_SECRET. "
            "Pass --app-id/--app-secret or --env-file."
        )
    return resolved_app_id, resolved_app_secret


def get_app_access_token(app_id: str, app_secret: str) -> str:
    resp = request_json(
        method="POST",
        path="/auth/v3/app_access_token/internal",
        payload={"app_id": app_id, "app_secret": app_secret},
    )
    token = resp.get("app_access_token") or resp.get("tenant_access_token")
    if not token:
        raise FeishuApiError(f"app_access_token missing in response: {resp}")
    return token


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(float(value))
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _compute_expiry(saved_at: datetime, expires_in: Any) -> Optional[str]:
    seconds = _coerce_positive_int(expires_in)
    if seconds is None:
        return None
    return _isoformat_utc(saved_at + timedelta(seconds=seconds))


def _parse_iso8601_utc(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _token_expiring_soon(expires_at: Any, leeway_seconds: int = ACCESS_TOKEN_REFRESH_LEEWAY_SECONDS) -> bool:
    parsed = _parse_iso8601_utc(expires_at)
    if parsed is None:
        return True
    return parsed <= (_utc_now() + timedelta(seconds=leeway_seconds))


def load_user_auth(file_path: Path) -> Optional[Dict[str, Any]]:
    if not file_path.exists():
        return None
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise FeishuApiError(f"Failed to read user auth file {file_path}: {exc}") from exc
    refresh_token = raw.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return None
    return raw


def save_user_auth(file_path: Path, auth_data: Dict[str, Any]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    saved_at = _utc_now()
    payload = {
        "name": auth_data.get("name"),
        "en_name": auth_data.get("en_name"),
        "open_id": auth_data.get("open_id"),
        "union_id": auth_data.get("union_id"),
        "tenant_key": auth_data.get("tenant_key"),
        "access_token": auth_data.get("access_token"),
        "expires_in": auth_data.get("expires_in"),
        "access_expires_at": auth_data.get("access_expires_at")
        or _compute_expiry(saved_at, auth_data.get("expires_in")),
        "refresh_token": auth_data.get("refresh_token"),
        "refresh_expires_in": auth_data.get("refresh_expires_in"),
        "refresh_expires_at": auth_data.get("refresh_expires_at")
        or _compute_expiry(saved_at, auth_data.get("refresh_expires_in")),
        "saved_at": _isoformat_utc(saved_at),
    }
    temp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp_path, 0o600)
    temp_path.replace(file_path)


def refresh_user_access_token(app_access_token: str, refresh_token: str) -> Dict[str, Any]:
    resp = request_json(
        method="POST",
        path="/authen/v1/oidc/refresh_access_token",
        token=app_access_token,
        payload={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    data = resp.get("data")
    if not isinstance(data, dict) or not data.get("access_token"):
        raise FeishuApiError(f"user access token missing in refresh response: {resp}")
    return data


def fetch_user_info(access_token: str) -> Dict[str, Any]:
    resp = request_json(method="GET", path="/authen/v1/user_info", token=access_token)
    data = resp.get("data")
    if not isinstance(data, dict):
        raise FeishuApiError(f"user_info payload missing data: {resp}")
    return data


def resolve_access_token(
    auth_mode: str,
    app_id: str,
    app_secret: str,
    user_auth_file: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    if auth_mode == "app":
        return get_app_access_token(app_id, app_secret), None

    user_auth_path = Path(user_auth_file or DEFAULT_USER_AUTH_FILE).expanduser().resolve()
    user_auth = load_user_auth(user_auth_path)
    if not user_auth:
        raise FeishuApiError(
            f"User auth file missing or invalid: {user_auth_path}. "
            "Run scripts/feishu_user_auth.py first."
        )

    cached_access_token = user_auth.get("access_token")
    if isinstance(cached_access_token, str) and cached_access_token and not _token_expiring_soon(
        user_auth.get("access_expires_at")
    ):
        return cached_access_token, user_auth.get("name")

    app_access_token = get_app_access_token(app_id, app_secret)
    refreshed = refresh_user_access_token(app_access_token, user_auth["refresh_token"])
    if "refresh_token" not in refreshed:
        refreshed["refresh_token"] = user_auth.get("refresh_token")
    try:
        user_info = fetch_user_info(refreshed["access_token"])
    except FeishuApiError:
        user_info = {}
    for field in ("name", "en_name", "open_id", "union_id", "tenant_key"):
        if field not in refreshed and field in user_info:
            refreshed[field] = user_info[field]
        if field not in refreshed and field in user_auth:
            refreshed[field] = user_auth[field]
    save_user_auth(user_auth_path, refreshed)
    return refreshed["access_token"], refreshed.get("name")
