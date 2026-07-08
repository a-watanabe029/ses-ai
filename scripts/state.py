"""cron化フェーズ1: 予算上限・キルスイッチ・セッション単位ロック・監査ログ（_state/_runlog）。

複数人が別々のPCから実行するため、状態は共有スプレッドシートの `_state`/`_runlog`
タブに置く（ローカルに可変stateファイルを持たない・2026-07-01決定）。

ロックはスクリプト単体でなく「1バッチの処理サイクル全体」を守るセッション単位。
fetch_gmail.py が取得し、append_sheet.py が完了時に解放する（本命のレース＝
fetch後の長い人手triage/structure作業中に別の人が追いfetchすることを防ぐため）。
"""
import getpass
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))

STATE_SHEET = "_state"
RUNLOG_SHEET = "_runlog"
STATE_HEADERS = ["key", "value", "updated_at", "updated_by"]
RUNLOG_HEADERS = [
    "run_id", "started_at", "finished_at", "phase", "status", "run_by",
    "count_requested", "count_processed", "budget_remaining_after", "notes",
]
DEFAULT_STATE = {
    "schema_version": "1",
    "kill_switch": "off",
    "daily_budget": "8000",
    "day_spent": "0",
    "day_spent_date": "",
    "lock_owner": "",
    "lock_acquired_at": "",
    "lock_ttl_sec": "21600",
    "lock_note": "",
}


class KillSwitchOn(Exception):
    """_state.kill_switch=on のため処理を中断した。"""


def current_owner() -> str:
    run_by = os.environ.get("RUN_BY")
    if run_by:
        return run_by
    return f"{getpass.getuser()}@{socket.gethostname()}"


def _now_iso() -> str:
    return datetime.now(JST).isoformat()


def _read_state_rows(sheets, sheet_id: str) -> list:
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{STATE_SHEET}!A2:D"
    ).execute()
    rows = []
    for row in resp.get("values", []):
        row = row + [""] * (4 - len(row))
        rows.append(row[:4])
    return rows


def _read_state(sheets, sheet_id: str) -> dict:
    return {r[0]: r[1] for r in _read_state_rows(sheets, sheet_id) if r[0]}


def _write_state(sheets, sheet_id: str, changed: dict, owner: str) -> None:
    """changed に含まれるキーだけ値・updated_at・updated_byを更新し、
    それ以外の既存行（コード側が知らない手動追加行等）はそのまま書き戻す。"""
    rows = _read_state_rows(sheets, sheet_id)
    by_key = {r[0]: r for r in rows if r[0]}
    now = _now_iso()
    for key, value in changed.items():
        if key in by_key:
            row = by_key[key]
            row[1] = str(value)
            row[2] = now
            row[3] = owner
        else:
            rows.append([key, str(value), now, owner])

    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{STATE_SHEET}!A2:D{len(rows) + 1}",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


def check_kill_switch(sheets, sheet_id: str) -> None:
    state = _read_state(sheets, sheet_id)
    if str(state.get("kill_switch", "off")).strip().lower() == "on":
        raise KillSwitchOn(
            "kill_switch=on のため中断しました"
            "（python scripts/state.py killswitch off で解除）"
        )


def set_kill_switch(sheets, sheet_id: str, on: bool, owner: str = None) -> None:
    owner = owner or current_owner()
    _write_state(sheets, sheet_id, {"kill_switch": "on" if on else "off"}, owner)


def acquire_lock(sheets, sheet_id: str, owner: str, ttl_sec: int = None) -> bool:
    """ロックが空・自分自身・またはTTL超過なら取得してTrue。
    他者が保持中で未失効ならFalse（ベストエフォート＝真のCASではない。
    同時実行はまれという既存の運用前提に合わせた許容）。"""
    state = _read_state(sheets, sheet_id)
    ttl = ttl_sec if ttl_sec is not None else int(
        state.get("lock_ttl_sec") or DEFAULT_STATE["lock_ttl_sec"]
    )
    lock_owner = state.get("lock_owner", "")
    lock_acquired_at = state.get("lock_acquired_at", "")
    now = time.time()

    if lock_owner and lock_owner != owner:
        acquired_at = float(lock_acquired_at) if lock_acquired_at else 0.0
        if now - acquired_at < ttl:
            return False

    _write_state(sheets, sheet_id, {
        "lock_owner": owner,
        "lock_acquired_at": f"{now:.0f}",
    }, owner)
    return True


