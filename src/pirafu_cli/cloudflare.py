"""Pagesの対象プロジェクトだけを読む。管理レスポンスを出力層へ渡さない。"""

import json
import os
import re
import time

import httpx

from pirafu_cli.results import PirafuError

API_ORIGIN = "https://api.cloudflare.com/client/v4"
MAX_RESPONSE = 1024 * 1024


class PagesReader:
    def __init__(self, *, transport=None):
        account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not re.fullmatch(r"[a-fA-F0-9]{32}", account) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,256}", token
        ):
            raise PirafuError(
                "CLOUDFLARE_CREDENTIALS_REQUIRED",
                "Pages Readのトークンと対象アカウントIDを実行環境へ設定してください。",
                4,
            )
        self._account = account
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self):
        self._client.close()

    def get_project(self, name: str, *, budget: float = 20) -> dict:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?", name):
            raise PirafuError("PROJECT_NAME_INVALID", "Pagesプロジェクト名が不正です。")
        deadline = time.monotonic() + budget
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                with self._client.stream(
                    "GET",
                    f"{API_ORIGIN}/accounts/{self._account}/pages/projects/{name}",
                    timeout=max(0.01, min(5, remaining / 4)),
                ) as response:
                    code = response.status_code
                    if code in {401, 403}:
                        raise PirafuError(
                            "CLOUDFLARE_AUTH_DENIED",
                            "認証またはPages Read権限を確認してください。",
                            4,
                        )
                    if code == 404:
                        raise PirafuError(
                            "PAGES_NOT_FOUND", "対象プロジェクトを確認できません。", 5
                        )
                    if code == 429 or 500 <= code <= 599:
                        if attempt < 2:
                            delay = min(0.25 * (2**attempt), max(0, deadline - time.monotonic()))
                            time.sleep(delay)
                            continue
                        raise PirafuError(
                            "CLOUDFLARE_UNAVAILABLE", "APIの混雑・一時障害が続いています。", 6
                        )
                    if code != 200:
                        raise PirafuError(
                            "CLOUDFLARE_RESPONSE", "APIが正常応答を返しませんでした。", 6
                        )
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        if len(content) + len(chunk) > MAX_RESPONSE:
                            raise PirafuError(
                                "CLOUDFLARE_RESPONSE_LIMIT", "API応答の容量上限を超えました。", 6
                            )
                        content.extend(chunk)
                        if time.monotonic() >= deadline:
                            raise PirafuError(
                                "CLOUDFLARE_TIMEOUT", "API照会の期限を超えました。", 6
                            )
                data = json.loads(content)
                if (
                    not isinstance(data, dict)
                    or data.get("success") is not True
                    or not isinstance(data.get("result"), dict)
                ):
                    raise PirafuError("CLOUDFLARE_PAYLOAD_INVALID", "API応答の形式が不正です。", 6)
                return data["result"]
            except (httpx.HTTPError, ValueError, UnicodeError) as exc:
                raise PirafuError(
                    "CLOUDFLARE_COMMUNICATION", "APIへの接続・応答解析に失敗しました。", 6
                ) from exc
        raise PirafuError("CLOUDFLARE_TIMEOUT", "API照会の期限を超えました。", 6)
