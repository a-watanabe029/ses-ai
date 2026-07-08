"""v0-C: マッチ結果を人員タブ（1人1タブ・タブ名＝要員名）へ追記。

- 入力: data/matches/<要員名>.jsonl（Claude が評価・作成した1行1案件のマッチ結果）
- タブが無ければ見出し行付きで新規作成
- 既存の「案件row_key(messageId)」列で重複回避（再実行は新規のみ・既存行の手動ステータスは保持）
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build

import state as st
from auth import get_credentials

REPO_ROOT = Path(__file__).resolve().parent.parent
MATCHES_DIR = REPO_ROOT / "data" / "matches"
JST = timezone(timedelta(hours=9))

MATCH_HEADERS = [
    "記載日", "適合度", "案件名", "単価", "勤務地", "リモート", "商流",
    "必須スキル", "適合理由", "懸念", "ステータス", "案件row_key(messageId)", "リンク",
]
KEY_COL = "案件row_key(messageId)"


def _load_matches(person_name: str) -> list:
    path = MATCHES_DIR / f"{person_name}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"マッチ結果ファイルが見つかりません: {path}")
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _ensure_tab(sheets, sheet_id: str, person_name: str) -> None:
    meta = sheets.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties.title").execute()
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

    if person_name not in tabs:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": person_name}}}]},
        ).execute()
        print(f"Tab created: {person_name}")

    # タブが既存でも、見出し行が無ければ（=手動作成の空タブ等）ここで補完する
    header_resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{person_name}!A1:A1"
    ).execute()
    if header_resp.get("values"):
        return

    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{person_name}!A1",
        valueInputOption="RAW",
        body={"values": [MATCH_HEADERS]},
    ).execute()
    print(f"Header written: {person_name}")


def _existing_row_keys(sheets, sheet_id: str, person_name: str) -> set:
    col_index = MATCH_HEADERS.index(KEY_COL)
    col_letter = chr(ord("A") + col_index)
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{person_name}!{col_letter}2:{col_letter}"
    ).execute()
    return {row[0] for row in resp.get("values", []) if row}


def _build_row(match: dict) -> list:
    row = {h: match.get(h, "") for h in MATCH_HEADERS}
    row["記載日"] = datetime.now(JST).strftime("%Y-%m-%d")
    row[KEY_COL] = match.get("row_key", match.get(KEY_COL, ""))
    if not row.get("ステータス"):
        row["ステータス"] = "未対応"
    return [row.get(h, "") for h in MATCH_HEADERS]


def append_matches(sheets, sheet_id: str, person_name: str, matches: list) -> int:
    _ensure_tab(sheets, sheet_id, person_name)
    existing = _existing_row_keys(sheets, sheet_id, person_name)

    new_rows = []
    for match in matches:
        key = match.get("row_key", match.get(KEY_COL, ""))
        if not key or key in existing:
            continue
        new_rows.append(_build_row(match))
        existing.add(key)

    if new_rows:
        sheets.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{person_name}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()

    return len(new_rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="マッチ結果を人員タブへ追記（messageId dedup）"
    )
    parser.add_argument("person_name", help="要員名（＝タブ名・data/matches/<名前>.jsonl）")
    parser.add_argument(
        "--advance-cursor", dest="advance_cursor",
        help="追記成功後に _state の match_cursor.<名前> をこの高水位ISOへ前進"
             "（read_sheet.py / match_prepare.py の high_water をそのまま保存）",
    )
    args = parser.parse_args()
    person_name = args.person_name

    load_dotenv(REPO_ROOT / ".env")
    sheet_id = os.environ["SHEET_ID"]

    creds = get_credentials()
    sheets = build("sheets", "v4", credentials=creds)

    # 差分を評価したがマッチ0件だった場合、結果ファイルが無い/空でも
    # カーソルは前進させたい（再評価ループ防止）。--advance-cursor 指定時のみ許容。
    try:
        matches = _load_matches(person_name)
    except FileNotFoundError:
        if not args.advance_cursor:
            raise
        matches = []
        print(f"[note] {person_name}: マッチ結果ファイルが無いため追記0件（--advance-cursor によりカーソルのみ前進）。")

    appended = append_matches(sheets, sheet_id, person_name, matches)
    print(f"Appended {appended} rows to tab '{person_name}' ({len(matches) - appended} skipped as duplicates).")

    # 追記成功後にカーソルを前進（追記→前進の順。途中クラッシュは次回再スキャンで回収。DECISIONS §9）。
    # 高水位そのままを保存する（overlap は入れない）。カーソル＝最新行の ingested_at と一致するので、
    # 次回 `ingested_at > cursor` で最新バッチが正しく除外され、新着が無ければ差分0になる。
    if args.advance_cursor:
        st.set_match_cursor(sheets, sheet_id, person_name, args.advance_cursor)
        print(f"match_cursor.{person_name} を {args.advance_cursor} へ前進。")


if __name__ == "__main__":
    main()