def release_lock(sheets, sheet_id: str, owner: str, force: bool = False) -> bool:
    """自分がownerの時だけ（forceなら無条件に）クリアする。"""
    state = _read_state(sheets, sheet_id)
    if not force and state.get("lock_owner") != owner:
        return False
    _write_state(sheets, sheet_id, {
        "lock_owner": "",
        "lock_acquired_at": "",
    }, owner)
    return True


def get_lock_status(sheets, sheet_id: str) -> dict:
    state = _read_state(sheets, sheet_id)
    return {
        "lock_owner": state.get("lock_owner", ""),
        "lock_acquired_at": state.get("lock_acquired_at", ""),
        "lock_ttl_sec": state.get("lock_ttl_sec", DEFAULT_STATE["lock_ttl_sec"]),
    }


def reserve_budget(sheets, sheet_id: str, n_requested: int, owner: str) -> tuple:
    """JST日付が変わっていたら day_spent を0リセットしてから予算を予約する。
    戻り値 (allowed, remaining_after)。呼び出し側は
    allowed < n_requested を「一部許可」、allowed == 0 かつ n_requested > 0 を
    「完全拒否」として扱うこと。"""
    state = _read_state(sheets, sheet_id)
    today = datetime.now(JST).strftime("%Y-%m-%d")
    day_spent = int(state.get("day_spent") or 0)
    if state.get("day_spent_date", "") != today:
        day_spent = 0

    budget = int(state.get("daily_budget") or 0)
    remaining = max(budget - day_spent, 0)
    allowed = max(min(remaining, n_requested), 0)

    _write_state(sheets, sheet_id, {
        "day_spent": day_spent + allowed,
        "day_spent_date": today,
    }, owner)
    return allowed, remaining - allowed


def get_budget_status(sheets, sheet_id: str) -> dict:
    state = _read_state(sheets, sheet_id)
    return {
        "daily_budget": state.get("daily_budget", ""),
        "day_spent": state.get("day_spent", ""),
        "day_spent_date": state.get("day_spent_date", ""),
    }


# --- 差分マッチ（要員→案件）のカーソル（DECISIONS §9） ---
# key=match_cursor.<要員名>・value=ISO8601(JST) の ingested_at 到達点。
# 未設定=空文字=未実行=全件対象（新規要員のオンボーディング）。

MATCH_CURSOR_PREFIX = "match_cursor."


def get_match_cursor(sheets, sheet_id: str, person: str) -> str:
    """要員 person のマッチ差分カーソル（ingested_at 到達点・ISO8601）を返す。
    未設定なら空文字（＝未実行＝全件対象）。"""
    state = _read_state(sheets, sheet_id)
    return state.get(f"{MATCH_CURSOR_PREFIX}{person}", "")


def set_match_cursor(sheets, sheet_id: str, person: str, iso: str, owner: str = None) -> None:
    """要員 person のマッチ差分カーソルを iso（ingested_at・ISO8601）へ更新する。"""
    owner = owner or current_owner()
    _write_state(sheets, sheet_id, {f"{MATCH_CURSOR_PREFIX}{person}": iso}, owner)


