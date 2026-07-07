"""v0-C: 要員プロフィール（Google Drive上の 稼働中/<人員フォルダ>/<人員名>.md）を取得する。

`要員/<名前>.md` はPIIのためgit管理から除外し、Google Driveの専用チームドライブフォルダに
保管する方式（フェーズ5-1）。フォルダ構成は 稼働中（`PROFILE_FOLDER_ID`）直下に人員フォルダが並び、
各人員フォルダの中に `<人員名>.md` を置く。取得はDrive API（v3）を`supportsAllDrives=True`で叩き、
Google Drive MCPコネクタは使わない（接続アイデンティティが対象チームドライブへの
アクセス権を持たないため。Routine化ロードマップ フェーズ3-4参照）。
"""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build

from auth import get_credentials

REPO_ROOT = Path(__file__).resolve().parent.parent
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def list_person_folders(drive, root_folder_id: str) -> list:
    resp = drive.files().list(
        q=f"'{root_folder_id}' in parents and mimeType = '{FOLDER_MIME_TYPE}' and trashed = false",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute()
    return resp.get("files", [])


def find_profile_file(drive, folder_id: str, person_name: str):
    query = f"'{folder_id}' in parents and name = '{person_name}.md' and trashed = false"
    resp = drive.files().list(
        q=query,
        fields="files(id, name, mimeType)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute()
    files = resp.get("files", [])
    return files[0] if files else None


def download_profile(drive, file_meta: dict) -> str:
    file_id = file_meta["id"]
    if file_meta.get("mimeType") == GOOGLE_DOC_MIME_TYPE:
        data = drive.files().export_media(fileId=file_id, mimeType="text/plain").execute()
    else:
        data = drive.files().get_media(fileId=file_id).execute()
    return data.decode("utf-8") if isinstance(data, bytes) else data


def find_person_profile(drive, root_folder_id: str, person_name: str) -> str:
    """指定した1名の人員フォルダ内の<人員名>.mdを取得する（単発指定用）。
    人員フォルダ・プロフィールいずれかが無ければFileNotFoundError。"""
    folders = [f for f in list_person_folders(drive, root_folder_id) if f["name"] == person_name]
    if not folders:
        raise FileNotFoundError(f"人員フォルダが見つかりません: {person_name}（folder={root_folder_id}）")
    file_meta = find_profile_file(drive, folders[0]["id"], person_name)
    if not file_meta:
        raise FileNotFoundError(f"プロフィールが見つかりません: {person_name}.md（folder={folders[0]['id']}）")
    return download_profile(drive, file_meta)


def iter_all_profiles(drive, root_folder_id: str):
    """稼働中フォルダ直下の人員フォルダを走査し(person_name, content)を順に返す。
    人員フォルダ内に<人員名>.mdが無い場合はエラーにせずスキップするが、
    どのフォルダをスキップしたかはstderrにログ出力する。"""
    for folder in list_person_folders(drive, root_folder_id):
        person_name = folder["name"]
        file_meta = find_profile_file(drive, folder["id"], person_name)
        if not file_meta:
            print(
                f"[skip] {person_name}: フォルダ内に{person_name}.mdが見つからないためスキップ"
                f"（folder={folder['id']}）",
                file=sys.stderr,
            )
            continue
        yield person_name, download_profile(drive, file_meta)


def main() -> None:
    if len(sys.argv) > 2:
        print("Usage: python read_profile.py [<名前>]", file=sys.stderr)
        sys.exit(1)

    load_dotenv(REPO_ROOT / ".env")
    folder_id = os.environ["PROFILE_FOLDER_ID"]

    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds)

    if len(sys.argv) == 2:
        print(find_person_profile(drive, folder_id, sys.argv[1]))
        return

    profiles = [
        {"person_name": name, "content": content}
        for name, content in iter_all_profiles(drive, folder_id)
    ]
    print(json.dumps(profiles, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
