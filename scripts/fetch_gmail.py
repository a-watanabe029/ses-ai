"""v0-B: 未処理メール取得（-label:SES-AI/processed）→ data/inbox/*.jsonl

カーソルは Gmail ラベルだけ（時刻カーソルを使わない＝取りこぼし無し）。
fetch はフィルタしない・本文テキスト抽出＋軽量クリーニング（MIME デコード＋text/plain優先、
無ければHTML→テキスト。フッター切除・URL短縮・改行/空行正規化。§CLAUDE.md v0実装契約）。
可逆性は案件メールリンク（messageIdからGmail原文へ復元可能）で担保する。
"""
import base64
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import state as st
from auth import get_credentials

REPO_ROOT = Path(__file__).resolve().parent.parent
INBOX_DIR = REPO_ROOT / "data" / "inbox"
JST = timezone(timedelta(hours=9))
QUERY = "-label:SES-AI/processed"

# 送信元ドメイン単位の本文スキップリスト（レビュー2026-07-06 指摘 #7）。
# ニュースレター等・案件/人材の実体が無いと判明した配信元は、本文（追跡URLの塊等）を
# 読まずマーカーへ差し替える。メール自体は inbox に残り triage→append_sheet の流れで
# processed ラベルが付くため、取りこぼしは起きない（＝毎回再取得され続けない）。
# 誤って実案件ドメインを入れないよう、明確なニュース/通知配信元のみを最小限で登録する。
SKIP_BODY_DOMAINS = {
    "ligare.news",  # モビリティ系ニュースレター（本文の大半が追跡URL・実体なし）
}


