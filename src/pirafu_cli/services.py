import json
import platform
import re
import shutil
from pathlib import Path

import yaml

from pirafu_cli import __version__
from pirafu_cli.contracts import bundle, images, validate
from pirafu_cli.files import inside, load_manifest, no_links, read_json
from pirafu_cli.media import check_image
from pirafu_cli.processes import run_tool
from pirafu_cli.results import Notice, PirafuError, Result


def needs_action(result: Result, code: str, message: str) -> Result:
    result.outcome = "needs_action"
    result.exit_code = 5
    result.nextActions.append(Notice(code=code, message=message))
    return result


def detect(root: Path) -> list[str]:
    candidates = []
    package = root / "package.json"
    if package.exists():
        data = read_json(package)
        if isinstance(data, dict):
            if any(
                not isinstance(data.get(key, {}), dict)
                for key in ("dependencies", "devDependencies", "scripts")
            ):
                raise PirafuError("PACKAGE_INVALID", "package.jsonの構成情報が不正です。")
            dependencies = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            if "vite" in dependencies and isinstance(data.get("scripts", {}).get("build"), str):
                candidates.append("vite")
    if (root / "index.html").is_file() or (root / "public/index.html").is_file():
        candidates.append("html")
    if (root / "project.godot").is_file():
        candidates.append("godot-manual")
    return candidates


