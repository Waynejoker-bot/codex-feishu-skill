#!/usr/bin/env python3
"""
Feishu user access token helper.

Use the authorization code only once to seed a local refresh token, then reuse
the saved refresh token for silent token rotation.

Examples:
  python3 feishu_user_auth.py
  python3 feishu_user_auth.py --refresh
  python3 feishu_user_auth.py --login
  python3 feishu_user_auth.py --auth-file ~/.codex/skills/feishu/.user_auth.json
  python3 feishu_user_auth.py --redirect-uri 'https://acnhb5kgvgtx.feishu.cn/wiki/Q4dTwYHD6i8IYckQ6dDc2eDZnDf' --print-auth-url
  python3 feishu_user_auth.py --redirect-uri 'https://acnhb5kgvgtx.feishu.cn/wiki/Q4dTwYHD6i8IYckQ6dDc2eDZnDf' --exchange-redirect-url 'https://acnhb5kgvgtx.feishu.cn/wiki/Q4dTwYHD6i8IYckQ6dDc2eDZnDf?code=xxx&state=benchmark_auth'
"""

import argparse
import http.server
import json
import os
import sys
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".user_auth.json")
CALLBACK_PORT = 19876
LOCAL_REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}/callback"


def load_env(env_file=None):
    """Load FEISHU_APP_ID / FEISHU_APP_SECRET from .env."""
    target = env_file or ENV_FILE
    if target and os.path.exists(target):
        with open(target) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")

    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        print("Error: FEISHU_APP_ID / FEISHU_APP_SECRET not found", file=sys.stderr)
        sys.exit(1)
    return app_id, app_secret


def resolve_cache_path(raw_path):
    return os.path.abspath(os.path.expanduser(raw_path or TOKEN_CACHE))


def atomic_write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)


