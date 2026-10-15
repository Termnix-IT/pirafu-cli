import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from pirafu_cli.cloudflare import PagesReader
from pirafu_cli.contracts import validate
from pirafu_cli.files import load_manifest
from pirafu_cli.results import Notice, PirafuError, Result


def _object(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PirafuError("CLOUDFLARE_PAYLOAD_INVALID", "API応答の形式が不正です。", 6)
    return value


def deployment(value) -> dict:
    if not isinstance(value, dict):
        return {"state": "unknown", "environment": "unknown"}
    stage = _object(value.get("latest_stage"))
    state = stage.get("status")
    stage_name = stage.get("name")
    if value.get("is_skipped") is True:
        state = "skipped"
    elif state == "success" and stage_name != "deploy":
        state = "building"
    elif isinstance(state, str) and state in {"idle", "active"}:
        state = "pending" if stage_name == "queued" else "building"
    elif not isinstance(state, str) or state not in {"success", "failure", "canceled"}:
        state = "unknown"
    environment = value.get("environment")
    result = {
        "state": state,
        "environment": environment
        if isinstance(environment, str) and environment in {"production", "preview"}
        else "unknown",
    }
    metadata = _object(_object(value.get("deployment_trigger")).get("metadata"))
    sha = metadata.get("commit_hash", "")
    if isinstance(sha, str) and re.fullmatch(r"[a-fA-F0-9]{7,64}", sha):
        result["commit"] = sha.lower()
    identifier = value.get("id", "")
    if isinstance(identifier, str) and re.fullmatch(r"[a-fA-F0-9-]{32,36}", identifier):
        result["id"] = identifier
    created = value.get("created_on")
    if isinstance(created, str) and len(created) <= 40:
        try:
            result["createdAt"] = datetime.fromisoformat(created).isoformat()
        except ValueError:
            pass
    return result


def _directory(value):
    if not isinstance(value, str):
        return None
    return "" if value in {"", ".", "/"} else value.strip("/")


def inspect_project(project: dict, manifest: dict, *, dry_run=False, expected=None) -> Result:
    hosting = manifest["hosting"]
    result = Result(
        command="status",
        dryRun=dry_run,
        data={
            "projectId": manifest["project"]["id"],
            "provider": "cloudflare-pages",
            "authentication": "verified",
            "configuration": "not_checked",
        },
    )
    if project.get("name") != hosting["projectName"]:
        raise PirafuError("PAGES_IDENTITY_MISMATCH", "API応答の対象プロジェクトが一致しません。", 6)
    subdomain = project.get("subdomain")
    if not isinstance(subdomain, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]*\.pages\.dev", subdomain
    ):
        raise PirafuError("PAGES_SUBDOMAIN_INVALID", "実際のPages本番ホストを確認できません。", 6)
    result.data["productionUrl"] = f"https://{subdomain}/"
    current = deployment(project.get("canonical_deployment"))
    latest = deployment(project.get("latest_deployment"))
    result.data.update({"currentProduction": current, "latestAttempt": latest})
    source = _object(project.get("source"))
    source_config = _object(source.get("config"))
    if source.get("type") != "github":
        result.outcome = "needs_action"
        result.exit_code = 5
        result.nextActions.append(
            Notice(
                code="CONNECT_GIT_MANUALLY", message="対象リポジトリのGit連携を確認してください。"
            )
        )
        return result
    mismatches = []
    if (
        project.get("production_branch") != hosting["productionBranch"]
        or source_config.get("production_branch") != hosting["productionBranch"]
    ):
        mismatches.append("hosting.productionBranch")
    build = _object(project.get("build_config"))
    if _directory(build.get("root_dir")) != _directory(hosting["rootDirectory"]):
        mismatches.append("hosting.rootDirectory")
    if _directory(build.get("destination_dir")) != _directory(hosting["outputDirectory"]):
        mismatches.append("hosting.outputDirectory")
    if build.get("build_command") != hosting["buildCommand"]:
        mismatches.append("hosting.buildCommand")
    repository = manifest.get("links", {}).get("repository")
    if repository:
        url = urlsplit(repository)
        wanted = url.path.removesuffix("/").removesuffix(".git").lower()
        actual = f"/{source_config.get('owner', '')}/{source_config.get('repo_name', '')}".lower()
        if url.hostname != "github.com" or wanted != actual:
            mismatches.append("links.repository")
    else:
        result.warnings.append(
            Notice(
                code="REPOSITORY_NOT_CHECKED",
                message="公開repositoryが未指定のためGit接続先の同一性は未確認です。",
            )
        )
    launch_url = manifest["launch"].get("url")
    if launch_url and (
        urlsplit(launch_url).hostname != subdomain or urlsplit(launch_url).port not in {None, 443}
    ):
        mismatches.append("launch.url")
    if mismatches:
        result.data["configuration"] = "mismatch"
        result.data["mismatchedFields"] = mismatches
        result.outcome, result.exit_code = "error", 7
        result.errors.append(
            Notice(
                code="PAGES_CONFIGURATION_MISMATCH",
                message="設定とPagesの実状態に差異があります。自動変更は行いません。",
            )
        )
        return result
    result.data["configuration"] = "matched"
    differs = (
        latest["environment"] == "production"
        and "commit" in latest
        and "commit" in current
        and latest["commit"] != current["commit"]
    )
    result.data["productionDiffersFromLatestAttempt"] = differs
    if differs:
        result.warnings.append(
            Notice(
                code="PRODUCTION_DIFFERS",
                message="現在の本番と最新の本番試行のSHAが異なります。切り戻し・失敗・スキップを確認してください。",
            )
        )
    failed = latest["environment"] == "production" and latest["state"] in {"failure", "canceled"}
    if failed and (expected is None or latest.get("commit") == expected):
        result.outcome, result.exit_code = "error", 3
        result.errors.append(
            Notice(
                code="LATEST_PRODUCTION_FAILED",
                message="最新の本番試行が失敗・キャンセルされています。現在配信中の本番状態とは別です。",
            )
        )
    elif current["environment"] != "production" or current["state"] != "success":
        result.outcome, result.exit_code = "needs_action", 5
        result.nextActions.append(
            Notice(code="DEPLOYMENT_PENDING", message="成功済みの現在の本番をまだ確認できません。")
        )
    elif expected and current.get("commit") != expected:
        result.outcome, result.exit_code = "needs_action", 5
        result.nextActions.append(
            Notice(code="EXPECTED_COMMIT_PENDING", message="指定SHAが現在の本番になっていません。")
        )
    elif latest["environment"] == "production" and latest["state"] in {"pending", "building"}:
        result.outcome, result.exit_code = "needs_action", 5
        result.nextActions.append(
            Notice(code="DEPLOYMENT_PENDING", message="本番の新しい試行が処理中です。")
        )
    elif latest["state"] in {"failure", "canceled", "unknown", "skipped"}:
        result.warnings.append(
            Notice(
                code="LATEST_ATTEMPT_NOT_SUCCESS",
                message="最新試行の状態はcurrentProductionと分けて確認してください。",
            )
        )
    return result