def initialize(
    config: Path,
    *,
    template: str | None,
    project_id: str | None,
    title: str | None,
    description: str | None,
    category: str,
    project_name: str | None,
    branch: str | None,
    output: str | None,
    repository: str | None,
    dry_run: bool,
    yes: bool,
) -> Result:
    result = Result(command="init", dryRun=dry_run)
    config = config.absolute()
    no_links(config)
    if config.exists():
        raise PirafuError("CONFIG_EXISTS", "既存設定は上書きしません。", 7)
    if not config.parent.is_dir():
        raise PirafuError("ROOT_MISSING", "設定の配置先ディレクトリが存在しません。", 3)
    candidates = detect(config.parent)
    result.data = {"candidates": candidates, "detectedOnly": True}
    if template is None:
        return needs_action(result, "SELECT_TEMPLATE", "--templateで構成を明示してください。")
    if template not in {"showcase", "html", "vite", "manual"}:
        raise PirafuError("TEMPLATE_INVALID", "対応テンプレートを指定してください。")
    missing = [
        name
        for name, value in {
            "id": project_id,
            "title": title,
            "description": description,
            **(
                {"project-name": project_name, "production-branch": branch}
                if template in {"html", "vite"}
                else {}
            ),
        }.items()
        if not value
    ]
    if missing:
        result.data["requiredOptions"] = missing
        return needs_action(
            result, "METADATA_REQUIRED", "requiredOptionsの項目を指定してください。"
        )
    if template == "vite" and template not in candidates:
        raise PirafuError("VITE_NOT_DETECTED", "Viteとbuildスクリプトを確認できません。", 3)
    manifest = {
        "schemaVersion": 1,
        "project": {
            "id": project_id,
            "title": title,
            "description": description,
            "category": category,
            "status": "planned",
            "visibility": "hidden",
            "tags": [],
        },
        "hosting": {"provider": "none", "mode": "none"},
        "launch": {"type": "showcase" if template == "showcase" else "external"},
        "catalog": {"featured": False, "screenshots": []},
    }
    if repository:
        manifest["links"] = {"repository": repository}
    if template == "manual":
        manifest["hosting"] = {"provider": "external", "mode": "unmanaged"}
    elif template in {"html", "vite"}:
        output = output or (
            "dist"
            if template == "vite"
            else ("public" if (config.parent / "public/index.html").is_file() else ".")
        )
        manifest["hosting"] = {
            "provider": "cloudflare-pages",
            "mode": "git",
            "projectName": project_name,
            "productionBranch": branch,
            "rootDirectory": ".",
            "buildCommand": "npm run build" if template == "vite" else "",
            "outputDirectory": output,
        }
        if template == "vite":
            manifest["local"] = {"build": ["npm", "run", "build"]}
        result.warnings.append(
            Notice(
                code="HOSTING_UNVERIFIED",
                message="Pages名の取得可能性・Git連携・本番URLは未確認です。",
            )
        )
    validate(manifest)
    result.data.update({"projectId": project_id, "template": template, "detectedOnly": False})
    result.changes = [{"action": "create", "path": config.name, "content": manifest}]
    if dry_run:
        result.outcome = "planned"
        return result
    if not yes:
        return needs_action(result, "CONFIRM_CREATE", "変更内容を確認し、--yesで生成してください。")
    try:
        with config.open("x", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(manifest, stream, allow_unicode=True, sort_keys=False)
    except FileExistsError as exc:
        raise PirafuError("CONFIG_EXISTS", "既存設定は上書きしません。", 7) from exc
    result.data["created"] = True
    return result


def _check_output(root: Path) -> int:
    no_links(root)
    if not root.is_dir():
        raise PirafuError("OUTPUT_MISSING", "ビルド出力ディレクトリがありません。", 3)
    index = inside(root, "index.html", "hosting.outputDirectory")
    if not index.is_file():
        raise PirafuError("INDEX_MISSING", "配信先にindex.htmlがありません。", 3)
    count = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            no_links(path)
            count += 1
            if count > 20_000:
                raise PirafuError("OUTPUT_LIMIT", "配信物の検査上限を超えました。", 3)
            name = path.name.lower()
            if (
                name.startswith(".env")
                or name
                in {
                    ".git",
                    ".ssh",
                    ".aws",
                    ".codex",
                    ".agents",
                    "node_modules",
                    "pirafu.yaml",
                    "hosts.yml",
                    "id_rsa",
                    "id_ed25519",
                }
                or path.suffix.lower() in {".pem", ".key"}
            ):
                raise PirafuError(
                    "PRIVATE_OUTPUT", "公開対象に管理用・機密候補ファイルがあります。", 3
                )
            if path.is_dir():
                pending.append(path)
            elif not path.is_file() or path.stat().st_size > 25 * 1024 * 1024:
                raise PirafuError("OUTPUT_ASSET_INVALID", "配信物の形式・容量が不適切です。", 3)
    return count


def _check_catalog(root: Path, manifest: dict) -> str:
    root = root.absolute()
    directory = inside(root, "projects", "catalog")
    if not directory.is_dir():
        raise PirafuError("CATALOG_MISSING", "catalog/projectsがありません。", 3)
    ids = set()
    matched = None
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json" or not path.is_file():
            raise PirafuError("CATALOG_INVALID", "カタログに不正なファイルがあります。", 3)
        record = validate(read_json(path), "catalog")
        project_id = record["project"]["id"]
        if project_id in ids or path.stem != project_id:
            raise PirafuError("ID_CONFLICT", "重複IDまたはIDと異なるファイル名があります。", 7)
        ids.add(project_id)
        if project_id == manifest["project"]["id"]:
            matched = record
    if matched:
        repository = manifest.get("links", {}).get("repository")
        if not repository or repository != matched.get("links", {}).get("repository"):
            raise PirafuError("ID_CONFLICT", "既存IDの作品との同一性を確認できません。", 7)
        return "existing_same_repository"
    return "unique_in_snapshot"


def check(config: Path, *, target: str, catalog: Path | None, dry_run: bool) -> Result:
    if target not in {"local", "publish", "register"}:
        raise PirafuError("TARGET_INVALID", "local/publish/registerを指定してください。")
    config = config.absolute()
    manifest = validate(load_manifest(config))
    root = config.parent
    for image in images(manifest):
        check_image(root, image)
    hosting = manifest["hosting"]
    output_count = None
    if hosting["provider"] == "cloudflare-pages":
        build_root = inside(root, hosting["rootDirectory"], "hosting.rootDirectory", allow_dot=True)
        if not build_root.is_dir():
            raise PirafuError("BUILD_ROOT_MISSING", "ビルドルートが見つかりません。", 3)
        output = inside(
            build_root, hosting["outputDirectory"], "hosting.outputDirectory", allow_dot=True
        )
        if target == "publish":
            output_count = _check_output(output)
    id_state = _check_catalog(catalog, manifest) if catalog else "not_checked"
    result = Result(
        command="check",
        dryRun=dry_run,
        data={
            "projectId": manifest["project"]["id"],
            "target": target,
            "localValidation": "passed",
            "readyToPublish": False,
            "catalogIdCheck": id_state,
            "deploymentCheck": "not_checked",
            "imageCount": len(images(manifest)),
            "outputEntries": output_count,
        },
    )
    if not catalog:
        result.warnings.append(
            Notice(code="CATALOG_NOT_CHECKED", message="Portal全体のID重複は未確認です。")
        )
    if manifest["launch"]["type"] == "external" and not manifest["launch"].get("url"):
        result.warnings.append(Notice(code="LAUNCH_URL_MISSING", message="本番URLが未設定です。"))
        if target == "register":
            return needs_action(result, "SET_LAUNCH_URL", "確認済み本番URLを設定してください。")
    if target in {"publish", "register"} and manifest["launch"]["type"] == "external":
        return needs_action(
            result,
            "VERIFY_HOSTING_ONLINE",
            "本番・連携状態のオンライン確認はPhase 2Bで対応します。",
        )
    return result


def doctor(*, offline: bool, dry_run: bool) -> Result:
    _, source = bundle()
    result = Result(
        command="doctor",
        dryRun=dry_run,
        data={
            "version": __version__,
            "python": platform.python_version(),
            "contractRevision": source["commit"],
            "tools": {},
            "githubAuth": "not_checked",
            "credentialStorage": "unknown",
            "cloudflareAuth": "not_checked",
        },
    )
    for name in ("git", "gh", "node", "npm", "uv"):
        available = shutil.which(name) is not None
        item = {"available": available, "required": name in {"git", "gh"}}
        result.data["tools"][name] = item
        if not available and item["required"]:
            result.errors.append(Notice(code="TOOL_MISSING", message=f"{name}が必要です。"))
            result.exit_code = 3
        elif available and name != "npm":
            code, content = run_tool(name, ["--version"])
            match = re.search(rb"(?:version\s+|^v|^uv\s+)([0-9]+(?:\.[0-9]+){1,3})", content)
            item["version"] = match[1].decode("ascii") if match and code == 0 else "unknown"
    if not offline and result.data["tools"]["gh"]["available"]:
        code, content = run_tool(
            "gh",
            [
                "auth",
                "status",
                "--active",
                "--hostname",
                "github.com",
                "--json",
                "hosts",
                "--jq",
                '.hosts["github.com"][] | select(.active) | {state,tokenSource}',
            ],
        )
        try:
            auth = json.loads(content) if code == 0 else {}
        except ValueError:
            auth = {}
        if auth.get("state") == "success":
            result.data["githubAuth"] = "verified"
            storage = auth.get("tokenSource", "")
            result.data["credentialStorage"] = (
                "os_store"
                if storage == "keyring"
                else "environment"
                if storage in {"GH_TOKEN", "GITHUB_TOKEN"}
                else "unverified_or_plaintext"
            )
        else:
            result.data["githubAuth"] = "unavailable"
            result.errors.append(
                Notice(code="GITHUB_AUTH_UNAVAILABLE", message="GitHub認証を確認できません。")
            )
            result.exit_code = result.exit_code or 4
    result.warnings.append(
        Notice(
            code="CLOUDFLARE_AUTH_NOT_CHECKED",
            message="ブラウザーのログインとCLI認証は別です。Cloudflare API認証は未検証です。",
        )
    )
    if result.data["credentialStorage"] in {"unknown", "unverified_or_plaintext"}:
        result.warnings.append(
            Notice(
                code="CREDENTIAL_STORAGE_UNVERIFIED",
                message="資格情報の安全な保存方式は未確認です。",
            )
        )
    if offline:
        result.warnings.append(
            Notice(
                code="OFFLINE_AUTH_NOT_CHECKED", message="オフラインのため認証疎通は未確認です。"
            )
        )
    if result.errors:
        result.outcome = "error"
    return result
