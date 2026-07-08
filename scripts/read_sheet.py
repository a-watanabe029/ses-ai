"""v0-C: マッチ用に 案件台帳 を読む。

- 既定（引数なし）: 全件（後方互換）。
- --person <名前>: _state の match_cursor.<名前> を読み、ingested_at がカーソル超の
  差分だけ返す（差分マッチ・DECISIONS §9）。
- --ingested-since <ISO>: ingested_at > ISO の行だけ返す（--person の下位指定）。
- --full: カーソルを無視して全件返す（プロフィール更新後の再マッチ用）。

いずれの場合も、読取時点の全行の max ingested_at（高水位）を stderr へ
`[cursor] high_water=<ISO>` で出力する（write_match.py --advance-cursor が使う）。
読取自体はカーソルを進めない（副作用なし）。

ヘッダー行をキーにした dict のリストを返す・出力する。
案件台帳の列構成が変わっても（列追加・削除・リネーム・順序変更）そのまま追従する。
"""
import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build

import state as st
from auth import get_credentials

REPO_ROOT = Path(__file__).resolve().parent.parent
SHEET_NAME = "案件台帳"
INGESTED_AT_COL = "ingested_at"


def read_anken(sheets, sheet_id: str) -> list:
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{SHEET_NAME}!A1:Z"
    ).execute()
    values = resp.get("values", [])
    if not values:
        return []

    headers = values[0]
    records = []
    for row in values[1:]:
        padded = row + [""] * (len(headers) - len(row))
        records.append(dict(zip(headers, padded)))
    return records


def high_water(records: list) -> str:
    """全行の max ingested_at を返す（ISO8601 は同一オフセットなら辞書順＝時系列順）。
    ingested_at を持つ行が無ければ空文字。"""
    values = [r.get(INGESTED_AT_COL, "") for r in records]
    values = [v for v in values if v]
    return max(values) if values else ""


def filter_since(records: list, since: str) -> list:
    """ingested_at > since の行だけ返す。since が空なら全件。"""
    if not since:
        return records
    return [r for r in records if r.get(INGESTED_AT_COL, "") > since]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="案件台帳をマッチ用に読む（既定=全件・--person で要員ごと差分）"
    )
    parser.add_argument(
        "--person", help="要員名。_state の match_cursor.<名前> 以降の差分だけ返す"
    )
    parser.add_argument(
        "--ingested-since", dest="ingested_since",
        help="ingested_at > この ISO8601 の行だけ返す（--person の下位指定）",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="カーソルを無視して全件返す（プロフィール更新後の再マッチ用）",
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    sheet_id = os.environ["SHEET_ID"]

    creds = get_credentials()
    sheets = build("sheets", "v4", credentials=creds)

    records = read_anken(sheets, sheet_id)

    # 高水位は「読取時点の全行」から出す（フィルタ前）。--advance-cursor へ渡す値。
    hw = high_water(records)

    # カーソル（since）の決定: --full なら無視。--ingested-since が最優先、
    # 次に --person のカーソル。いずれも無ければ全件（since=""）。
    since = ""
    if not args.full:
        if args.ingested_since:
            since = args.ingested_since
        elif args.person:
            since = st.get_match_cursor(sheets, sheet_id, args.person)

    filtered = filter_since(records, since)

    if since:
        print(
            f"[cursor] person={args.person or ''} since={since} "
            f"returned={len(filtered)}/{len(records)}",
            file=sys.stderr,
        )
    elif args.full and args.person:
        print(f"[cursor] person={args.person} --full（全件）", file=sys.stderr)
    print(f"[cursor] high_water={hw}", file=sys.stderr)

    print(json.dumps(filtered, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()