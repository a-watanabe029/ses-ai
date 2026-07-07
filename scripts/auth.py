"""Gmail/Sheets 共通の OAuth 認証ヘルパー（リポジトリルート相対で secrets/ を解決）。"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def _resolve(env_key: str, default: str) -> Path:
    value = os.environ.get(env_key, default)
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def get_credentials() -> Credentials:
    """優先順位: (1) GOOGLE_OAUTH_TOKEN_JSON（token.jsonの中身そのものを環境変数として渡す。Routineの環境変数登録を想定）
    → (2) GOOGLE_SERVICE_ACCOUNT_KEY（サービスアカウント鍵ファイル。当面未使用だが代替手段として残置）
    → (3) 従来通り secrets/token.json のユーザーOAuthフロー（ローカル実行）。"""
    token_json_env = os.environ.get("GOOGLE_OAUTH_TOKEN_JSON")
    if token_json_env:
        creds = Credentials.from_authorized_user_info(json.loads(token_json_env), SCOPES)
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds

    service_account_key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    if service_account_key:
        key_path = Path(service_account_key)
        if not key_path.is_absolute():
            key_path = REPO_ROOT / key_path
        return service_account.Credentials.from_service_account_file(str(key_path), scopes=SCOPES)

    creds_path = _resolve("GOOGLE_CREDENTIALS", "./secrets/credentials.json")
    token_path = _resolve("GOOGLE_TOKEN", "./secrets/token.json")

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds
