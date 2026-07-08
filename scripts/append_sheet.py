"""v0-B: data/parsed を 案件台帳 へ追記（messageId dedup）→ data/inbox 全メールに processed ラベル付与。

- 案件台帳への重複回避: シートの row_key 列の完全一致で判定（messageId#連番も
  row_key 単位で扱うため、同一メールの複数案件 #1/#2 は別行として両方残る）。
- ラベル付与は「案件として追記できたメール」だけでなく、Claude が判定済みの
  data/inbox 全メール（人材/その他でスキップされた分も含む）に対して行う。
  これにより、案件でないメールが毎回再取得・再判定され続けることを防ぐ。
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build

import state as st
from auth import get_credentials

REPO_ROOT = Path(__file__).resolve().parent.parent
INBOX_DIR = REPO_ROOT / "data" / "inbox"
PARSED_DIR = REPO_ROOT / "data" / "parsed"
JST = timezone(timedelta(hours=9))
PROCESSED_LABEL = "SES-AI/processed"
SHEET_NAME = "案件台帳"
DRYRUN_SHEET_NAME = "案件台帳_dryrun"  # --dry-run 時の書き込み先（本番台帳を汚さない・処理後は手動削除可）

ANKEN_HEADERS = [
    "row_key", "ingested_at", "received_at", "source_from", "source_email",
    "案件名", "商流", "必須スキル", "尚可スキル", "必要経験年数", "単価", "精算幅",
    "勤務地_県", "勤務地_詳細", "リモート", "開始時期", "期間", "面談回数", "募集人数",
    "国籍_年齢制限", "契約形態", "ステータス", "案件メールリンク", "生文抜粋", "備考",
]

# append_sheet.py が inbox 由来で自動補完する列（parsed には含まれない想定）
_AUTO_FILLED_HEADERS = {"ingested_at", "received_at", "source_from", "source_email"}
# data/parsed/*.jsonl が毎行必ず持つべき全21キー（row_key を含む。欠落キー検知用）
PARSED_KEYS = [h for h in ANKEN_HEADERS if h not in _AUTO_FILLED_HEADERS]

# --- 正規化層（レビュー指摘 #1: 開始時期/勤務地_県/リモートはモデルの気配りでなく
#     決定的変換で揃える。Claude は原文抽出のみ担当し、正規化はここで行う） ---

PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]

# 市区・駅・地名 → 都道府県（機械補完用・ベストエフォート。網羅は目指さず
# 実データで頻出した地名を収録。未一致は空欄のまま＝安全側。
# 新しい地名は _build_row の warn を見ながら随時追記していく運用）
AREA_TO_PREFECTURE = {
    # 東京23区
    "千代田区": "東京都", "中央区": "東京都", "港区": "東京都", "新宿区": "東京都",
    "文京区": "東京都", "台東区": "東京都", "墨田区": "東京都", "江東区": "東京都",
    "品川区": "東京都", "目黒区": "東京都", "大田区": "東京都", "世田谷区": "東京都",
    "渋谷区": "東京都", "中野区": "東京都", "杉並区": "東京都", "豊島区": "東京都",
    "北区": "東京都", "荒川区": "東京都", "板橋区": "東京都", "練馬区": "東京都",
    "足立区": "東京都", "葛飾区": "東京都", "江戸川区": "東京都",
    # 東京の主要駅・地名
    "神田": "東京都", "大手町": "東京都", "丸の内": "東京都", "八丁堀": "東京都",
    "六本木": "東京都", "虎ノ門": "東京都", "霞が関": "東京都", "内幸町": "東京都",
    "田町": "東京都", "浜松町": "東京都", "高田馬場": "東京都", "大崎": "東京都",
    "西新宿": "東京都", "都庁前": "東京都", "門前仲町": "東京都", "豊洲": "東京都",
    "小川町": "東京都", "淡路町": "東京都", "新宿御苑前": "東京都", "曙橋": "東京都",
    "中野坂上": "東京都", "新橋": "東京都", "渋谷": "東京都", "新宿": "東京都",
    "品川": "東京都", "目黒": "東京都", "恵比寿": "東京都", "新整備場": "東京都",
    "亀戸": "東京都", "秋葉原": "東京都", "都内": "東京都",
    # 大阪
    "梅田": "大阪府", "本町": "大阪府", "難波": "大阪府", "心斎橋": "大阪府",
    "大阪市": "大阪府",
    # 神奈川
    "横浜": "神奈川県", "みなとみらい": "神奈川県", "伊勢原": "神奈川県", "泉区": "神奈川県",
    "川崎": "神奈川県",
    # 千葉
    "海浜幕張": "千葉県", "幕張": "千葉県", "千葉市": "千葉県",
    # 茨城
    "日立市": "茨城県", "つくば": "茨城県",
    # 千葉（追加）
    "八幡宿": "千葉県",
    # 兵庫（追加）
    "伊丹": "兵庫県", "北伊丹": "兵庫県",
    # 主要都市（政令指定都市・県庁所在地クラス）
    "札幌": "北海道", "仙台": "宮城県", "さいたま": "埼玉県", "北大宮": "埼玉県",
    "名古屋": "愛知県",
    "京都市": "京都府", "神戸": "兵庫県", "広島市": "広島県", "福岡市": "福岡県",
    "博多": "福岡県", "天神": "福岡県", "静岡市": "静岡県", "浜松市": "静岡県",
}

_MONTH_TOKEN_RE = re.compile(r"(\d{4})年(\d{1,2})月|(\d{1,2})月")
_REMOTE_SPECIAL_CASES = {"フル常駐": "常駐", "基本常駐": "常駐"}


def _received_year(received_at: str) -> int:
    if received_at:
        try:
            return datetime.fromisoformat(received_at).year
        except ValueError:
            pass
    return datetime.now(JST).year


def normalize_start_date(raw: str, received_at: str) -> str:
    """開始時期を YYYY-MM／即日 へ決定的に正規化。年欠落は received_at 年で補完、
    複数月併記（例「5月/6月/7月」）は最早月を採用。未対応パターンは原文のまま返す
    （推測補完しない）。"""
    if not raw or not raw.strip():
        return ""
    text = raw.strip()
    if re.match(r"^\d{4}-(0[1-9]|1[0-2])$", text):
        return text
    if text.startswith("即日"):
        return "即日"

    months = []
    year = None
    for m in _MONTH_TOKEN_RE.finditer(text):
        if m.group(1):
            year = int(m.group(1))
            months.append(int(m.group(2)))
        else:
            months.append(int(m.group(3)))
    if not months:
        return text

    month = min(months)
    if year is None:
        year = _received_year(received_at)
    return f"{year:04d}-{month:02d}"


def normalize_prefecture(pref: str, detail: str) -> str:
    """勤務地_県が既に埋まっていればそのまま採用（本文に都道府県名の明記がある場合）。
    空のときだけ勤務地_詳細（駅名・市区名）からの機械補完を試みる。
    どちらも一致しなければ空欄のまま（推測補完しない）。"""
    if pref and pref.strip():
        return pref.strip()
    if not detail:
        return ""
    for name in PREFECTURES:
        if name in detail:
            return name
    for keyword, name in AREA_TO_PREFECTURE.items():
        if keyword in detail:
            return name
    return ""


def normalize_remote(raw: str) -> str:
    """リモートを {フルリモート/一部リモート/常駐} のenumへ正規化し、
    元の限定条件（週n出社等）があれば括弧書きで保持する。未対応パターンは
    原文のまま返す（推測補完しない）。"""
    if not raw or not raw.strip():
        return ""
    text = raw.strip()
    if text in _REMOTE_SPECIAL_CASES:
        return _REMOTE_SPECIAL_CASES[text]

    if re.search(r"一部|併用", text) or re.search(r"週\d+", text):
        base = "一部リモート"
    elif "常駐" in text:
        base = "常駐"
    elif "フル" in text:
        base = "フルリモート"
    else:
        return text

    if text == base:
        return text

    detail = text
    for prefix in (base, "一部", "常駐", "フルリモート", "フル"):
        if detail.startswith(prefix):
            detail = detail[len(prefix):]
            break
    detail = detail.strip("（）() ")
    if not detail:
        return base
    return f"{base}（{detail}）"


def _load_jsonl_dir(dir_path: Path) -> list:
    records = []
    for path in sorted(dir_path.glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def _existing_row_keys(sheets, sheet_id: str, sheet_name: str = SHEET_NAME) -> set:
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{sheet_name}!A2:A"
    ).execute()
    return {row[0] for row in resp.get("values", []) if row}


def _ensure_anken_tab(sheets, sheet_id: str, sheet_name: str) -> None:
    """指定タブが無ければ見出し行付きで作成する（--dry-run のテスト用タブ用）。
    本番の 案件台帳 は setup_v0a.py で作成済みの想定なので通常は何もしない。"""
    meta = sheets.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties.title"
    ).execute()
    tabs = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if sheet_name not in tabs:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
        ).execute()
        print(f"Tab created: {sheet_name}")
    header_resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{sheet_name}!A1:A1"
    ).execute()
    if not header_resp.get("values"):
        sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"{sheet_name}!A1",
            valueInputOption="RAW", body={"values": [ANKEN_HEADERS]},
        ).execute()
        print(f"Header written: {sheet_name}")


def _gmail_link(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{message_id}"


def _extract_email(from_header: str) -> str:
    match = re.search(r"<([^<>]+@[^<>]+)>", from_header)
    if match:
        return match.group(1)
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", from_header)
    return match.group(0) if match else ""


def _build_row(parsed: dict, inbox_index: dict, warnings: list) -> list:
    row_key = parsed.get("row_key", "?")
    missing_keys = [k for k in PARSED_KEYS if k not in parsed]
    if missing_keys:
        warnings.append(f"{row_key}: 欠落キー {missing_keys}（空文字として扱う）")
    unexpected_keys = sorted(set(parsed.keys()) - set(PARSED_KEYS))
    if unexpected_keys:
        warnings.append(f"{row_key}: 想定外キー {unexpected_keys}（無視される）")

    message_id = parsed["row_key"].split("#")[0]
    src = inbox_index.get(message_id, {})
    row = {h: parsed.get(h, "") for h in ANKEN_HEADERS}
    row["row_key"] = parsed["row_key"]
    row["ingested_at"] = datetime.now(JST).isoformat()
    row["received_at"] = src.get("received_at", "")
    row["source_from"] = src.get("from", "")
    row["source_email"] = _extract_email(src.get("from", ""))
    if not row.get("案件メールリンク"):
        row["案件メールリンク"] = _gmail_link(message_id)

    row["開始時期"] = normalize_start_date(row.get("開始時期", ""), row["received_at"])
    row["勤務地_県"] = normalize_prefecture(row.get("勤務地_県", ""), row.get("勤務地_詳細", ""))
    row["リモート"] = normalize_remote(row.get("リモート", ""))
    detail = row.get("勤務地_詳細", "")
    if detail and not row.get("勤務地_県") and "リモート" not in detail:
        warnings.append(
            f"{row['row_key']}: 勤務地_県が未確定（勤務地_詳細={detail!r}）。"
            "AREA_TO_PREFECTURE に地名を追加してください。"
        )

    return [row.get(h, "") for h in ANKEN_HEADERS]


def append_new_rows(sheets, sheet_id: str, parsed_records: list, inbox_index: dict,
                    sheet_name: str = SHEET_NAME) -> tuple:
    existing = _existing_row_keys(sheets, sheet_id, sheet_name)
    new_rows = []
    warnings = []
    for parsed in parsed_records:
        row_key = parsed["row_key"]
        if row_key in existing:
            continue
        new_rows.append(_build_row(parsed, inbox_index, warnings))
        existing.add(row_key)

    if new_rows:
        sheets.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{sheet_name}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()

    return len(new_rows), warnings


def label_all_inbox_messages(gmail, message_ids: list) -> int:
    if not message_ids:
        return 0
    labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
    label_id = next((l["id"] for l in labels if l["name"] == PROCESSED_LABEL), None)
    if not label_id:
        raise RuntimeError(f"Label not found: {PROCESSED_LABEL}（先に scripts/setup_v0a.py を実行してください）")

    for i in range(0, len(message_ids), 1000):
        chunk = message_ids[i:i + 1000]
        gmail.users().messages().batchModify(
            userId="me",
            body={"ids": chunk, "addLabelIds": [label_id]},
        ).execute()
    return len(message_ids)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true",
        help=f"本番 {SHEET_NAME} でなく {DRYRUN_SHEET_NAME} タブへ書き込み、processed ラベル付与も行わない（テスト用）",
    )
    args = parser.parse_args()
    dry_run = args.dry_run
    target_sheet = DRYRUN_SHEET_NAME if dry_run else SHEET_NAME

    load_dotenv(REPO_ROOT / ".env")
    sheet_id = os.environ["SHEET_ID"]

    creds = get_credentials()
    sheets = build("sheets", "v4", credentials=creds)
    gmail = build("gmail", "v1", credentials=creds)

    # _state/_runlog タブが無い環境（新規シート・Routine等）でも動くよう冪等に用意する。
    st.ensure_state_tabs(sheets, sheet_id)

    owner = st.current_owner()
    inbox_records = _load_jsonl_dir(INBOX_DIR)

    # すべての試行（キルスイッチによる拒否も含む）を _runlog に残すため、
    # 判定より必ず先に run_id を発行する。予算チェックはしない
    # （コスト実体はtriage/structure段階で発生済み。appendは後始末のため）。
    run_id = st.log_run_start(
        sheets, sheet_id, phase="append", run_by=owner, count_requested=len(inbox_records),
    )
    status = "error"
    count_processed = 0
    notes = ""
    try:
        st.check_kill_switch(sheets, sheet_id)

        if not inbox_records:
            print("No inbox records found in data/inbox/.")
            status = "success"
            return
        inbox_index = {r["messageId"]: r for r in inbox_records}

        if dry_run:
            print(f"[DRY-RUN] 本番 {SHEET_NAME} には書き込みません。書き込み先: {target_sheet} タブ。ラベル付与もスキップします。")
            _ensure_anken_tab(sheets, sheet_id, target_sheet)

        parsed_records = _load_jsonl_dir(PARSED_DIR)
        appended_count, warnings = append_new_rows(
            sheets, sheet_id, parsed_records, inbox_index, sheet_name=target_sheet
        )
        print(f"Appended {appended_count} rows to {target_sheet}.")
        for w in warnings:
            print(f"[warn] {w}")

        if dry_run:
            print(f"[DRY-RUN] processed ラベル付与をスキップしました（対象 {len(inbox_index)} 通は未処理のまま）。")
            count_processed = appended_count
        else:
            labeled_count = label_all_inbox_messages(gmail, list(inbox_index.keys()))
            print(f"Labeled {labeled_count} messages as {PROCESSED_LABEL}.")
            count_processed = labeled_count
        status = "success"

        # 1バッチサイクルの正常な終了点＝ここでセッションロックを解放する。
        if not st.release_lock(sheets, sheet_id, owner):
            print("[warn] ロックは自分の保持ではなかったため解放しませんでした（"
                  "python scripts/state.py lock status で確認してください）。")
    except st.KillSwitchOn as e:
        status = "aborted_killswitch"
        notes = str(e)
        print(f"[abort] {e}")
    except Exception as e:
        notes = f"{type(e).__name__}: {e}"
        raise
    finally:
        st.log_run_finish(
            sheets, sheet_id, run_id, phase="append", status=status,
            count_processed=count_processed, notes=notes,
        )


if __name__ == "__main__":
    main()
