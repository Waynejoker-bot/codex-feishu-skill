#!/usr/bin/env python3
"""Create or update a Feishu docx document and return the document URL."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen

BASE_URL = "https://open.feishu.cn/open-apis"
LOCAL_IMAGE_PLACEHOLDER_HOST = "https://feishu-local-image.invalid"
DEFAULT_USER_AUTH_FILE = Path(__file__).resolve().parent.parent / ".user_auth.json"
ACCESS_TOKEN_REFRESH_LEEWAY_SECONDS = 300

DEFAULT_APP_ID = ""
DEFAULT_APP_SECRET = ""


class FeishuApiError(RuntimeError):
    """Raised when a Feishu API request fails."""


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
            "Feishu API error "
            f"code={parsed.get('code')} msg={parsed.get('msg')} path={path} "
            f"response={json.dumps(parsed, ensure_ascii=False)}"
        )
    return parsed


def _request_json(
    method: str,
    path: str,
    token: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    query: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}"
    if query:
        query_items = {k: v for k, v in query.items() if v is not None}
        if query_items:
            url = f"{url}?{urlencode(query_items)}"

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


def get_app_access_token(app_id: str, app_secret: str) -> str:
    resp = _request_json(
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
    if isinstance(value, str):
        try:
            parsed = int(value)
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
    resp = _request_json(
        method="POST",
        path="/authen/v1/oidc/refresh_access_token",
        token=app_access_token,
        payload={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    data = resp.get("data")
    if not isinstance(data, dict) or not data.get("access_token"):
        raise FeishuApiError(f"user access token missing in refresh response: {resp}")
    return data


def resolve_api_tokens(args: argparse.Namespace) -> Tuple[str, str, Optional[str]]:
    app_access_token = get_app_access_token(args.app_id, args.app_secret)
    user_auth_path = Path(args.user_auth_file).expanduser().resolve()
    user_auth = load_user_auth(user_auth_path)
    if not user_auth:
        return app_access_token, app_access_token, None

    cached_access_token = user_auth.get("access_token")
    if isinstance(cached_access_token, str) and cached_access_token and not _token_expiring_soon(
        user_auth.get("access_expires_at")
    ):
        user_name = user_auth.get("name")
        if not isinstance(user_name, str):
            user_name = None
        return cached_access_token, app_access_token, user_name

    try:
        refreshed = refresh_user_access_token(app_access_token, user_auth["refresh_token"])
    except FeishuApiError as exc:
        raise FeishuApiError(
            f"Failed to refresh saved Feishu user auth from {user_auth_path}. "
            "Re-authorize once to reseed the local token set. "
            f"Original error: {exc}"
        ) from exc
    if "refresh_token" not in refreshed:
        refreshed["refresh_token"] = user_auth.get("refresh_token")
    for field in ("name", "en_name", "open_id", "union_id", "tenant_key"):
        if field not in refreshed and field in user_auth:
            refreshed[field] = user_auth[field]
    save_user_auth(user_auth_path, refreshed)
    user_name = refreshed.get("name")
    if not isinstance(user_name, str):
        user_name = None
    return refreshed["access_token"], app_access_token, user_name


def create_document(token: str, title: str, folder_token: str) -> Tuple[str, int]:
    payload: Dict[str, Any] = {"title": title}
    if folder_token:
        payload["folder_token"] = folder_token

    resp = _request_json(
        method="POST",
        path="/docx/v1/documents",
        token=token,
        payload=payload,
    )
    doc = resp["data"]["document"]
    return doc["document_id"], int(doc.get("revision_id", 0))


def _split_markdown_link_target(raw_target: str) -> Tuple[str, str]:
    target = raw_target.strip()
    if not target:
        return "", ""

    if target.startswith("<"):
        closing = target.find(">")
        if closing != -1:
            return target[1:closing], target[closing + 1 :].strip()

    parts = target.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()


def _resolve_local_image_path(target: str, content_base_dir: Path) -> Optional[str]:
    normalized = target.strip()
    if not normalized:
        return None

    if normalized.startswith("http://") or normalized.startswith("https://") or normalized.startswith("data:"):
        return None

    if normalized.startswith("file://"):
        parsed = urlparse(normalized)
        local_path = unquote(parsed.path or "")
        if os.name == "nt" and local_path.startswith("/"):
            local_path = local_path[1:]
        candidate = Path(local_path)
    else:
        candidate = Path(os.path.expanduser(normalized))
        if not candidate.is_absolute():
            candidate = (content_base_dir / candidate).resolve()

    if candidate.is_file():
        return str(candidate)
    return None


def rewrite_markdown_with_local_image_placeholders(
    markdown: str,
    content_base_dir: Path,
) -> Tuple[str, Dict[str, str]]:
    pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
    placeholder_to_local: Dict[str, str] = {}

    def _replace(match: re.Match[str]) -> str:
        alt_text = match.group(1)
        original_target = match.group(2)
        link_target, tail = _split_markdown_link_target(original_target)
        local_path = _resolve_local_image_path(link_target, content_base_dir)
        if not local_path:
            # Some markdown links include unescaped spaces in local paths.
            local_path = _resolve_local_image_path(original_target, content_base_dir)
            if local_path:
                tail = ""
        if not local_path:
            return match.group(0)

        suffix = Path(local_path).suffix.lower() or ".img"
        placeholder = f"{LOCAL_IMAGE_PLACEHOLDER_HOST}/{uuid.uuid4().hex}{suffix}"
        placeholder_to_local[placeholder] = local_path
        new_target = placeholder if not tail else f"{placeholder} {tail}"
        return f"![{alt_text}]({new_target})"

    rewritten = pattern.sub(_replace, markdown)
    return rewritten, placeholder_to_local


def convert_markdown_to_blocks(
    token: str,
    markdown: str,
) -> Tuple[List[str], List[Dict[str, Any]], Dict[str, str]]:
    resp = _request_json(
        method="POST",
        path="/docx/v1/documents/blocks/convert",
        token=token,
        payload={"content_type": "markdown", "content": markdown},
    )
    data = resp["data"]

    block_id_to_image_url: Dict[str, str] = {}
    for item in data.get("block_id_to_image_urls", []):
        block_id = item.get("block_id")
        image_url = item.get("image_url")
        if isinstance(block_id, str) and isinstance(image_url, str):
            block_id_to_image_url[block_id] = image_url

    return data["first_level_block_ids"], data["blocks"], block_id_to_image_url


def _sanitize_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for block in blocks:
        current = dict(block)
        current.pop("revision_id", None)
        current.pop("parent_id", None)
        table = current.get("table")
        if isinstance(table, dict):
            prop = table.get("property")
            if isinstance(prop, dict):
                prop.pop("merge_info", None)
        cleaned.append(current)
    return cleaned


def insert_descendants(
    token: str,
    document_id: str,
    first_level_block_ids: List[str],
    blocks: List[Dict[str, Any]],
    index: Optional[int],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "children_id": first_level_block_ids,
        "descendants": _sanitize_blocks(blocks),
    }
    if index is not None:
        payload["index"] = index

    resp = _request_json(
        method="POST",
        path=f"/docx/v1/documents/{document_id}/blocks/{document_id}/descendant",
        token=token,
        payload=payload,
    )
    return resp["data"]


def list_document_root_children(token: str, document_id: str, page_size: int = 500) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None

    while True:
        query: Dict[str, Any] = {"page_size": page_size}
        if page_token:
            query["page_token"] = page_token

        resp = _request_json(
            method="GET",
            path=f"/docx/v1/documents/{document_id}/blocks/{document_id}/children",
            token=token,
            query=query,
        )
        data = resp.get("data", {})
        page_items = data.get("items", [])
        if isinstance(page_items, list):
            items.extend(page_items)

        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break

    return items


def clear_document_root_children(token: str, document_id: str) -> int:
    latest_revision = 0
    while True:
        items = list_document_root_children(token, document_id, page_size=500)
        if not items:
            break

        resp = _request_json(
            method="DELETE",
            path=f"/docx/v1/documents/{document_id}/blocks/{document_id}/children/batch_delete",
            token=token,
            payload={
                "start_index": 0,
                # end_index is exclusive in Feishu API.
                "end_index": len(items),
            },
            query={"client_token": str(uuid.uuid4())},
        )
        latest_revision = int(resp.get("data", {}).get("document_revision_id", latest_revision))

    return latest_revision


def _guess_image_suffix(url: str, content_type: Optional[str]) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix:
        return suffix

    if content_type:
        mime_type = content_type.split(";", 1)[0].strip().lower()
        guessed = mimetypes.guess_extension(mime_type)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed

    return ".img"


def download_remote_image(url: str) -> str:
    suffix = _guess_image_suffix(url, None)

    req = Request(url, headers={"User-Agent": "feishu-skill/1.0"})
    try:
        with urlopen(req, timeout=45) as resp:
            content = resp.read()
            content_type = resp.headers.get("Content-Type")
        suffix = _guess_image_suffix(url, content_type)
        fd, temp_path = tempfile.mkstemp(prefix="feishu_doc_writer_img_", suffix=suffix)
        os.close(fd)
        with open(temp_path, "wb") as file_obj:
            file_obj.write(content)
        return temp_path
    except (HTTPError, URLError, OSError):
        pass

    # Fallback for sites with stricter TLS behavior.
    fd, temp_path = tempfile.mkstemp(prefix="feishu_doc_writer_img_", suffix=suffix)
    os.close(fd)
    try:
        subprocess.run(
            [
                "curl",
                "-fsSL",
                "--retry",
                "2",
                "--connect-timeout",
                "20",
                "--max-time",
                "90",
                "-o",
                temp_path,
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return temp_path
    except subprocess.CalledProcessError as exc:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        stderr = (exc.stderr or "").strip()
        raise FeishuApiError(f"Failed to download image {url} via curl fallback: {stderr}") from exc


def upload_docx_image(token: str, file_path: str, parent_node: str, parent_type: str = "docx_image") -> str:
    with open(file_path, "rb") as file_obj:
        file_bytes = file_obj.read()

    boundary = f"----FeishuFormBoundary{uuid.uuid4().hex}"
    file_name = Path(file_path).name
    content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

    fields = {
        "file_name": file_name,
        "parent_type": parent_type,
        "parent_node": parent_node,
        "size": str(len(file_bytes)),
    }

    chunks: List[bytes] = []
    for key, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )

    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(file_bytes)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)

    req = Request(
        f"{BASE_URL}/drive/v1/medias/upload_all",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        data=body,
    )

    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise FeishuApiError(f"HTTP {exc.code} /drive/v1/medias/upload_all: {body_text}") from exc
    except URLError as exc:
        raise FeishuApiError(f"Network error calling /drive/v1/medias/upload_all: {exc}") from exc

    parsed = _parse_response_json(raw, "/drive/v1/medias/upload_all")
    token_value = parsed.get("data", {}).get("file_token")
    if not token_value:
        raise FeishuApiError(f"file_token missing in upload response: {raw}")
    return token_value


def load_image_dimensions(file_path: str) -> Optional[Tuple[int, int]]:
    try:
        from PIL import Image  # type: ignore

        with Image.open(file_path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None


def replace_image_block(
    token: str,
    document_id: str,
    block_id: str,
    image_token: str,
    dimensions: Optional[Tuple[int, int]] = None,
) -> int:
    replace_payload: Dict[str, Any] = {"token": image_token, "align": 2}
    if dimensions:
        replace_payload["width"] = dimensions[0]
        replace_payload["height"] = dimensions[1]

    resp = _request_json(
        method="PATCH",
        path=f"/docx/v1/documents/{document_id}/blocks/batch_update",
        token=token,
        payload={
            "requests": [
                {
                    "block_id": block_id,
                    "replace_image": replace_payload,
                }
            ]
        },
    )
    return int(resp.get("data", {}).get("document_revision_id", 0))


def hydrate_image_blocks(
    token: str,
    document_id: str,
    inserted_data: Dict[str, Any],
    block_id_to_image_url: Dict[str, str],
    placeholder_to_local: Dict[str, str],
    upload_fallback_token: Optional[str] = None,
) -> Tuple[int, int, List[str]]:
    latest_revision = int(inserted_data.get("document_revision_id", 0))
    warnings: List[str] = []
    replaced_count = 0

    temporary_to_actual: Dict[str, str] = {}
    for relation in inserted_data.get("block_id_relations", []):
        temporary_id = relation.get("temporary_block_id")
        actual_id = relation.get("block_id")
        if isinstance(temporary_id, str) and isinstance(actual_id, str):
            temporary_to_actual[temporary_id] = actual_id

    downloaded_paths: List[str] = []
    try:
        for temporary_block_id, image_url in block_id_to_image_url.items():
            target_block_id = temporary_to_actual.get(temporary_block_id)
            if not target_block_id:
                warnings.append(f"missing block mapping for temporary image block: {temporary_block_id}")
                continue

            image_source: Optional[str] = placeholder_to_local.get(image_url)
            if not image_source and (image_url.startswith("http://") or image_url.startswith("https://")):
                try:
                    image_source = download_remote_image(image_url)
                    downloaded_paths.append(image_source)
                except FeishuApiError as exc:
                    warnings.append(str(exc))
                    continue

            if not image_source:
                continue

            try:
                try:
                    image_token = upload_docx_image(token, image_source, parent_node=target_block_id)
                except FeishuApiError as exc:
                    if (
                        upload_fallback_token
                        and upload_fallback_token != token
                        and ("docs:document.media:upload" in str(exc) or "code=99991679" in str(exc))
                    ):
                        image_token = upload_docx_image(
                            upload_fallback_token,
                            image_source,
                            parent_node=target_block_id,
                        )
                    else:
                        raise
                dimensions = load_image_dimensions(image_source)
                revision = replace_image_block(
                    token=token,
                    document_id=document_id,
                    block_id=target_block_id,
                    image_token=image_token,
                    dimensions=dimensions,
                )
                if revision > 0:
                    latest_revision = revision
                replaced_count += 1
            except FeishuApiError as exc:
                warnings.append(f"failed to replace image {image_url}: {exc}")
                continue
    finally:
        for temp_file in downloaded_paths:
            try:
                os.remove(temp_file)
            except OSError:
                pass

    return latest_revision, replaced_count, warnings


def fetch_document_info(token: str, document_id: str) -> Tuple[str, int]:
    resp = _request_json(
        method="GET",
        path=f"/docx/v1/documents/{document_id}",
        token=token,
    )
    doc = resp["data"]["document"]
    return doc["title"], int(doc.get("revision_id", 0))


def try_get_document_url(token: str, document_id: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        resp = _request_json(
            method="POST",
            path="/drive/v1/files/create_shortcut",
            token=token,
            payload={
                "parent_token": "",
                "refer_entity": {"refer_token": document_id, "refer_type": "docx"},
            },
        )
    except FeishuApiError as exc:
        return f"https://feishu.cn/docx/{document_id}", str(exc)

    url = resp.get("data", {}).get("succ_shortcut_node", {}).get("url")
    if isinstance(url, str) and url:
        return url, None
    return f"https://feishu.cn/docx/{document_id}", "Shortcut API succeeded but did not return a URL."


def _build_public_permission_payload(current: Dict[str, Any]) -> Dict[str, Any]:
    allowed_keys = [
        "comment_entity",
        "copy_entity",
        "external_access_entity",
        "link_share_entity",
        "manage_collaborator_entity",
        "security_entity",
        "share_entity",
    ]
    payload: Dict[str, Any] = {}
    for key in allowed_keys:
        value = current.get(key)
        if value is not None:
            payload[key] = value
    return payload


def ensure_tenant_editable(token: str, document_id: str) -> Dict[str, Any]:
    current_resp = _request_json(
        method="GET",
        path=f"/drive/v2/permissions/{document_id}/public",
        token=token,
        query={"type": "docx"},
    )
    current_permission = current_resp.get("data", {}).get("permission_public", {})
    if not isinstance(current_permission, dict):
        raise FeishuApiError(
            f"Unexpected permission_public payload for document {document_id}: {current_permission}"
        )

    if current_permission.get("link_share_entity") == "tenant_editable":
        return current_permission

    payload = _build_public_permission_payload(current_permission)
    payload["link_share_entity"] = "tenant_editable"
    updated = _request_json(
        method="PATCH",
        path=f"/drive/v2/permissions/{document_id}/public",
        token=token,
        query={"type": "docx"},
        payload=payload,
    )
    updated_permission = updated.get("data", {}).get("permission_public", {})
    if not isinstance(updated_permission, dict):
        raise FeishuApiError(
            f"Unexpected updated permission_public payload for document {document_id}: {updated_permission}"
        )
    return updated_permission


def read_markdown_content(args: argparse.Namespace) -> Tuple[str, Path]:
    if args.content is not None:
        return args.content, Path.cwd()

    if args.content_file:
        content_file = Path(args.content_file).resolve()
        with open(content_file, "r", encoding="utf-8") as file_obj:
            return file_obj.read(), content_file.parent

    if not sys.stdin.isatty():
        return sys.stdin.read(), Path.cwd()

    return "", Path.cwd()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or update a Feishu docx document with Markdown content."
    )
    parser.add_argument("--title", help="Title for new documents.")
    parser.add_argument("--document-id", help="Target document_id to update.")
    parser.add_argument(
        "--folder-token",
        default="",
        help="Optional folder token for new document creation.",
    )
    parser.add_argument("--content", help="Inline markdown content.")
    parser.add_argument("--content-file", help="Path to markdown file.")
    parser.add_argument(
        "--replace-document",
        action="store_true",
        help="Clear existing root blocks before writing content (used with --document-id).",
    )
    parser.add_argument(
        "--index",
        type=int,
        default=None,
        help="Optional insert position for descendant creation.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--env-file", help="Optional .env file containing FEISHU_APP_ID / FEISHU_APP_SECRET.")
    parser.add_argument(
        "--skip-link",
        action="store_true",
        help="Do not call shortcut API to resolve document URL.",
    )
    parser.add_argument("--app-id", default=DEFAULT_APP_ID, help="Feishu App ID override.")
    parser.add_argument(
        "--app-secret",
        default=DEFAULT_APP_SECRET,
        help="Feishu App Secret override.",
    )
    parser.add_argument(
        "--public-editable",
        action="store_true",
        help="Set sharing permission to tenant editable (all members can edit via link).",
    )
    parser.add_argument(
        "--user-auth-file",
        default=str(DEFAULT_USER_AUTH_FILE),
        help="Optional local JSON file containing a saved Feishu user refresh_token. If present, documents are created as that user.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(args.env_file)
    if not args.app_id:
        args.app_id = os.environ.get("FEISHU_APP_ID", "")
    if not args.app_secret:
        args.app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not args.app_id or not args.app_secret:
        raise FeishuApiError(
            "Missing Feishu app credentials. Pass --app-id/--app-secret or --env-file."
        )
    markdown, content_base_dir = read_markdown_content(args)

    if not args.document_id and not args.title:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        args.title = f"Codex Output {timestamp}"

    if not args.document_id and not args.title:
        raise FeishuApiError("Either --document-id or --title is required.")

    token, convert_token, auth_user_name = resolve_api_tokens(args)

    created = False
    if args.document_id:
        document_id = args.document_id
        title, revision_id = fetch_document_info(token, document_id)
    else:
        document_id, revision_id = create_document(token, args.title, args.folder_token)
        title = args.title
        created = True

    image_replaced_count = 0
    image_warnings: List[str] = []

    if markdown.strip():
        if args.replace_document:
            cleared_revision = clear_document_root_children(token, document_id)
            if cleared_revision > 0:
                revision_id = cleared_revision

        rewritten_markdown, placeholder_to_local = rewrite_markdown_with_local_image_placeholders(
            markdown,
            content_base_dir,
        )
        first_level_ids, blocks, block_id_to_image_url = convert_markdown_to_blocks(
            convert_token, rewritten_markdown
        )
        inserted_data = insert_descendants(
            token=token,
            document_id=document_id,
            first_level_block_ids=first_level_ids,
            blocks=blocks,
            index=args.index,
        )
        revision_id = int(inserted_data.get("document_revision_id", 0))
        revision_id, image_replaced_count, image_warnings = hydrate_image_blocks(
            token=token,
            document_id=document_id,
            inserted_data=inserted_data,
            block_id_to_image_url=block_id_to_image_url,
            placeholder_to_local=placeholder_to_local,
            upload_fallback_token=convert_token,
        )

    url = None
    url_warning = None
    if not args.skip_link:
        url, url_warning = try_get_document_url(token, document_id)

    permission_public = None
    if args.public_editable:
        permission_public = ensure_tenant_editable(token, document_id)

    output = {
        "created": created,
        "document_id": document_id,
        "title": title,
        "revision_id": revision_id,
        "url": url,
        "url_warning": url_warning,
        "image_replaced_count": image_replaced_count,
        "image_warnings": image_warnings,
        "permission_public": permission_public,
        "auth_user_name": auth_user_name,
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False))
    else:
        print(f"document_id: {document_id}")
        print(f"title: {title}")
        print(f"revision_id: {revision_id}")
        if url:
            print(f"url: {url}")
        elif url_warning:
            print(f"url_warning: {url_warning}")
        if permission_public:
            print(f"link_share_entity: {permission_public.get('link_share_entity')}")
        if auth_user_name:
            print(f"auth_user_name: {auth_user_name}")
        print(f"image_replaced_count: {image_replaced_count}")
        if image_warnings:
            print("image_warnings:")
            for warning in image_warnings:
                print(f"- {warning}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FeishuApiError as exc:
        print(f"[feishu] {exc}", file=sys.stderr)
        raise SystemExit(1)
