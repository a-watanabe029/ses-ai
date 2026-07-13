"""v0-C: マッチ結果を人員タブ（1人1タブ・タブ名＝要員名）へ追記。

- 入力: data/matches/<要員名>.jsonl（Claude が評価・作成した1行1案件のマッチ結果）
- タブが無ければ見出し行付きで新規作成
- 既存の「案件row_key(messageId)」列で重複回避（再実行は新規のみ・既存行の手動ステータスは保持）
- 名簿 `人員一覧` を upsert（新規要員は行追加・既存要員は件数更新。タブへのリンク付き）
"""
import argparse
import json
import os
import sys
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

ROSTER_TAB = "人員一覧"
ROSTER_HEADERS = ["名前", "タブリンク", "件数"]


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


def _ensure_tab(sheets, sheet_id: str, person_name: str) -> int:
    """人員タブを用意し、その sheetId(gid) を返す（名簿のリンク生成に使う）。"""
    meta = sheets.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties(title,sheetId)"
    ).execute()
    gid_by_title = {
        s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])
    }

    if person_name not in gid_by_title:
        resp = sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": person_name}}}]},
        ).execute()
        tab_gid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
        print(f"Tab created: {person_name}")
    else:
        tab_gid = gid_by_title[person_name]

    # タブが既存でも、見出し行が無ければ（=手動作成の空タブ等）ここで補完する
    header_resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{person_name}!A1:A1"
    ).execute()
    if header_resp.get("values"):
        return tab_gid

    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{person_name}!A1",
        valueInputOption="RAW",
        body={"values": [MATCH_HEADERS]},
    ).execute()
    print(f"Header written: {person_name}")
    return tab_gid


def _ensure_roster_tab(sheets, sheet_id: str) -> None:
    """名簿タブ `人員一覧` と見出し行を用意する（無ければ作成）。"""
    meta = sheets.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties.title"
    ).execute()
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

    if ROSTER_TAB not in tabs:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": ROSTER_TAB}}}]},
        ).execute()
        print(f"Tab created: {ROSTER_TAB}")

    header_resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{ROSTER_TAB}!A1:A1"
    ).execute()
    if header_resp.get("values"):
        return

    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{ROSTER_TAB}!A1",
        valueInputOption="RAW",
        body={"values": [ROSTER_HEADERS]},
    ).execute()
    print(f"Header written: {ROSTER_TAB}")


def _upsert_roster(sheets, sheet_id: str, person_name: str, tab_gid: int, count: int) -> None:
    """名簿 `人員一覧` を upsert（新規要員は行追加・既存要員は件数更新）。

    タブ名＝要員名で突合。`タブリンク` は本人タブへ飛ぶ HYPERLINK 数式。
    ses-match は要員を1名ずつ順に処理する（並列化しない）ため read→write の競合は起きない。
    """
    _ensure_roster_tab(sheets, sheet_id)

    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{ROSTER_TAB}!A2:A"
    ).execute()
    names = [row[0] if row else "" for row in resp.get("values", [])]

    link = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={tab_gid}"
    safe_name = person_name.replace('"', '""')
    link_formula = f'=HYPERLINK("{link}","{safe_name}")'
    row_values = [[person_name, link_formula, count]]

    if person_name in names:
        row_num = names.index(person_name) + 2  # A2 が names[0]
        sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{ROSTER_TAB}!A{row_num}",
            valueInputOption="USER_ENTERED",
            body={"values": row_values},
        ).execute()
        print(f"Roster updated: {person_name} (件数={count})")
    else:
        sheets.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{ROSTER_TAB}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": row_values},
        ).execute()
        print(f"Roster added: {person_name} (件数={count})")


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
    tab_gid = _ensure_tab(sheets, sheet_id, person_name)
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

    # 名簿 `人員一覧` を upsert（新規要員は行追加・既存要員は件数更新）。
    # 件数＝本人タブの一意 row_key 総数（既存＋今回追記）。マッチ0件の新規要員も
    # タブが作られるので名簿に 件数=0 で載せる（＝新規要員の追加が名簿へ反映される）。
    _upsert_roster(sheets, sheet_id, person_name, tab_gid, len(existing))

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
    parser.add_argument(
        "--export-share-book", dest="export_share_book", action="store_true",
        help="追記・カーソル前進の成功後に、本人タブから共有book（稼働中/<名前>/<名前>_案件マッチ）へ"
             "共有可の列だけを射影する（H-1）。失敗しても本人タブ/カーソルは壊さない。",
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

    # H-1: 本人タブ→共有book へ射影（追記→カーソル前進の後・失敗は隔離）。
    # 共有book書込が落ちても本人タブ/カーソルは確定済みで、次回実行で rebuild し取り戻せる
    # （export_share_book は本人タブを毎回全読み rebuild する冪等な射影）。
    # export_share_book は本人1名に閉じたリソースのみ触り（本人タブ読取＋本人の別book書込）、
    # _state・人員一覧には触れないため、直列書込フェーズ内で安全に走る。
    if args.export_share_book:
        try:
            # 遅延import（循環参照回避: export_share_book は write_match を import する）。
            import export_share_book as esb
            root_folder_id = os.environ["PROFILE_FOLDER_ID"]
            drive = build("drive", "v3", credentials=creds)
            esb.export_one(drive, sheets, sheet_id, root_folder_id, person_name)
        except Exception as e:
            print(
                f"[warn] {person_name}: 共有book反映に失敗（本人タブ/カーソルは確定済み・次回再反映）: {e}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
