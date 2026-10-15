import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from pirafu_cli import __version__
from pirafu_cli.preview import preview, stop_preview
from pirafu_cli.results import Notice, PirafuError, Result
from pirafu_cli.services import check, doctor, initialize
from pirafu_cli.status import status

app = typer.Typer(
    help="Pirafu2411 Playgroundの公開準備CLI（Phase 2B）。",
    no_args_is_help=True,
    invoke_without_command=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
Config = Annotated[Path, typer.Option("--config", help="作品設定ファイル。")]
Json = Annotated[bool, typer.Option("--json", help="stdoutへ結果JSONを1件だけ出力。")]
DryRun = Annotated[bool, typer.Option("--dry-run", help="変更・ビルド・サーバー起動を行わない。")]
NonInteractive = Annotated[bool, typer.Option("--non-interactive", help="入力待ちを行わない。")]
Offline = Annotated[bool, typer.Option("--offline", help="認証照会を含む通信を行わない。")]


@app.callback()
def root(version: Annotated[bool, typer.Option("--version", is_eager=True)] = False):
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command("init")
def init_command(
    config: Config = Path("pirafu.yaml"),
    json_output: Json = False,
    dry_run: DryRun = False,
    non_interactive: NonInteractive = False,
    offline: Offline = False,
    template: Annotated[str | None, typer.Option(help="showcase/html/vite/manual。")] = None,
    project_id: Annotated[str | None, typer.Option("--id")] = None,
    title: Annotated[str | None, typer.Option()] = None,
    description: Annotated[str | None, typer.Option()] = None,
    category: Annotated[str, typer.Option()] = "other",
    project_name: Annotated[str | None, typer.Option()] = None,
    production_branch: Annotated[str | None, typer.Option()] = None,
    output_directory: Annotated[str | None, typer.Option()] = None,
    repository: Annotated[str | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes", help="表示する新規設定の作成を承認する。")] = False,
) -> Result:
    options = dict(
        config=config,
        template=template,
        project_id=project_id,
        title=title,
        description=description,
        category=category,
        project_name=project_name,
        branch=production_branch,
        output=output_directory,
        repository=repository,
        dry_run=dry_run,
        yes=yes,
    )
    result = initialize(**options)
    if (
        not json_output
        and not non_interactive
        and not dry_run
        and not yes
        and sys.stdin.isatty()
        and result.nextActions
        and result.nextActions[0].code == "CONFIRM_CREATE"
    ):
        typer.echo(json.dumps(result.changes, ensure_ascii=False, indent=2), err=True)
        if typer.confirm("この設定ファイルを新規作成しますか？", err=True):
            options["yes"] = True
            return initialize(**options)
    return result


@app.command("check")
def check_command(
    config: Config = Path("pirafu.yaml"),
    json_output: Json = False,
    dry_run: DryRun = False,
    non_interactive: NonInteractive = False,
    offline: Offline = False,
    target: Annotated[str, typer.Option(help="local/publish/register。")] = "local",
    catalog: Annotated[Path | None, typer.Option(help="Portalのcatalogディレクトリ。")] = None,
    online: Annotated[
        bool, typer.Option(help="Cloudflareの本番と設定を読み取り照合する。")
    ] = False,
) -> Result:
    """設定・パス・画像を検証する。ビルド・ネットワーク通信は行わない。"""
    if online and offline:
        raise PirafuError("ARGUMENT_INVALID", "--onlineと--offlineは同時指定できません。")
    return check(config, target=target, catalog=catalog, dry_run=dry_run, online=online)


@app.command("doctor")
def doctor_command(
    config: Config = Path("pirafu.yaml"),
    json_output: Json = False,
    dry_run: DryRun = False,
    non_interactive: NonInteractive = False,
    offline: Offline = False,
) -> Result:
    """必要ツールと認証を診断する。インストール・ログインは行わない。"""
    return doctor(offline=offline, dry_run=dry_run, config=config)


@app.command("status")
def status_command(
    config: Config = Path("pirafu.yaml"),
    json_output: Json = False,
    dry_run: DryRun = False,
    non_interactive: NonInteractive = False,
    offline: Offline = False,
    wait: Annotated[bool, typer.Option(help="指定期限まで本番を待つ。")] = False,
    timeout: Annotated[float, typer.Option()] = 120.0,
    interval: Annotated[float, typer.Option()] = 5.0,
    expect_commit: Annotated[str | None, typer.Option()] = None,
) -> Result:
    """現在の本番と最新試行を区別して照会する。外部変更は行わない。"""
    return status(
        config,
        offline=offline,
        dry_run=dry_run,
        wait=wait,
        timeout=timeout,
        interval=interval,
        expected=expect_commit,
    )


@app.command("preview")
def preview_command(
    config: Config = Path("pirafu.yaml"),
    json_output: Json = False,
    dry_run: DryRun = False,
    non_interactive: NonInteractive = False,
    offline: Offline = False,
    port: Annotated[int, typer.Option()] = 0,
    ttl: Annotated[int, typer.Option(help="プレビューの自動終了までの秒数。")] = 3600,
    spa: Annotated[bool, typer.Option(help="拡張子のない未存在パスをindex.htmlへ戻す。")] = False,
    background: Annotated[bool, typer.Option()] = False,
    run_command: Annotated[bool, typer.Option(help="対応するlocal.previewを実行する。")] = False,
    yes: Annotated[bool, typer.Option()] = False,
    stop: Annotated[str | None, typer.Option(help="指定セッションを停止する。")] = None,
) -> Result:
    """127.0.0.1でプレビュー。JSON・非対話はバックグラウンドで起動する。"""
    if stop:
        return stop_preview(stop, dry_run=dry_run)
    return preview(
        config,
        dry_run=dry_run,
        background=background or json_output or non_interactive,
        run_command=run_command,
        yes=yes,
        port=port,
        ttl=ttl,
        spa=spa,
    )


def main(args: list[str] | None = None) -> int:
    if args is None:
        # JSONをパイプで受けるCodex等へ、WindowsでもUTF-8で返す。
        for stream in (sys.stdout, sys.stderr):
            if not stream.isatty() and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    argv = sys.argv[1:] if args is None else args
    json_output = "--json" in argv
    dry_run = "--dry-run" in argv
    command = next(
        (arg for arg in argv if arg in {"init", "check", "doctor", "status", "preview"}), "cli"
    )
    try:
        result = app(args=argv, standalone_mode=False)
        if not isinstance(result, Result):
            return 0
    except PirafuError as exc:
        result = Result(
            command=command,
            dryRun=dry_run,
            outcome="needs_action" if exc.exit_code == 5 else "error",
            errors=[exc.notice],
            exit_code=exc.exit_code,
        )
    except typer.TyperException:
        result = Result(
            command=command,
            dryRun=dry_run,
            outcome="error",
            exit_code=2,
            errors=[
                Notice(code="ARGUMENT_INVALID", message="引数が不正です。helpを確認してください。")
            ],
        )
    except (KeyboardInterrupt, typer.Abort):
        result = Result(
            command=command,
            dryRun=dry_run,
            outcome="needs_action",
            exit_code=130,
            errors=[Notice(code="INTERRUPTED", message="操作を中断しました。")],
        )
    except OSError:
        result = Result(
            command=command,
            dryRun=dry_run,
            outcome="error",
            exit_code=3,
            errors=[Notice(code="IO_FAILED", message="ファイルまたはツールを操作できません。")],
        )
    except Exception:
        result = Result(
            command=command,
            dryRun=dry_run,
            outcome="error",
            exit_code=1,
            errors=[Notice(code="INTERNAL_ERROR", message="内部エラーが発生しました。")],
        )
    if json_output:
        typer.echo(result.model_dump_json(exclude_none=True))
    else:
        typer.echo(f"{result.command}: {result.outcome}")
        if result.data:
            typer.echo(json.dumps(result.data, ensure_ascii=False, indent=2))
        if result.changes:
            typer.echo(json.dumps(result.changes, ensure_ascii=False, indent=2))
        for notice in [*result.warnings, *result.errors, *result.nextActions]:
            typer.echo(f"{notice.code}: {notice.message}", err=True)
    return result.exit_code