def _call_with_backoff(request, max_retries=5):
    delay = 1
    for attempt in range(max_retries):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status in (429, 500, 502, 503) and attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def _strip_html(html_body: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_body, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


# --- 本文の定型除去（レビュー指摘 #4: フッター/URL/\r/空行が本文の相当割合を占める。
#     data/inbox の body は構造化の作業用データであり、原文は Gmail 側に残るため
#     可逆性は「案件メールリンク」（messageId から復元可能）で担保する。 ---

_FOOTER_CUT_MARKERS = [
    "労働者派遣事業許可番号",
    "配信停止をご希望",
    "配信解除をご希望",
    "本メールに心当たりがない場合",
    "このメールにお心当たりのない場合",
    "個人情報の取扱いについて",
]
_DECORATIVE_SEPARATOR_RE = re.compile(r"[-=_■◆●○★☆♦♢▼▲△▽~～]{4,}")
_SIGNATURE_DELIMITER_RE = re.compile(r"^--\s*$", flags=re.M)
_URL_RE = re.compile(r"https?://\S+")


def _strip_footer(text: str) -> str:
    cut_at = len(text)
    m = _SIGNATURE_DELIMITER_RE.search(text)
    if m:
        cut_at = min(cut_at, m.start())
    for marker in _FOOTER_CUT_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            line_start = text.rfind("\n", 0, idx)
            cut_at = min(cut_at, line_start if line_start != -1 else idx)
    return text[:cut_at].rstrip()


def _shorten_urls(text: str) -> str:
    def _replace(m: "re.Match") -> str:
        domain_match = re.match(r"https?://([^/]+)", m.group(0))
        return f"[link:{domain_match.group(1)}]" if domain_match else "[link]"
    return _URL_RE.sub(_replace, text)


def _clean_body(text: str) -> str:
    if not text:
        return text
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_footer(text)
    text = _shorten_urls(text)
    text = _DECORATIVE_SEPARATOR_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _decode_part(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    plain, html_text = None, None

    def walk(part: dict) -> None:
        nonlocal plain, html_text
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain" and plain is None:
            plain = _decode_part(data)
        elif data and mime == "text/html" and html_text is None:
            html_text = _decode_part(data)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return _clean_body(plain.strip())
    if html_text:
        return _clean_body(_strip_html(html_text))
    return ""


def _header(headers: list, name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _from_domain(from_header: str) -> str:
    m = re.search(r"[\w.+-]+@([\w-]+\.[\w.-]+)", from_header)
    return m.group(1).lower() if m else ""


def _is_skip_domain(from_header: str) -> bool:
    domain = _from_domain(from_header)
    if not domain:
        return False
    # サブドメイン（news.ligare.news 等）も後方一致で拾う
    return any(domain == d or domain.endswith("." + d) for d in SKIP_BODY_DOMAINS)


def fetch_unprocessed(gmail, limit: int = None, extra_query: str = "") -> list:
    query = f"{QUERY} {extra_query}".strip()
    message_ids = []
    page_token = None
    while True:
        resp = _call_with_backoff(
            gmail.users().messages().list(
                userId="me", q=query, pageToken=page_token,
                maxResults=min(100, limit) if limit else 100,
            )
        )
        message_ids.extend(m["id"] for m in resp.get("messages", []))
        if limit and len(message_ids) >= limit:
            message_ids = message_ids[:limit]
            break
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    records = []
    for msg_id in message_ids:
        msg = _call_with_backoff(
            gmail.users().messages().get(userId="me", id=msg_id, format="full")
        )
        headers = msg["payload"].get("headers", [])
        received_at = (
            datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
            .astimezone(JST)
            .isoformat()
        )
        from_header = _header(headers, "From")
        if _is_skip_domain(from_header):
            body = f"[skipped: 本文スキップ対象ドメイン {_from_domain(from_header)}（ニュースレター等・案件/人材の実体なし）]"
        else:
            body = _extract_body(msg["payload"])
        records.append({
            "messageId": msg["id"],
            "from": from_header,
            "subject": _header(headers, "Subject"),
            "received_at": received_at,
            "body": body,
        })
    return records


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="取得件数の上限（テスト用。未指定なら全件）")
    parser.add_argument("--date", type=str, default=None, help="特定日のみ取得（YYYY-MM-DD・実測用）")
    # TODO(暫定・削除予定): 2026-07-06のパイプライン検証で、未処理キューの直近が特定送信元
    # （SasaTech等の人材ブラスト）で占められ案件に到達できなかったため一時追加。
    # 将来 人材台帳 取込（CLAUDE.md §将来スコープ）を実装する際は人材メールも対象にするため、
    # このフラグで恒常的に特定送信元を除外する運用はしない。検証が終わったら削除を検討。
    parser.add_argument(
        "--exclude-from", type=str, default=None,
        help="[暫定・削除予定] 送信元ドメインをカンマ区切りで除外（例: sasatech.co.jp）",
    )
    args = parser.parse_args()

    extra_query = ""
    if args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d")
        next_day = day + timedelta(days=1)
        extra_query = f"after:{day.strftime('%Y/%m/%d')} before:{next_day.strftime('%Y/%m/%d')}"
    if args.exclude_from:
        excludes = " ".join(f"-from:{d.strip()}" for d in args.exclude_from.split(",") if d.strip())
        extra_query = f"{extra_query} {excludes}".strip()

    load_dotenv(REPO_ROOT / ".env")
    sheet_id = os.environ["SHEET_ID"]
    creds = get_credentials()
    gmail = build("gmail", "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    # _state/_runlog タブが無い環境（新規シート・Routine等）でも動くよう、
    # log_run_start より前に冪等に用意する（既存値は上書きしない）。
    st.ensure_state_tabs(sheets, sheet_id)

    owner = st.current_owner()
    # 予算の要求件数: --limit未指定時は「実質無制限」を表す大きな値を渡す
    # （日次予算の残量で自動的に頭打ちになる）
    n_requested = args.limit if args.limit else 999_999

    # すべての試行（キルスイッチ/ロック/予算による拒否も含む）を _runlog に
    # 残すため、判定より必ず先に run_id を発行する。
    run_id = st.log_run_start(sheets, sheet_id, phase="fetch", run_by=owner, count_requested=n_requested)
    status = "error"
    count_processed = 0
    budget_remaining_after = ""
    notes = ""
    try:
        st.check_kill_switch(sheets, sheet_id)

        if not st.acquire_lock(sheets, sheet_id, owner):
            lock_status = st.get_lock_status(sheets, sheet_id)
            status = "aborted_lock"
            notes = f"lock_owner={lock_status['lock_owner']} により拒否"
            print(
                f"[abort] ロック取得に失敗しました（保持者: {lock_status['lock_owner']}）。"
                f"TTL（{lock_status['lock_ttl_sec']}秒）経過で自然解放されるか、"
                "python scripts/state.py lock release --force で手動解除できます。"
            )
            return

        allowed, remaining_after = st.reserve_budget(sheets, sheet_id, n_requested, owner)
        budget_remaining_after = remaining_after
        if allowed == 0 and n_requested > 0:
            status = "aborted_budget"
            notes = "daily_budget残量なし"
            print("[abort] 本日の daily_budget を使い切っているため処理を中断しました。")
            return
        if allowed < n_requested:
            print(f"[warn] 要求{n_requested}件のうち予算により{allowed}件のみ許可されました。")

        records = fetch_unprocessed(gmail, limit=allowed, extra_query=extra_query)
        count_processed = len(records)
        status = "success" if allowed >= n_requested else "partial_budget"
        if not records:
            print("No unprocessed messages.")
            return

        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(JST).strftime("%Y%m%dT%H%M%S")
        out_path = INBOX_DIR / f"{stamp}.jsonl"
        with out_path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        print(f"Fetched {len(records)} messages -> {out_path}")
    except st.KillSwitchOn as e:
        status = "aborted_killswitch"
        notes = str(e)
        print(f"[abort] {e}")
    except Exception as e:
        notes = f"{type(e).__name__}: {e}"
        raise
    finally:
        # ロックはここでは解放しない（セッション単位＝append_sheet.py完了まで保持。
        # fetch失敗でバッチを中断する場合は運用者が状況を見て手動解放するか、
        # TTL失効に委ねる）。
        st.log_run_finish(
            sheets, sheet_id, run_id, phase="fetch", status=status,
            count_processed=count_processed,
            budget_remaining_after=budget_remaining_after, notes=notes,
        )


if __name__ == "__main__":
    main()
