"""Prepare immutable registration plans, then apply only the approved bytes."""

import copy
import hashlib
import io
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from pirafu_cli.contracts import images, validate
from pirafu_cli.files import inside, load_manifest, read_bytes
from pirafu_cli.media import check_image_bytes
from pirafu_cli.registration_io import GitHub, Snapshot, blob_sha, external, json_data
from pirafu_cli.results import Notice, PirafuError, Result
from pirafu_cli.services import check


def serialized(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def prepare(config: Path) -> tuple[dict, dict[str, bytes], str]:
    config = config.absolute()
    raw = read_bytes(config)
    manifest = validate(load_manifest(config))
    record = {
        key: copy.deepcopy(manifest[key])
        for key in ("schemaVersion", "project", "launch", "catalog")
    }
    if manifest.get("links"):
        record["links"] = copy.deepcopy(manifest["links"])
    inputs = {"manifest": digest(raw)}
    assets = {}
    for image in images(record):
        raw_image = read_bytes(
            inside(config.parent, image["path"], "catalog.image.path"), 2 * 1024 * 1024
        )
        check_image_bytes(raw_image, Path(image["path"]).suffix.lower())
        inputs[image["path"]] = digest(raw_image)
        try:
            with Image.open(io.BytesIO(raw_image)) as source:
                source.load()
                oriented = ImageOps.exif_transpose(source)
                # A fresh pixel buffer carries no EXIF/XMP/ICC/comments/text chunks.
                pixels = oriented.convert(
                    "RGBA"
                    if "A" in oriented.getbands() or "transparency" in oriented.info
                    else "RGB"
                )
                clean = Image.frombytes(pixels.mode, pixels.size, pixels.tobytes())
                target = io.BytesIO()
                clean.save(target, format="PNG", optimize=True)
                content = target.getvalue()
        except (OSError, ValueError, Warning) as exc:
            raise PirafuError("IMAGE_REENCODE_FAILED", "登録用画像を生成できません。", 3) from exc
        if len(content) > 2 * 1024 * 1024:
            raise PirafuError(
                "IMAGE_REENCODE_TOO_LARGE",
                "再エンコード後の画像が2 MiBを超えています。画像を縮小してください。",
                3,
            )
        path = f"media/{record['project']['id']}/{digest(content)}.png"
        image["path"] = path
        assets[f"catalog/{path}"] = content
    record["publication"] = {
        "visibility": record["project"]["visibility"],
        "launchEnabled": record["launch"]["type"] == "external",
    }
    validate(record, "catalog")
    if raw != read_bytes(config):
        raise PirafuError(
            "REGISTER_INPUT_CHANGED", "検査中に設定が変わりました。再実行してください。", 7
        )
    return record, assets, digest(serialized(inputs))


def catalog_records(records: dict[str, bytes], categories: bytes) -> dict:
    category_rows = json_data(categories)
    if not isinstance(category_rows, list) or any(
        not isinstance(row, dict) for row in category_rows
    ):
        raise PirafuError("CATEGORIES_INVALID", "カテゴリー一覧が不正です。", 7)
    category_ids = [row.get("id") for row in category_rows]
    if any(not isinstance(value, str) for value in category_ids):
        raise PirafuError("CATEGORIES_INVALID", "カテゴリーIDは文字列で指定してください。", 7)
    if len(category_ids) != len(set(category_ids)):
        raise PirafuError("CATEGORIES_INVALID", "カテゴリーIDが重複しています。", 7)
    found = {}
    for path, content in records.items():
        record = validate(json_data(content), "catalog")
        project = record["project"]
        if (
            path != f"catalog/projects/{project['id']}.json"
            or project["id"] in found
            or project["category"] not in category_ids
        ):
            raise PirafuError("ID_CONFLICT", "カタログID・ファイル名・カテゴリーが不整合です。", 7)
        found[project["id"]] = record
    return found


@dataclass
class Plan:
    result: Result
    config: Path
    github: GitHub | None
    snapshot: Snapshot | None
    files: dict[str, bytes]
    input_hash: str
    update: bool
    fingerprint: str
    confirm_external: bool = False


def plan_registration(
    config: Path,
    *,
    portal_repo: str | None,
    base: str | None,
    update=False,
    offline=False,
    catalog: Path | None = None,
    dry_run=False,
    confirm_external=False,
    github_factory=GitHub,
) -> Plan:
    from pirafu_cli.registration_io import branch_name, repository_name

    if not portal_repo or not base:
        raise PirafuError(
            "PORTAL_TARGET_REQUIRED", "--portal-repo owner/repoと--baseを明示してください。"
        )
    repository_name(portal_repo)
    branch_name(base)
    record, assets, input_hash = prepare(config)
    project_id = record["project"]["id"]
    branch = f"pirafu/register/{project_id}"
    if base == branch:
        raise PirafuError("BASE_INVALID", "登録ブランチをbaseにはできません。")
    local = check(config, target="register", catalog=None, dry_run=dry_run)
    local.command = "register"
    local.warnings = [item for item in local.warnings if item.code != "CATALOG_NOT_CHECKED"]
    local.data.update(
        portalRepository=portal_repo,
        base=base,
        branch=branch,
        record=record,
        previewMayBePublic=True,
        readyToApply=False,
    )
    unmanaged = load_manifest(config.absolute())["hosting"]["provider"] == "external"
    if record["launch"]["type"] == "external" and not offline and unmanaged and confirm_external:
        local.outcome, local.exit_code, local.nextActions = "success", 0, []
        local.warnings.append(
            Notice(
                code="EXTERNAL_CONFIRMED_BY_OPERATOR",
                message="外部ホスティングの公開先は利用者の手動確認に基づきます。API照合は行っていません。",
            )
        )
    elif record["launch"]["type"] == "external" and not offline:
        from pirafu_cli.status import status

        observed = status(config, dry_run=dry_run)
        local.data["hostingObservation"] = observed.data
        if observed.exit_code:
            local.outcome, local.exit_code = observed.outcome, observed.exit_code
            local.errors, local.nextActions = observed.errors, observed.nextActions
            return Plan(local, config, None, None, {}, input_hash, update, "")
        local.outcome, local.exit_code, local.nextActions = "success", 0, []
        local.warnings.extend(observed.warnings)
    if offline:
        if catalog:
            directory = inside(catalog.absolute(), "projects", "catalog.projects")
            if not directory.is_dir():
                raise PirafuError("CATALOG_MISSING", "ローカルカタログがありません。", 3)
            found = catalog_records(
                {f"catalog/projects/{path.name}": read_bytes(path) for path in directory.iterdir()},
                read_bytes(inside(catalog.absolute(), "categories.json", "catalog.categories")),
            )
            local.data["localIdExists"] = project_id in found
        local.outcome, local.exit_code = "needs_action", 5
        local.nextActions = [
            Notice(
                code="REGISTER_ONLINE_REQUIRED",
                message="最新base・登録ブランチ・PRをオンラインで確認してください。",
            )
        ]
        return Plan(local, config, None, None, {}, input_hash, update, "")
    if local.exit_code:
        return Plan(local, config, None, None, {}, input_hash, update, "")
    github = github_factory(portal_repo, base)
    public_repo = record.get("links", {}).get("repository")
    if public_repo:
        github.public_repository(public_repo)
    snapshot = github.snapshot(project_id)
    records = catalog_records(snapshot.records, snapshot.categories)
    category_ids = [row["id"] for row in json_data(snapshot.categories)]
    if record["project"]["category"] not in category_ids:
        raise PirafuError("CATEGORY_MISSING", "登録するカテゴリーがPortalにありません。", 7)
    path = f"catalog/projects/{project_id}.json"
    old = records.get(project_id)
    base_old = (
        validate(json_data(github.blob(snapshot.base_tree, path)), "catalog")
        if path in snapshot.base_tree
        else None
    )
    for prior in (old, base_old):
        if prior and prior.get("links", {}).get("repository") != public_repo:
            raise PirafuError(
                "ID_CONFLICT",
                "既存IDのrepositoryが異なります。移管・名称変更は手動レビューが必要です。",
                7,
            )
    if old:
        record["publication"] = copy.deepcopy((base_old or old)["publication"])
    if base_old and not update and old != record:
        raise PirafuError(
            "REGISTER_UPDATE_REQUIRED", "既存IDの変更には--updateを指定してください。", 7
        )
    validate(record, "catalog")
    candidates = {path: serialized(record), **assets}
    files = {}
    changes = []
    for destination, content in candidates.items():
        existing = snapshot.tree.get(destination)
        if existing:
            if existing.get("mode") != "100644":
                raise PirafuError(
                    "PORTAL_FILE_INVALID", "登録先ファイルが通常ファイルではありません。", 7
                )
            if destination == path and old == record:
                continue
            if existing.get("sha") == blob_sha(content):
                continue
            if destination != path:
                raise PirafuError("IMAGE_HASH_CONFLICT", "同名の画像に異なる内容があります。", 7)
        files[destination] = content
        changes.append(
            {
                "path": destination,
                "action": "update" if existing else "create",
                "sha256": digest(content),
                "bytes": len(content),
            }
        )
    # Existing branch images must really match their immutable filenames.
    if snapshot.head:
        for destination in snapshot.tree.keys() - snapshot.base_tree.keys():
            if (
                destination.startswith(f"catalog/media/{project_id}/")
                and snapshot.tree[destination].get("type") != "tree"
            ):
                content = github.blob(snapshot.tree, destination, limit=2 * 1024 * 1024)
                if Path(destination).name != f"{digest(content)}.png":
                    raise PirafuError(
                        "IMAGE_HASH_CONFLICT", "登録ブランチの画像ハッシュが不一致です。", 7
                    )
    fingerprint = digest(
        serialized(
            {
                "repository": portal_repo,
                "base": base,
                "branch": branch,
                "snapshot": snapshot.signature(),
                "input": input_hash,
                "update": update,
                "confirmExternal": confirm_external,
                "files": {name: digest(content) for name, content in sorted(candidates.items())},
            }
        )
    )
    local.data.update(
        record=record,
        operation="update" if base_old else "create",
        planHash=fingerprint,
        baseSha=snapshot.base,
        headSha=snapshot.head,
        readyToApply=True,
        existingPullRequest=snapshot.pull,
        previousRecord=old,
        catalogIdCheck="existing_same_id" if old else "unique_in_snapshot",
    )
    local.changes = changes
    if not files and (snapshot.head is None or snapshot.pull or snapshot.at_base):
        local.outcome = "no_change"
    else:
        local.outcome = "planned"
        local.nextActions = [
            Notice(
                code="CONFIRM_REGISTER",
                message="差分と公開プレビューの可能性を確認し、--applyで適用してください。",
            )
        ]
    return Plan(
        local, config, github, snapshot, files, input_hash, update, fingerprint, confirm_external
    )


def apply_registration(plan: Plan, *, expect_plan: str | None = None) -> Result:
    if not plan.github or not plan.snapshot or not plan.fingerprint or plan.result.exit_code:
        raise PirafuError("REGISTER_NOT_READY", "適用可能な登録計画がありません。", 5)
    if plan.result.dryRun:
        return plan.result
    if expect_plan is not None and expect_plan != plan.fingerprint:
        raise PirafuError(
            "REGISTER_PLAN_CHANGED",
            "指定した計画と現在の入力・リモートが一致しません。再計画してください。",
            7,
        )
    github = plan.github
    refreshed = plan_registration(
        plan.config,
        portal_repo=github.repository,
        base=github.base,
        update=plan.update,
        github_factory=lambda *_: github,
        confirm_external=plan.confirm_external,
    )
    if refreshed.fingerprint != plan.fingerprint:
        raise PirafuError(
            "REGISTER_PLAN_CHANGED",
            "承認後に入力・base・head・PRが変わりました。再計画してください。",
            7,
        )
    if plan.result.outcome == "no_change":
        return plan.result
    result = plan.result.model_copy(deep=True)
    snapshot = plan.snapshot
    branch = result.data["branch"]
    pushed = False
    with tempfile.TemporaryDirectory(prefix="pirafu-register-") as temporary:
        root = Path(temporary)
        git_dir = root / "repository.git"
        hooks = root / "empty-hooks"
        hooks.mkdir()
        user = snapshot.user
        identity = f"{user['id']}+{user['login']}@users.noreply.github.com"
        environment = {
            "GIT_AUTHOR_NAME": user["login"],
            "GIT_COMMITTER_NAME": user["login"],
            "GIT_AUTHOR_EMAIL": identity,
            "GIT_COMMITTER_EMAIL": identity,
        }

        def git(*arguments, content=None):
            code, output = external(
                "git",
                [
                    "-c",
                    f"core.hooksPath={hooks.as_posix()}",
                    "-c",
                    "credential.helper=",
                    "-c",
                    "credential.helper=!gh auth git-credential",
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "commit.gpgsign=false",
                    f"--git-dir={git_dir}",
                    *arguments,
                ],
                content=content,
                environment=environment,
            )
            if code:
                raise PirafuError(
                    "REGISTER_GIT_FAILED",
                    "登録用Git操作に失敗しました。force pushは行いません。",
                    7,
                )
            return output.strip()

        try:
            git("init", "--bare")
            git("remote", "add", "origin", f"https://github.com/{github.repository}.git")
            git("fetch", "--no-tags", "origin", f"refs/heads/{github.base}")
            if git("rev-parse", "FETCH_HEAD").decode() != snapshot.base:
                raise PirafuError(
                    "REGISTER_BASE_CHANGED", "適用前にbaseが変わりました。再計画してください。", 7
                )
            parent = snapshot.base if snapshot.at_base else snapshot.head or snapshot.base
            if snapshot.head and (not snapshot.at_base or snapshot.reconcile):
                git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
                if git("rev-parse", "FETCH_HEAD").decode() != snapshot.head:
                    raise PirafuError("REGISTER_HEAD_CHANGED", "適用前にheadが変わりました。", 7)
            new_head = parent
            if plan.files:
                git("read-tree", parent)
                for destination, content in plan.files.items():
                    sha = git("hash-object", "-w", "--stdin", content=content).decode()
                    if sha != blob_sha(content):
                        raise PirafuError(
                            "REGISTER_BLOB_MISMATCH", "生成したGitオブジェクトが一致しません。", 7
                        )
                    git("update-index", "--add", "--cacheinfo", f"100644,{sha},{destination}")
                tree = git("write-tree").decode()
                project_id = result.data["projectId"]
                message = f"Register {project_id}\n\nPirafu-Register: {project_id}\n"
                parents = (
                    ["-p", snapshot.head, "-p", snapshot.base]
                    if snapshot.reconcile
                    else ["-p", parent]
                )
                new_head = git("commit-tree", tree, *parents, content=message.encode()).decode()
            if github.ref(github.base) != snapshot.base or github.ref(branch) != snapshot.head:
                raise PirafuError(
                    "REGISTER_REMOTE_CHANGED", "push直前にリモートが変わりました。", 7
                )
            if plan.files:
                pushed = True
                git("push", "--porcelain", "origin", f"{new_head}:refs/heads/{branch}")
            result.data["pushedHeadSha"] = new_head
            # Re-read before PR creation. Never merge or bypass required PR checks.
            current = github.snapshot(result.data["projectId"])
            if current.base != snapshot.base or current.head != new_head:
                raise PirafuError(
                    "REGISTER_REMOTE_CHANGED",
                    "push後にリモートが変わりました。既存ブランチを手動確認してください。",
                    7,
                )
            if current.pull:
                result.data["pullRequest"] = current.pull
            else:
                body = root / "body.txt"
                body.write_text(
                    f"作品 `{result.data['projectId']}` の公開カタログを登録・更新します。\n\n"
                    "画像はメタデータを除去して再エンコード済みです。既存の掲載・起動停止状態を維持します。\n"
                    "Pagesのプレビューは公開される可能性があります。自動マージは行いません。\n",
                    encoding="utf-8",
                )
                code, output = external(
                    "gh",
                    [
                        "pr",
                        "create",
                        "--repo",
                        github.repository,
                        "--base",
                        github.base,
                        "--head",
                        branch,
                        "--title",
                        f"Register {result.data['projectId']}",
                        "--body-file",
                        str(body),
                        "--no-maintainer-edit",
                    ],
                    cwd=root,
                )
                if code:
                    raise PirafuError(
                        "REGISTER_PR_FAILED",
                        "PR作成に失敗しました。再実行で既存ブランチを確認・回収します。",
                        6,
                    )
                import re

                match = re.fullmatch(
                    rf"https://github\.com/{re.escape(github.repository)}/pull/([1-9][0-9]*)",
                    output.decode().strip(),
                )
                if not match:
                    raise PirafuError(
                        "REGISTER_PR_UNVERIFIED",
                        "PR作成結果を確認できません。再実行してください。",
                        6,
                    )
                result.data["pullRequest"] = {"number": int(match[1]), "url": match[0]}
            result.outcome = "success"
            result.nextActions = [
                Notice(
                    code="REVIEW_REGISTER_PR",
                    message="PRの差分・CI・プレビューを確認してからマージしてください。",
                )
            ]
        except PirafuError as exc:
            result.outcome = "error"
            result.exit_code = exc.exit_code
            result.errors = [exc.notice]
            result.data["branchMayExist"] = pushed or snapshot.head is not None
            result.nextActions = [
                Notice(
                    code="RETRY_REGISTER",
                    message="同じ対象でregisterを再実行し、既存ブランチとPRを確認してください。",
                )
            ]
    return result
