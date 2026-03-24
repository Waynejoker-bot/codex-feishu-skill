#!/usr/bin/env python3
"""Unified Bitable + permission operations for the Feishu skill."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from feishu_auth_common import FeishuApiError, get_app_credentials, request_json, resolve_access_token


TEXT_FIELD_TYPE = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate Feishu Bitable with app or user identity.")
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
    parser.add_argument("--json", action="store_true", help="Print compact JSON.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    app_p = subparsers.add_parser("create-app", help="Create a new bitable/base.")
    app_p.add_argument("--name", required=True, help="Bitable app name.")
    app_p.add_argument("--folder-token", help="Optional target folder token.")

    lt_p = subparsers.add_parser("list-tables", help="List tables inside a bitable app.")
    lt_p.add_argument("--app-token", required=True, help="Bitable app token.")

    ct_p = subparsers.add_parser("create-table", help="Create a table in a bitable app.")
    ct_p.add_argument("--app-token", required=True)
    ct_p.add_argument("--name", required=True)

    lf_p = subparsers.add_parser("list-fields", help="List table fields.")
    lf_p.add_argument("--app-token", required=True)
    lf_p.add_argument("--table-id", required=True)

    cf_p = subparsers.add_parser("create-field", help="Create a field in a table.")
    cf_p.add_argument("--app-token", required=True)
    cf_p.add_argument("--table-id", required=True)
    cf_p.add_argument("--field-name", required=True)
    cf_p.add_argument(
        "--field-type",
        type=int,
        default=TEXT_FIELD_TYPE,
        help="Raw numeric Feishu field type. Default 1 (text).",
    )
    cf_p.add_argument("--property-json", help="Optional JSON blob for field property.")

    lr_p = subparsers.add_parser("list-records", help="List records in a table.")
    lr_p.add_argument("--app-token", required=True)
    lr_p.add_argument("--table-id", required=True)
    lr_p.add_argument("--page-size", type=int, default=200)

    cr_p = subparsers.add_parser("clear-records", help="Delete all records in a table.")
    cr_p.add_argument("--app-token", required=True)
    cr_p.add_argument("--table-id", required=True)
    cr_p.add_argument("--page-size", type=int, default=500)

    one_p = subparsers.add_parser("create-record", help="Create a single record.")
    one_p.add_argument("--app-token", required=True)
    one_p.add_argument("--table-id", required=True)
    one_p.add_argument(
        "--field",
        action="append",
        default=[],
        help="Field pair in the format 名称=值. Repeat for multiple fields.",
    )
    one_p.add_argument("--fields-json", help="Raw JSON object for fields.")

    many_p = subparsers.add_parser("create-records", help="Create many records from a JSON file.")
    many_p.add_argument("--app-token", required=True)
    many_p.add_argument("--table-id", required=True)
    many_p.add_argument("--records-file", required=True, help="JSON array or {'records':[...]} file.")

    share_p = subparsers.add_parser("share-member", help="Grant a user/group permission on a Drive asset.")
    share_p.add_argument("--token", required=True, help="Drive token: docx, bitable, sheet, wiki, folder, etc.")
    share_p.add_argument(
        "--type",
        required=True,
        choices=["doc", "docx", "sheet", "file", "wiki", "bitable", "folder", "mindnote", "minutes", "slides"],
    )
    share_p.add_argument("--member-type", required=True, choices=["email", "openid", "unionid", "openchat", "opendepartmentid", "userid", "groupid", "wikispaceid"])
    share_p.add_argument("--member-id", required=True)
    share_p.add_argument("--perm", required=True, choices=["view", "edit", "full_access"])
    share_p.add_argument("--need-notification", action="store_true")
    return parser


def render_output(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def ensure_token(args: argparse.Namespace) -> str:
    app_id, app_secret = get_app_credentials(args.app_id, args.app_secret, args.env_file)
    token, _ = resolve_access_token(args.auth_mode, app_id, app_secret, args.user_auth_file)
    return token


def list_records(token: str, app_token: str, table_id: str, page_size: int) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        query = {"page_size": page_size}
        if page_token:
            query["page_token"] = page_token
        resp = request_json(
            method="GET",
            path=f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
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


def parse_fields(args: argparse.Namespace) -> Dict[str, Any]:
    if getattr(args, "fields_json", None):
        payload = json.loads(args.fields_json)
        if not isinstance(payload, dict):
            raise FeishuApiError("--fields-json must be a JSON object.")
        return payload
    fields: Dict[str, Any] = {}
    for pair in getattr(args, "field", []):
        if "=" not in pair:
            raise FeishuApiError(f"Invalid --field {pair!r}. Use 名称=值.")
        key, value = pair.split("=", 1)
        fields[key] = value
    return fields


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    token = ensure_token(args)

    if args.command == "create-app":
        payload = {"name": args.name}
        if args.folder_token:
            payload["folder_token"] = args.folder_token
        resp = request_json(method="POST", path="/bitable/v1/apps", token=token, payload=payload)
        render_output(resp.get("data", {}), args.json)
        return 0

    if args.command == "list-tables":
        resp = request_json(method="GET", path=f"/bitable/v1/apps/{args.app_token}/tables", token=token)
        render_output(resp.get("data", {}), args.json)
        return 0

    if args.command == "create-table":
        resp = request_json(
            method="POST",
            path=f"/bitable/v1/apps/{args.app_token}/tables",
            token=token,
            payload={"table": {"name": args.name}},
        )
        render_output(resp.get("data", {}), args.json)
        return 0

    if args.command == "list-fields":
        resp = request_json(
            method="GET",
            path=f"/bitable/v1/apps/{args.app_token}/tables/{args.table_id}/fields",
            token=token,
        )
        render_output(resp.get("data", {}), args.json)
        return 0

    if args.command == "create-field":
        payload: Dict[str, Any] = {
            "field_name": args.field_name,
            "type": args.field_type,
        }
        if args.property_json:
            payload["property"] = json.loads(args.property_json)
        resp = request_json(
            method="POST",
            path=f"/bitable/v1/apps/{args.app_token}/tables/{args.table_id}/fields",
            token=token,
            payload=payload,
        )
        render_output(resp.get("data", {}), args.json)
        return 0

    if args.command == "list-records":
        records = list_records(token, args.app_token, args.table_id, args.page_size)
        render_output({"items": records, "total": len(records)}, args.json)
        return 0

    if args.command == "clear-records":
        records = list_records(token, args.app_token, args.table_id, args.page_size)
        deleted = []
        for record in records:
            record_id = record.get("record_id") or record.get("id")
            if not record_id:
                continue
            request_json(
                method="DELETE",
                path=f"/bitable/v1/apps/{args.app_token}/tables/{args.table_id}/records/{record_id}",
                token=token,
            )
            deleted.append(record_id)
        render_output({"deleted": deleted, "deleted_count": len(deleted)}, args.json)
        return 0

    if args.command == "create-record":
        fields = parse_fields(args)
        resp = request_json(
            method="POST",
            path=f"/bitable/v1/apps/{args.app_token}/tables/{args.table_id}/records",
            token=token,
            payload={"fields": fields},
        )
        render_output(resp.get("data", {}), args.json)
        return 0

    if args.command == "create-records":
        records_payload = json.loads(Path(args.records_file).read_text(encoding="utf-8"))
        if isinstance(records_payload, dict):
            records = records_payload.get("records")
        else:
            records = records_payload
        if not isinstance(records, list):
            raise FeishuApiError("--records-file must be a JSON array or {'records': [...]} object.")
        created = []
        for index, item in enumerate(records, start=1):
            if not isinstance(item, dict):
                raise FeishuApiError(f"Record #{index} must be a JSON object.")
            fields = item.get("fields", item)
            resp = request_json(
                method="POST",
                path=f"/bitable/v1/apps/{args.app_token}/tables/{args.table_id}/records",
                token=token,
                payload={"fields": fields},
                query={"client_token": str(uuid.uuid4())},
            )
            created.append(resp.get("data", {}).get("record", {}))
        render_output({"created_count": len(created), "items": created}, args.json)
        return 0

    if args.command == "share-member":
        payload = {
            "member_type": args.member_type,
            "member_id": args.member_id,
            "perm": args.perm,
        }
        if args.member_type in {"openid", "userid", "unionid", "email"}:
            payload["type"] = "user"
        resp = request_json(
            method="POST",
            path=f"/drive/v1/permissions/{args.token}/members",
            token=token,
            query={"type": args.type, "need_notification": str(args.need_notification).lower()},
            payload=payload,
        )
        render_output(resp.get("data", {}), args.json)
        return 0

    raise FeishuApiError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FeishuApiError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
