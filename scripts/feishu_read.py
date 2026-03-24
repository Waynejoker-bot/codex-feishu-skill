#!/usr/bin/env python3
"""Read common Feishu objects (wiki, docx, sheet, bitable) and return JSON/Markdown."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from feishu_auth_common import FeishuApiError, get_app_credentials, request_json, resolve_access_token


URL_TOKEN_PATTERNS = {
    "wiki": re.compile(r"/wiki/([A-Za-z0-9]+)"),
    "docx": re.compile(r"/docx/([A-Za-z0-9]+)"),
    "sheet": re.compile(r"/sheets/([A-Za-z0-9]+)"),
    "bitable": re.compile(r"/base/([A-Za-z0-9]+)"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read Feishu wiki/docx/sheet/bitable content.")
    parser.add_argument("--env-file", help="Optional .env file containing FEISHU_APP_ID / FEISHU_APP_SECRET.")
    parser.add_argument("--app-id", help="Feishu App ID override.")
    parser.add_argument("--app-secret", help="Feishu App Secret override.")
    parser.add_argument(
        "--auth-mode",
        default="user",
        choices=["app", "user"],
        help="Use user identity by default. Switch to app only when explicitly needed.",
    )
    parser.add_argument(
        "--user-auth-file",
        default=str(Path(__file__).resolve().parent.parent / ".user_auth.json"),
        help="User auth JSON written by feishu_user_auth.py.",
    )
    parser.add_argument("--url", help="Full Feishu URL.")
    parser.add_argument("--kind", choices=["wiki", "docx", "sheet", "bitable"], help="Explicit token kind.")
    parser.add_argument("--token", help="Explicit token when not passing --url.")
    parser.add_argument("--table-id", help="Optional Bitable table_id to focus on.")
    parser.add_argument("--sheet-id", help="Optional sheet_id to focus on.")
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--max-sheets", type=int, default=3)
    parser.add_argument("--max-cols", type=int, default=20)
    parser.add_argument("--max-rows", type=int, default=100)
    parser.add_argument("--json", action="store_true")
    return parser


def render(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def infer_from_url(url: str) -> Tuple[str, str]:
    parsed = urlparse(url)
    for kind, pattern in URL_TOKEN_PATTERNS.items():
        match = pattern.search(parsed.path)
        if match:
            token = match.group(1)
            return kind, token
    raise FeishuApiError(f"Unsupported Feishu URL: {url}")


def index_to_col_name(num: int) -> str:
    ret = ""
    while num > 0:
        num -= 1
        ret = chr(65 + (num % 26)) + ret
        num //= 26
    return ret or "A"


def markdown_table(rows: List[List[Any]]) -> str:
    if not rows:
        return ""
    max_len = max(len(row) if isinstance(row, list) else 0 for row in rows)
    if max_len == 0:
        return "(Empty Table)"
    normalized: List[List[str]] = []
    for row in rows:
        clean: List[str] = []
        for index in range(max_len):
            value = row[index] if isinstance(row, list) and index < len(row) else ""
            if value is None:
                clean.append("")
            elif isinstance(value, (dict, list)):
                clean.append(json.dumps(value, ensure_ascii=False))
            else:
                clean.append(str(value).replace("\n", "<br>"))
        normalized.append(clean)
    header = normalized[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * max_len) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def read_wiki(token: str, access_token: str, args: argparse.Namespace) -> Dict[str, Any]:
    resp = request_json(
        method="GET",
        path="/wiki/v2/spaces/get_node",
        token=access_token,
        query={"token": token},
    )
    node = resp.get("data", {}).get("node", {})
    obj_type = node.get("obj_type")
    obj_token = node.get("obj_token")
    if not obj_type or not obj_token:
        raise FeishuApiError(f"Wiki node missing obj_type/obj_token: {resp}")
    nested = dispatch_read(obj_type, obj_token, access_token, args)
    nested["wiki_title"] = node.get("title")
    nested["wiki_token"] = token
    return nested


def read_docx(token: str, access_token: str, args: argparse.Namespace) -> Dict[str, Any]:
    info = request_json(
        method="GET",
        path=f"/docx/v1/documents/{token}",
        token=access_token,
    )
    raw = request_json(
        method="GET",
        path=f"/docx/v1/documents/{token}/raw_content",
        token=access_token,
    )
    document = info.get("data", {}).get("document", {})
    return {
        "kind": "docx",
        "token": token,
        "title": document.get("title"),
        "content": raw.get("data", {}).get("content", ""),
    }


def read_sheet(token: str, access_token: str, args: argparse.Namespace) -> Dict[str, Any]:
    meta = request_json(
        method="GET",
        path=f"/sheets/v3/spreadsheets/{token}/sheets/query",
        token=access_token,
    )
    sheets = meta.get("data", {}).get("sheets", [])
    visible = [sheet for sheet in sheets if not sheet.get("hidden")]
    if args.sheet_id:
        target_sheets = [sheet for sheet in visible if sheet.get("sheet_id") == args.sheet_id]
    else:
        target_sheets = visible[: args.max_sheets]
    blocks: List[str] = []
    for sheet in target_sheets:
        grid = sheet.get("grid_properties", {})
        max_rows = min(grid.get("row_count", args.max_rows), args.max_rows)
        max_cols = min(grid.get("column_count", args.max_cols), args.max_cols)
        if max_rows <= 0 or max_cols <= 0:
            blocks.append(f"## Sheet: {sheet.get('title', 'Untitled')}\n\n(Empty)")
            continue
        last_col = index_to_col_name(max_cols)
        range_name = f"{sheet['sheet_id']}!A1:{last_col}{max_rows}"
        values = request_json(
            method="GET",
            path=f"/sheets/v2/spreadsheets/{token}/values/{range_name}",
            token=access_token,
        )
        rows = values.get("data", {}).get("valueRange", {}).get("values", [])
        blocks.append(f"## Sheet: {sheet.get('title', 'Untitled')}\n\n{markdown_table(rows)}")
    return {
        "kind": "sheet",
        "token": token,
        "title": meta.get("data", {}).get("title") or "Feishu Sheet",
        "content": "\n\n".join(blocks),
        "sheets": target_sheets,
    }


def read_bitable(token: str, access_token: str, args: argparse.Namespace) -> Dict[str, Any]:
    tables = request_json(
        method="GET",
        path=f"/bitable/v1/apps/{token}/tables",
        token=access_token,
    )
    items = tables.get("data", {}).get("items", [])
    if args.table_id:
        target_tables = [table for table in items if table.get("table_id") == args.table_id]
    else:
        target_tables = items[:3]
    blocks: List[str] = []
    table_payloads = []
    for table in target_tables:
        records = request_json(
            method="GET",
            path=f"/bitable/v1/apps/{token}/tables/{table['table_id']}/records",
            token=access_token,
            query={"page_size": args.page_size},
        )
        rows = records.get("data", {}).get("items", [])
        all_fields: List[str] = []
        seen = set()
        for row in rows:
            for field_name in row.get("fields", {}).keys():
                if field_name not in seen:
                    seen.add(field_name)
                    all_fields.append(field_name)
        table_md_rows: List[List[Any]] = [all_fields]
        for row in rows:
            table_md_rows.append([row.get("fields", {}).get(name, "") for name in all_fields])
        blocks.append(f"## Table: {table.get('name', 'Untitled')}\n\n{markdown_table(table_md_rows)}")
        table_payloads.append({"table": table, "records": rows})
    return {
        "kind": "bitable",
        "token": token,
        "title": "Feishu Bitable",
        "content": "\n\n".join(blocks),
        "tables": table_payloads,
    }


def dispatch_read(kind: str, token: str, access_token: str, args: argparse.Namespace) -> Dict[str, Any]:
    if kind == "wiki":
        return read_wiki(token, access_token, args)
    if kind == "docx":
        return read_docx(token, access_token, args)
    if kind == "sheet":
        return read_sheet(token, access_token, args)
    if kind == "bitable":
        return read_bitable(token, access_token, args)
    raise FeishuApiError(f"Unsupported kind: {kind}")


def main() -> int:
    args = build_parser().parse_args()
    if not args.url and not (args.kind and args.token):
        raise FeishuApiError("Pass either --url or both --kind and --token.")
    app_id, app_secret = get_app_credentials(args.app_id, args.app_secret, args.env_file)
    access_token, auth_user_name = resolve_access_token(args.auth_mode, app_id, app_secret, args.user_auth_file)
    if args.url:
        kind, token = infer_from_url(args.url)
    else:
        kind, token = args.kind, args.token
    payload = dispatch_read(kind, token, access_token, args)
    payload["auth_mode"] = args.auth_mode
    payload["auth_user_name"] = auth_user_name
    render(payload, args.json)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FeishuApiError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
