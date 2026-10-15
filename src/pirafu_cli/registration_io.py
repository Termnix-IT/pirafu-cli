"""Bounded subprocesses and GitHub reads; no repository code is executed."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

from pirafu_cli.processes import stop_owned
from pirafu_cli.results import PirafuError

MAX_RESPONSE = 8 * 1024 * 1024
REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}")
SHA = re.compile(r"[0-9a-f]{40}")


def repository_name(value: str) -> str:
    if not REPOSITORY.fullmatch(value) or value.split("/")[1] in {".", ".."}:
        raise PirafuError("PORTAL_REPO_INVALID", "GitHubのowner/repoを明示してください。")
    return value


def branch_name(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or len(value) > 200
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
        or any(
            part.startswith((".", "-")) or part.endswith((".lock", "."))
            for part in value.split("/")
        )
        or ".." in value
        or "//" in value
        or value.endswith("/")
    ):
        raise PirafuError("BASE_INVALID", "安全なbaseブランチ名を明示してください。")
    return value


def external(
    tool: str,
    arguments: list[str],
    *,
    cwd: Path | None = None,
    content: bytes | None = None,
    environment: dict | None = None,
    limit: int = MAX_RESPONSE,
    timeout: float = 60,
) -> tuple[int, bytes]:
    executable = shutil.which(tool)
    if not executable or Path(executable).suffix.lower() in {".bat", ".cmd"}:
        raise PirafuError("TOOL_MISSING", "必要なGit/gh実行ファイルが見つかりません。", 3)
    env = os.environ.copy()
    for key in list(env):
        if key.upper().startswith(("GIT_", "GH_DEBUG")):
            env.pop(key)
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GH_HOST="github.com",
        GH_PROMPT_DISABLED="1",
        GH_NO_UPDATE_NOTIFIER="1",
        GH_NO_EXTENSION_UPDATE_NOTIFIER="1",
        NO_COLOR="1",
    )
    env.update(environment or {})
    process = subprocess.Popen(
        [executable, *arguments],
        cwd=cwd,
        stdin=subprocess.PIPE if content is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        shell=False,
    )
    chunks = []

    def read():
        data = process.stdout.read(limit + 1)
        chunks.append(data)
        if len(data) > limit:
            stop_owned(process)

    def write():
        try:
            process.stdin.write(content)
            process.stdin.close()
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    writer = None
    if content is not None:
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stop_owned(process)
        raise PirafuError(
            "REMOTE_TIMEOUT", "Git/GitHub操作が期限内に終了しませんでした。", 6
        ) from exc
    except BaseException:
        stop_owned(process)
        raise
    finally:
        reader.join(timeout=5)
        if writer:
            writer.join(timeout=5)
        process.stdout.close()
    data = b"".join(chunks)
    if reader.is_alive() or len(data) > limit:
        raise PirafuError("REMOTE_OUTPUT_LIMIT", "Git/GitHub応答が容量上限を超えました。", 6)
    return code, data


def blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def json_data(content: bytes):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    try:
        return json.loads(content, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PirafuError(
            "REMOTE_JSON_INVALID", "GitHub/カタログのJSON形式が不正です。", 6
        ) from exc


@dataclass
class Snapshot:
    base: str
    head: str | None
    base_tree: dict
    tree: dict
    records: dict[str, bytes]
    categories: bytes
    user: dict
    pull: dict | None
    at_base: bool = False
    reconcile: bool = False

    def signature(self) -> dict:
        return {
            "base": self.base,
            "head": self.head,
            "pull": self.pull,
            "user": self.user,
            "atBase": self.at_base,
            "reconcile": self.reconcile,
        }


class GitHub:
    def __init__(self, repository: str, base: str):
        self.repository = repository_name(repository)
        self.base = branch_name(base)
        self.prefix = f"repos/{self.repository}"

    def api(self, endpoint: str):
        code, data = external(
            "gh", ["api", "--hostname", "github.com", "--method", "GET", endpoint]
        )
        if code:
            try:
                status = int(json.loads(data).get("status", 0))
            except (ValueError, TypeError, AttributeError):
                status = 0
            raise PirafuError(
                "GITHUB_READ_FAILED",
                "GitHubを読み取れません。認証・権限・接続先を確認してください。",
                4 if status in {401, 403} else 5 if status == 404 else 6,
            )
        payload = json_data(data)
        if not isinstance(payload, (dict, list)):
            raise PirafuError("GITHUB_PAYLOAD_INVALID", "GitHub応答の形式が不正です。", 6)
        return payload

    def ref(self, branch: str) -> str | None:
        rows = self.api(f"{self.prefix}/git/matching-refs/heads/{quote(branch, safe='')}")
        if not isinstance(rows, list):
            raise PirafuError("GITHUB_PAYLOAD_INVALID", "GitHubのref応答が不正です。", 6)
        found = [row for row in rows if row.get("ref") == f"refs/heads/{branch}"]
        if not found:
            return None
        sha = found[0].get("object", {}).get("sha", "")
        if len(found) != 1 or not SHA.fullmatch(sha):
            raise PirafuError("GITHUB_PAYLOAD_INVALID", "GitHubのSHAを確認できません。", 6)
        return sha

    def tree(self, sha: str) -> dict:
        payload = self.api(f"{self.prefix}/git/trees/{sha}?recursive=1")
        if payload.get("truncated") is not False or not isinstance(payload.get("tree"), list):
            raise PirafuError(
                "PORTAL_TREE_INCOMPLETE", "Portal全体のファイル一覧を取得できません。", 6
            )
        result = {}
        for item in payload["tree"]:
            path = item.get("path")
            if not isinstance(path, str) or path in result:
                raise PirafuError("PORTAL_TREE_INVALID", "Portalのファイル一覧が不正です。", 6)
            result[path] = item
        return result

    def blob(self, tree: dict, path: str, *, limit=256 * 1024) -> bytes:
        item = tree.get(path, {})
        if item.get("mode") != "100644" or not SHA.fullmatch(item.get("sha", "")):
            raise PirafuError(
                "PORTAL_FILE_INVALID", "Portalの必要ファイルが通常ファイルではありません。", 7
            )
        if not 0 <= item.get("size", limit + 1) <= limit:
            raise PirafuError(
                "PORTAL_FILE_TOO_LARGE", "Portalファイルが容量上限を超えています。", 3
            )
        payload = self.api(f"{self.prefix}/git/blobs/{item['sha']}")
        try:
            if payload.get("encoding") != "base64":
                raise ValueError
            content = base64.b64decode("".join(payload["content"].split()), validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise PirafuError("GITHUB_BLOB_INVALID", "GitHubのファイル応答が不正です。", 6) from exc
        if len(content) > limit or blob_sha(content) != item["sha"]:
            raise PirafuError("GITHUB_BLOB_MISMATCH", "GitHubファイルのハッシュが一致しません。", 6)
        return content

    def public_repository(self, url: str) -> None:
        match = re.fullmatch(r"https://github.com/([^/]+/[^/]+?)/?", url)
        if not match:
            raise PirafuError(
                "PUBLIC_REPOSITORY_UNVERIFIED",
                "公開repositoryはGitHub URLで確認するか、リンクを省略してください。",
                5,
            )
        name = repository_name(match[1].removesuffix(".git"))
        payload = self.api(f"repos/{name}")
        if payload.get("private") is not False or payload.get("visibility") != "public":
            raise PirafuError(
                "PRIVATE_REPOSITORY_LINK", "非公開リポジトリのリンクは登録できません。", 5
            )

    def snapshot(self, project_id: str) -> Snapshot:
        from pirafu_cli.contracts import bundle

        branch = f"pirafu/register/{project_id}"
        user = self.api("user")
        if (
            not isinstance(user.get("id"), int)
            or not isinstance(user.get("login"), str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", user["login"])
        ):
            raise PirafuError("GITHUB_IDENTITY_INVALID", "GitHub実行者を確認できません。", 4)
        user = {"id": user["id"], "login": user["login"]}
        base, head = self.ref(self.base), self.ref(branch)
        if base is None:
            raise PirafuError("BASE_MISSING", "指定baseブランチがありません。", 7)
        query = urlencode(
            {
                "state": "all",
                "head": f"{self.repository.split('/')[0]}:{branch}",
                "base": self.base,
                "per_page": 100,
            }
        )
        pulls = self.api(f"{self.prefix}/pulls?{query}")
        if not isinstance(pulls, list) or len(pulls) >= 100:
            raise PirafuError("PULL_LIST_INCOMPLETE", "PRの一覧を確定できません。", 7)
        pull = None
        merged_head = None
        for row in pulls:
            if (
                row.get("head", {}).get("ref") != branch
                or row.get("base", {}).get("ref") != self.base
            ):
                continue
            if row.get("state") != "open":
                if row.get("merged_at"):
                    if row.get("head", {}).get("sha") == head:
                        merged_head = row.get("merge_commit_sha")
                    continue
                raise PirafuError(
                    "REGISTER_BRANCH_RETIRED",
                    "未マージで閉じたPRがあります。手動確認が必要です。",
                    7,
                )
            if (
                pull is not None
                or row.get("head", {}).get("repo", {}).get("full_name", "").lower()
                != self.repository.lower()
                or row.get("base", {}).get("repo", {}).get("full_name", "").lower()
                != self.repository.lower()
                or row.get("user", {}).get("id") != user["id"]
                or row.get("head", {}).get("sha") != head
            ):
                raise PirafuError(
                    "REGISTER_PR_CONFLICT", "既存PRの対象・実行者・headが一致しません。", 7
                )
            number = row.get("number")
            if type(number) is not int or number < 1:
                raise PirafuError("PULL_INVALID", "PR番号が不正です。", 6)
            pull = {"number": number, "url": f"https://github.com/{self.repository}/pull/{number}"}
        base_tree = self.tree(base)
        tree = self.tree(head) if head else base_tree
        _, source = bundle()
        for filename in ("common.schema.json", "catalog.schema.json"):
            path = f"contracts/v1/{filename}"
            digest = source["files"][f"v1/{filename}"]
            if hashlib.sha256(self.blob(base_tree, path)).hexdigest() != digest:
                raise PirafuError(
                    "CONTRACT_REVISION_MISMATCH", "PortalとCLIのスキーマが異なります。", 7
                )
        at_base = False
        reconcile = False
        if head:
            comparison = self.api(f"{self.prefix}/compare/{base}...{head}")
            at_base = comparison.get("status") in {"behind", "identical"} and pull is None
            if (
                comparison.get("status") == "diverged"
                and pull is None
                and isinstance(merged_head, str)
                and SHA.fullmatch(merged_head)
            ):
                merged = self.api(f"{self.prefix}/compare/{merged_head}...{base}")
                reconcile = merged.get("status") in {"ahead", "identical"}
                at_base = reconcile
            if at_base:
                tree = base_tree
        if head and not at_base:
            commits = comparison.get("commits", [])
            if comparison.get("status") != "ahead" or not 0 < comparison.get("ahead_by", 0) <= 100:
                raise PirafuError(
                    "REGISTER_BASE_CHANGED",
                    "登録ブランチのbaseが古いか、分岐しています。手動確認が必要です。",
                    7,
                )
            if len(commits) != comparison["ahead_by"]:
                raise PirafuError(
                    "REGISTER_HISTORY_INCOMPLETE", "登録ブランチの履歴を確認できません。", 7
                )
            for commit in commits:
                message = commit.get("commit", {}).get("message", "")
                if (commit.get("author") or {}).get("id") != user[
                    "id"
                ] or f"\nPirafu-Register: {project_id}\n" not in f"{message}\n":
                    raise PirafuError(
                        "REGISTER_OTHER_CHANGES", "登録ブランチに所有者不明のcommitがあります。", 7
                    )
            changed = {
                path
                for path in base_tree.keys() | tree.keys()
                if base_tree.get(path) != tree.get(path)
                and (tree.get(path) or base_tree[path]).get("type") != "tree"
            }
            record_path = f"catalog/projects/{project_id}.json"
            media = re.compile(rf"catalog/media/{re.escape(project_id)}/[0-9a-f]{{64}}\.png")
            for path in changed:
                item = tree.get(path, {})
                if path == record_path:
                    allowed = item.get("mode") == "100644"
                else:
                    allowed = (
                        bool(media.fullmatch(path))
                        and path not in base_tree
                        and item.get("mode") == "100644"
                    )
                if not allowed:
                    raise PirafuError(
                        "REGISTER_OTHER_CHANGES", "登録ブランチに対象外の変更があります。", 7
                    )
        records = {}
        for path in tree:
            if path.startswith("catalog/projects/"):
                if len(path.split("/")) != 3 or not path.endswith(".json"):
                    raise PirafuError("CATALOG_INVALID", "カタログの構成が不正です。", 7)
                records[path] = self.blob(tree, path)
                if len(records) > 2000:
                    raise PirafuError("CATALOG_LIMIT", "MVPのカタログ件数上限を超えました。", 3)
        return Snapshot(
            base,
            head,
            base_tree,
            tree,
            records,
            self.blob(tree, "catalog/categories.json"),
            user,
            pull,
            at_base,
            reconcile,
        )
