"""差分マッチの入力を一括生成（一括モード用・DECISIONS §9）。

稼働中フォルダ(Drive)の**全要員**について、各自の `match_cursor` 以降の差分案件を
案件台帳から切り出して1回で束ねる。**人員を名指ししない**＝Driveの「稼働中」に登録された
全員が自動対象（人が増減してもコマンドは不変）。案件台帳は1回だけ読み、要員ごとに
`ingested_at > その要員のカーソル` で分割する（要員ごとのSheets読みを避ける）。

出力(stdout, JSON):
{
  "high_water": "<台帳全体の最新 ingested_at。全員のカーソル前進に使う共通値>",
  "targets": [
    {"person_name": "...", "cursor": "<現在のカーソル・空=初回全件>",
     "delta_count": N, "profile": "<md本文>", "delta": [ {案件dict}, ... ]},
    ...
  ]
}
- 各 `delta` はその要員のカーソル基準（＝要員ごとに範囲が違う）。`profile` は本人条件。
  `high_water` は全員共通で、評価後に各要員のカーソルを進める先に使う。
- `<人員名>.md` が無い人員フォルダは `iter_all_profiles` が `[skip]` を stderr に出してスキップ。
- `--full`: 全要員をカーソル無視で全件（スキル編集後の全員再マッチ。通常は state.py match-cursor reset を使う）。
- `--emit-inputs DIR`（既定 DIR=data/matches/_input）: 各要員(delta>0)の入力(profile+delta)を
  DIR/<名前>.json に書き出し、stdout を **delta を含まない軽量マニフェスト**
  （{high_water, targets:[{person_name, cursor, delta_count, input_file}]}）にする。
  評価を要員ごとに並列サブエージェントへ委譲する一括マッチ用（本体コンテキストに
  大量の delta を載せない・SKILL.md 手順2）。

使い方（一括マッチ・SKILL.md 手順）:
  1. このスクリプトで {high_water, targets} を得る（並列時は --emit-inputs でマニフェスト＋入力ファイル）
  2. 各 target の `delta`（差分案件）を `profile`（本人条件）と照合して評価
     → data/matches/<person_name>.jsonl（1行1案件）。並列時は1要員=1サブエージェント。
  3. 全評価の完了後、write_match.py <person_name> --advance-cursor <high_water> を1名ずつ順に
     実行し本人タブへ反映＋カーソル前進（Sheets/_state 書込みは直列）
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
from read_profile import iter_all_profiles
from read_sheet import filter_since, high_water, read_anken

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = "data/matches/_input"


def build_targets(sheets, drive, sheet_id: str, folder_id: str, full: bool = False):
    """(high_water, targets) を返す。台帳を1回読み、稼働中全員ぶんの差分を切り出す。"""
    records = read_anken(sheets, sheet_id)
    hw = high_water(records)

    targets = []
    for person_name, content in iter_all_profiles(drive, folder_id):
        cursor = "" if full else st.get_match_cursor(sheets, sheet_id, person_name)
        delta = filter_since(records, cursor)
        targets.append({
            "person_name": person_name,
            "cursor": cursor,
            "delta_count": len(delta),
            "profile": content,
            "delta": delta,
        })
    return hw, targets


def emit_inputs(targets: list, hw: str, out_dir: Path) -> list:
    """delta>0 の各要員の入力(profile+delta)を out_dir/<名前>.json へ書き出し、
    delta を含まない軽量マニフェスト（targets のリスト）を返す。

    サブエージェントに1要員=1ファイルで評価入力を渡し、本体（マニフェストを読む側）の
    コンテキストに大量の delta を載せないための分割。delta 0件の要員は input_file=None
    （評価不要＝カーソル前進のみ）。ファイルパスは Read しやすいよう絶対パスで返す。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for t in targets:
        entry = {
            "person_name": t["person_name"],
            "cursor": t["cursor"],
            "delta_count": t["delta_count"],
            "input_file": None,
        }
        if t["delta_count"] > 0:
            fp = out_dir / f"{t['person_name']}.json"
            payload = {
                "person_name": t["person_name"],
                "high_water": hw,
                "cursor": t["cursor"],
                "delta_count": t["delta_count"],
                "profile": t["profile"],
                "delta": t["delta"],
            }
            fp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            entry["input_file"] = str(fp)
        manifest.append(entry)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="稼働中フォルダ全要員の差分マッチ入力を一括生成（DECISIONS §9）"
    )
    parser.add_argument(
        "--full", action="store_true",
        help="全要員をカーソル無視で全件にする（スキル編集後の全員再マッチ）",
    )
    parser.add_argument(
        "--emit-inputs", dest="emit_inputs", nargs="?", const=DEFAULT_INPUT_DIR,
        metavar="DIR",
        help="各要員(delta>0)の入力(profile+delta)を DIR/<名前>.json に書き出し、"
             "stdout を delta を含まない軽量マニフェストにする（並列サブエージェント評価用・"
             f"既定 DIR={DEFAULT_INPUT_DIR}）",
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    sheet_id = os.environ["SHEET_ID"]
    folder_id = os.environ["PROFILE_FOLDER_ID"]

    creds = get_credentials()
    sheets = build("sheets", "v4", credentials=creds)
    drive = build("drive", "v3", credentials=creds)

    hw, targets = build_targets(sheets, drive, sheet_id, folder_id, full=args.full)

    total_delta = sum(t["delta_count"] for t in targets)
    print(
        f"[match_prepare] targets={len(targets)} high_water={hw} "
        f"total_delta_ankens={total_delta}{' (--full)' if args.full else ''}",
        file=sys.stderr,
    )
    for t in targets:
        print(
            f"[match_prepare]   {t['person_name']}: delta={t['delta_count']} "
            f"cursor={t['cursor'] or '(未設定=初回全件)'}",
            file=sys.stderr,
        )

    if args.emit_inputs is not None:
        out_dir = Path(args.emit_inputs)
        if not out_dir.is_absolute():
            out_dir = REPO_ROOT / out_dir
        manifest = emit_inputs(targets, hw, out_dir)
        n_files = sum(1 for m in manifest if m["input_file"])
        print(f"[match_prepare] emitted {n_files} input file(s) to {out_dir}", file=sys.stderr)
        print(json.dumps({"high_water": hw, "targets": manifest}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"high_water": hw, "targets": targets}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()