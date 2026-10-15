from pathlib import Path

from pirafu_cli.contracts import validate
from pirafu_cli.files import load_manifest
from pirafu_cli.results import Notice, PirafuError, Result
from pirafu_cli.services import check


def publish(config: Path, *, offline=False, dry_run=False) -> Result:
    manifest = validate(load_manifest(config.absolute()))
    result = check(config, target="publish", catalog=None, dry_run=dry_run, online=False)
    result.command = "publish"
    result.data.update({"performedDeployment": False, "externalChanges": False})
    hosting = manifest["hosting"]
    if hosting["provider"] == "none":
        result.outcome, result.exit_code = "no_change", 0
        return result
    result.data["setupProposal"] = hosting.copy()
    if offline:
        result.outcome, result.exit_code = "needs_action", 5
        result.nextActions = [
            Notice(
                code="VERIFY_HOSTING_ONLINE",
                message="オンラインでPagesの設定と本番を照合してください。",
            )
        ]
        return result
    from pirafu_cli.status import status

    try:
        observed = status(config, dry_run=dry_run)
    except PirafuError as exc:
        result.outcome = "needs_action" if exc.exit_code == 5 else "error"
        result.exit_code = exc.exit_code
        result.errors.append(exc.notice)
        result.nextActions = []
        if exc.notice.code == "PAGES_NOT_FOUND":
            result.nextActions.append(
                Notice(
                    code="CONNECT_GIT_MANUALLY",
                    message="Cloudflare管理画面で対象リポジトリだけを許可し、表示した設定でPagesのGit連携を作成してください。",
                    hint="https://developers.cloudflare.com/pages/framework-guides/deploy-anything/",
                )
            )
        return result
    result.data["hostingObservation"] = observed.data
    result.outcome, result.exit_code = observed.outcome, observed.exit_code
    result.errors, result.nextActions = observed.errors, observed.nextActions
    result.warnings.extend(observed.warnings)
    if result.exit_code == 0:
        result.nextActions.append(
            Notice(
                code="VERIFY_APP_AND_REGISTER",
                message="本番で作品の操作を確認し、確認済みURLを設定してregisterの計画を確認してください。",
            )
        )
    return result