def _append_runlog(sheets, sheet_id: str, row: list) -> None:
    sheets.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{RUNLOG_SHEET}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def log_run_start(sheets, sheet_id: str, phase: str, run_by: str, count_requested: int) -> str:
    """すべての試行（成功/拒否問わず）を残すため、kill_switch/lock/budgetの
    判定より必ず先に呼ぶこと。startのみでfinishが無い行＝ブロックされたか
    クラッシュした、と読む運用にする。"""
    run_id = str(uuid.uuid4())
    _append_runlog(sheets, sheet_id, [
        run_id, _now_iso(), "", phase, "started", run_by,
        count_requested, "", "", "",
    ])
    return run_id


def log_run_finish(
    sheets, sheet_id: str, run_id: str, phase: str, status: str,
    count_processed: int = 0, budget_remaining_after="", notes: str = "",
) -> None:
    _append_runlog(sheets, sheet_id, [
        run_id, "", _now_iso(), phase, status, "",
        "", count_processed, budget_remaining_after, notes,
    ])


def ensure_state_tabs(sheets, sheet_id: str) -> None:
    """_state/_runlog タブを冪等に用意する。既存の値は上書きしない
    （誤って再実行しても day_spent 等が初期化されない）。"""
    meta = sheets.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties.title"
    ).execute()
    tabs = {s["properties"]["title"] for s in meta.get("sheets", [])}

    if STATE_SHEET not in tabs:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": STATE_SHEET}}}]},
        ).execute()
        print(f"Tab created: {STATE_SHEET}")

    header_resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{STATE_SHEET}!A1:A1"
    ).execute()
    if not header_resp.get("values"):
        sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"{STATE_SHEET}!A1",
            valueInputOption="RAW", body={"values": [STATE_HEADERS]},
        ).execute()
        print(f"Header written: {STATE_SHEET}")

    existing = _read_state(sheets, sheet_id)
    missing = {k: v for k, v in DEFAULT_STATE.items() if k not in existing}
    if missing:
        _write_state(sheets, sheet_id, missing, "setup_v0a")
        print(f"Initial values written to {STATE_SHEET}: {list(missing.keys())}")

    if RUNLOG_SHEET not in tabs:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": RUNLOG_SHEET}}}]},
        ).execute()
        print(f"Tab created: {RUNLOG_SHEET}")

    header_resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{RUNLOG_SHEET}!A1:A1"
    ).execute()
    if not header_resp.get("values"):
        sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"{RUNLOG_SHEET}!A1",
            valueInputOption="RAW", body={"values": [RUNLOG_HEADERS]},
        ).execute()
        print(f"Header written: {RUNLOG_SHEET}")


def main() -> None:
    import argparse

    from dotenv import load_dotenv
    from googleapiclient.discovery import build

    from auth import get_credentials

    parser = argparse.ArgumentParser(description="_state/_runlog 手動操作CLI（復旧・確認用）")
    sub = parser.add_subparsers(dest="command", required=True)

    lock_p = sub.add_parser("lock")
    lock_p.add_argument("action", choices=["status", "release"])
    lock_p.add_argument("--force", action="store_true", help="ownerが一致しなくても強制解除")

    sub_budget = sub.add_parser("budget")
    sub_budget.add_argument("action", choices=["status"])

    kill_p = sub.add_parser("killswitch")
    kill_p.add_argument("action", choices=["on", "off", "status"])

    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    sheet_id = os.environ["SHEET_ID"]
    creds = get_credentials()
    sheets = build("sheets", "v4", credentials=creds)

    if args.command == "lock":
        if args.action == "status":
            print(get_lock_status(sheets, sheet_id))
        else:
            ok = release_lock(sheets, sheet_id, current_owner(), force=args.force)
            print("released" if ok else "not released（ownerが不一致・--force を付けてください）")
    elif args.command == "budget":
        print(get_budget_status(sheets, sheet_id))
    elif args.command == "killswitch":
        if args.action == "status":
            state = _read_state(sheets, sheet_id)
            print(f"kill_switch={state.get('kill_switch', 'off')}")
        else:
            set_kill_switch(sheets, sheet_id, on=(args.action == "on"))
            print(f"kill_switch set to {args.action}")


if __name__ == "__main__":
    main()