def status(
    config: Path,
    *,
    offline=False,
    dry_run=False,
    wait=False,
    timeout=120.0,
    interval=5.0,
    expected=None,
    reader_factory=None,
) -> Result:
    if not 0 < timeout <= 600 or not 0 < interval <= 30:
        raise PirafuError(
            "WAIT_OPTIONS_INVALID", "timeoutは600秒以下、intervalは30秒以下の正数にしてください。"
        )
    if expected is not None and not re.fullmatch(r"[a-fA-F0-9]{40,64}", expected):
        raise PirafuError("COMMIT_INVALID", "完全なGit SHAを指定してください。")
    expected = expected.lower() if expected else None
    manifest = validate(load_manifest(config.absolute()))
    provider = manifest["hosting"]["provider"]
    if provider != "cloudflare-pages":
        result = Result(
            command="status",
            dryRun=dry_run,
            data={"provider": provider, "deployment": "not_applicable"},
        )
        if provider != "none":
            result.outcome, result.exit_code = "needs_action", 5
            result.nextActions.append(
                Notice(
                    code="VERIFY_EXTERNAL_MANUALLY",
                    message="外部ホスティングは手動で確認してください。",
                )
            )
        return result
    if offline:
        return Result(
            command="status",
            dryRun=dry_run,
            outcome="needs_action",
            exit_code=5,
            data={"deployment": "unknown", "authentication": "not_checked"},
            nextActions=[
                Notice(
                    code="ONLINE_REQUIRED", message="公開状態の照合にはオンライン実行が必要です。"
                )
            ],
        )
    deadline = time.monotonic() + timeout
    reader = (reader_factory or PagesReader)()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PirafuError("STATUS_TIMEOUT", "公開状態の待機期限を超えました。", 6)
            project = reader.get_project(
                manifest["hosting"]["projectName"], budget=min(20, remaining)
            )
            result = inspect_project(project, manifest, dry_run=dry_run, expected=expected)
            pending = result.nextActions and result.nextActions[0].code in {
                "DEPLOYMENT_PENDING",
                "EXPECTED_COMMIT_PENDING",
            }
            if not wait or not pending:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result.outcome, result.exit_code = "error", 6
                result.errors.append(
                    Notice(code="STATUS_TIMEOUT", message="公開状態の待機期限を超えました。")
                )
                return result
            time.sleep(min(interval, remaining))
    finally:
        reader.close()