def build_oauth_url(app_id, redirect_uri, state, scopes=None):
    query = {
        "client_id": app_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if scopes:
        if isinstance(scopes, (list, tuple)):
            query["scope"] = " ".join([s for s in scopes if s])
        else:
            query["scope"] = str(scopes)
    return "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urllib.parse.urlencode(query)


def get_app_access_token(app_id, app_secret):
    """Get app_access_token for exchanging and refreshing user tokens."""
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    if result.get("code") != 0:
        print(f"Error getting app_access_token: {result}", file=sys.stderr)
        sys.exit(1)
    return result["app_access_token"]


def fetch_user_info(access_token):
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/authen/v1/user_info",
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    if result.get("code") != 0:
        raise RuntimeError(f"Failed to fetch user info: {result}")
    return result.get("data", {})


def load_cache(cache_path):
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def parse_expiry(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return float(raw_value)
    if isinstance(raw_value, str):
        try:
            return float(raw_value)
        except ValueError:
            normalized = raw_value.strip()
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    return None


def load_cached_token(cache_path):
    cache = load_cache(cache_path)
    if not cache:
        return None
    expires_at = parse_expiry(cache.get("expires_at"))
    if not expires_at:
        expires_at = parse_expiry(cache.get("access_expires_at"))
    if expires_at and time.time() < expires_at:
        return cache.get("access_token")
    return None


def load_refresh_token(cache_path):
    cache = load_cache(cache_path)
    if not cache:
        return None
    refresh_expires_at = parse_expiry(cache.get("refresh_expires_at"))
    if refresh_expires_at and time.time() >= refresh_expires_at:
        return None
    return cache.get("refresh_token")


def save_token(cache_path, access_token, refresh_token, expires_in, refresh_expires_in=None, user_info=None):
    user_info = user_info or {}
    payload = {
        "name": user_info.get("name"),
        "en_name": user_info.get("en_name"),
        "open_id": user_info.get("open_id"),
        "union_id": user_info.get("union_id"),
        "tenant_key": user_info.get("tenant_key"),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "expires_at": time.time() + max(int(expires_in) - 300, 0),
        "access_expires_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + max(int(expires_in), 0)),
        ),
        "refresh_expires_in": refresh_expires_in,
        "refresh_expires_at": (
            time.time() + max(int(refresh_expires_in) - 300, 0)
            if refresh_expires_in
            else None
        ),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    atomic_write_json(cache_path, payload)


def refresh_user_token(app_access_token, refresh_token, cache_path):
    """Refresh user_access_token with refresh_token."""
    data = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/authen/v1/oidc/refresh_access_token",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {app_access_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get("code") == 0:
            d = result["data"]
            user_info = fetch_user_info(d["access_token"])
            save_token(
                cache_path,
                d["access_token"],
                d.get("refresh_token", refresh_token),
                d.get("expires_in", 7200),
                d.get("refresh_expires_in"),
                user_info=user_info,
            )
            return d["access_token"]
    except Exception as e:
        print(f"Refresh failed: {e}", file=sys.stderr)
    return None


def exchange_code_for_token(app_access_token, code, cache_path):
    data = json.dumps({
        "grant_type": "authorization_code",
        "code": code,
    }).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {app_access_token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())

    if result.get("code") != 0:
        print(f"Error exchanging code: {json.dumps(result, ensure_ascii=False)}", file=sys.stderr)
        sys.exit(1)

    d = result["data"]
    user_info = fetch_user_info(d["access_token"])
    save_token(
        cache_path,
        d["access_token"],
        d.get("refresh_token", ""),
        d.get("expires_in", 7200),
        d.get("refresh_expires_in"),
        user_info=user_info,
    )
    return d["access_token"]


def extract_code(raw_value):
    if not raw_value:
        return None
    parsed = urllib.parse.urlparse(raw_value)
    if parsed.scheme and parsed.netloc:
        qs = urllib.parse.parse_qs(parsed.query)
        return qs.get("code", [None])[0]
    return raw_value


def is_local_redirect_uri(redirect_uri):
    parsed = urllib.parse.urlparse(redirect_uri)
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}


def oauth_login_local(app_id, app_access_token, redirect_uri, state, cache_path, scopes=None):
    """OAuth login via local callback URI."""
    auth_code = None
    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    callback_host = parsed_redirect.hostname or "127.0.0.1"
    callback_port = parsed_redirect.port or CALLBACK_PORT
    callback_path = parsed_redirect.path or "/callback"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != callback_path:
                self.send_response(404)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(parsed.query)
            auth_code = params.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Authorization received</h2></body></html>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer((callback_host, callback_port), Handler)
    oauth_url = build_oauth_url(app_id, redirect_uri, state, scopes=scopes)

    print("Opening browser for login...", file=sys.stderr)
    print(f"If browser doesn't open, visit: {oauth_url}", file=sys.stderr)
    webbrowser.open(oauth_url)

    server.timeout = 180
    server.handle_request()
    server.server_close()

    if not auth_code:
        print("Error: No auth code received (timeout or user cancelled)", file=sys.stderr)
        sys.exit(1)

    return exchange_code_for_token(app_access_token, auth_code, cache_path)


def print_manual_reauth_instructions(app_id, redirect_uri, state, scopes=None):
    auth_url = build_oauth_url(app_id, redirect_uri, state, scopes=scopes)
    print("Re-authorization required.", file=sys.stderr)
    print("1. Open this URL in your browser:", file=sys.stderr)
    print(auth_url, file=sys.stderr)
    print("2. Complete consent.", file=sys.stderr)
    print(
        "3. Re-run this script with --exchange-redirect-url '<final redirected URL>'.",
        file=sys.stderr,
    )


def main():
    parser = argparse.ArgumentParser(description="Get a reusable Feishu user_access_token")
    parser.add_argument("--refresh", action="store_true", help="Force refresh token")
    parser.add_argument("--login", action="store_true", help="Force re-login via OAuth")
    parser.add_argument("--auth-file", default=TOKEN_CACHE, help="Token cache path")
    parser.add_argument("--env-file", help="Optional .env file containing FEISHU_APP_ID / FEISHU_APP_SECRET")
    parser.add_argument("--redirect-uri", default=os.environ.get("FEISHU_REDIRECT_URI", LOCAL_REDIRECT_URI))
    parser.add_argument("--state", default="benchmark_auth", help="OAuth state value")
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        help="User scopes to request. Repeat the flag or pass a space-separated string.",
    )
    parser.add_argument("--print-auth-url", action="store_true", help="Print the OAuth URL and exit")
    parser.add_argument(
        "--exchange-redirect-url",
        help="Exchange the full redirected URL (or just the code) for tokens and save them locally",
    )
    args = parser.parse_args()

    app_id, app_secret = load_env(args.env_file)
    cache_path = resolve_cache_path(args.auth_file)

    scopes = []
    for item in args.scope:
        scopes.extend([part for part in str(item).split() if part])

    if args.print_auth_url:
        print(build_oauth_url(app_id, args.redirect_uri, args.state, scopes=scopes))
        return

    app_token = None

    if args.exchange_redirect_url:
        code = extract_code(args.exchange_redirect_url)
        if not code:
            print("Error: no code found in --exchange-redirect-url", file=sys.stderr)
            sys.exit(1)
        app_token = get_app_access_token(app_id, app_secret)
        print(exchange_code_for_token(app_token, code, cache_path))
        return

    if args.login:
        app_token = get_app_access_token(app_id, app_secret)
        if is_local_redirect_uri(args.redirect_uri):
            print(oauth_login_local(app_id, app_token, args.redirect_uri, args.state, cache_path, scopes=scopes))
            return
        print_manual_reauth_instructions(app_id, args.redirect_uri, args.state, scopes=scopes)
        sys.exit(2)

    if not args.refresh:
        cached = load_cached_token(cache_path)
        if cached:
            print(cached)
            return

    app_token = get_app_access_token(app_id, app_secret)
    refresh_token = load_refresh_token(cache_path)
    if refresh_token:
        token = refresh_user_token(app_token, refresh_token, cache_path)
        if token:
            print(token)
            return

    if is_local_redirect_uri(args.redirect_uri):
        print(oauth_login_local(app_id, app_token, args.redirect_uri, args.state, cache_path, scopes=scopes))
        return

    print_manual_reauth_instructions(app_id, args.redirect_uri, args.state, scopes=scopes)
    sys.exit(2)


if __name__ == "__main__":
    main()
