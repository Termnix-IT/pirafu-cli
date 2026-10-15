import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

from pirafu_cli.contracts import validate
from pirafu_cli.files import inside, load_manifest, no_links, read_json
from pirafu_cli.processes import stop_owned
from pirafu_cli.results import Notice, PirafuError, Result
from pirafu_cli.services import _check_output

STARTUP_TIMEOUT = 15.0


def state_directory() -> Path:
    override = os.environ.get("PIRAFU_PREVIEW_STATE_DIR")
    if override:
        return Path(override).absolute()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "pirafu" / "preview"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pirafu-preview"


def state_path(session: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", session):
        raise PirafuError("PREVIEW_SESSION_INVALID", "有効なセッションIDを指定してください。")
    path = state_directory() / f"{session}.json"
    no_links(path)
    return path


def child_environment() -> dict:
    allowed = {
        "PATH",
        "SystemRoot",
        "SYSTEMROOT",
        "WINDIR",
        "PATHEXT",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "PIRAFU_PREVIEW_STATE_DIR",
        "XDG_CACHE_HOME",
    }
    result = {key: value for key, value in os.environ.items() if key in allowed}
    result.update({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1"})
    return result


def available_port(requested=0) -> int:
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", requested))
            return sock.getsockname()[1]
    except OSError as exc:
        raise PirafuError("PREVIEW_PORT_BUSY", "ローカルポートを確保できません。", 3) from exc


def vite_argv(root: Path, manifest: dict, port: int) -> list[str]:
    argv = manifest.get("local", {}).get("preview")
    if not isinstance(argv, list) or len(argv) != 3 or argv[:2] != ["npm", "run"]:
        raise PirafuError(
            "PREVIEW_COMMAND_UNSUPPORTED",
            "local.previewは対応するnpm runのVite構成を指定してください。",
            5,
        )
    package = read_json(root / "package.json")
    script = package.get("scripts", {}).get(argv[2]) if isinstance(package, dict) else None
    if not isinstance(script, str) or not re.fullmatch(
        r"vite(?: preview)?(?: --host 127\.0\.0\.1)?", script
    ):
        raise PirafuError(
            "PREVIEW_COMMAND_UNSUPPORTED",
            "Vite以外・複合シェルスクリプトは手動プレビューにしてください。",
            5,
        )
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise PirafuError("TOOL_MISSING", "コマンドプレビューにはNode/npmが必要です。", 3)
    npm_cli = (
        Path(node).parent / "node_modules/npm/bin/npm-cli.js"
        if os.name == "nt"
        else Path(npm).resolve()
    )
    if npm_cli.name != "npm-cli.js" or not npm_cli.is_file():
        raise PirafuError(
            "NPM_LAYOUT_UNSUPPORTED",
            "npmの実行構成を確認できません。静的プレビューを利用してください。",
            5,
        )
    return [
        node,
        str(npm_cli),
        "--ignore-scripts",
        "run",
        argv[2],
        "--",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--strictPort",
    ]


def _control(state: dict, method="GET") -> bool:
    port = state.get("controlPort")
    token = state.get("token")
    if (
        type(port) is not int
        or not 1 <= port <= 65535
        or not isinstance(token, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token)
    ):
        raise PirafuError("PREVIEW_STATE_INVALID", "ローカルセッション情報が不正です。", 3)
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=1) as client:
            response = client.request(
                method,
                f"http://127.0.0.1:{port}/_pirafu/session",
                headers={"Authorization": f"Bearer {token}"},
            )
        return response.status_code == 200 and response.json() == {"session": state["session"]}
    except (httpx.HTTPError, ValueError):
        return False


def stop_preview(session: str, *, dry_run=False) -> Result:
    path = state_path(session)
    result = Result(command="preview", dryRun=dry_run, data={"session": session})
    if not path.exists():
        result.outcome = "no_change"
        return result
    state = read_json(path)
    if not isinstance(state, dict) or state.get("session") != session:
        raise PirafuError("PREVIEW_STATE_INVALID", "ローカルセッション情報が不正です。", 3)
    result.changes = [{"action": "stop_local_preview", "session": session}]
    if dry_run:
        result.outcome = "planned"
        return result
    if not _control(state):
        result.outcome, result.exit_code = "needs_action", 5
        result.data.update({"stopped": False, "control": "not_reachable"})
        result.nextActions.append(
            Notice(
                code="PREVIEW_CONTROL_UNAVAILABLE",
                message="制御先を確認できません。TTLによる終了を待ち、再確認してください。",
            )
        )
        return result
    if not _control(state, "POST"):
        raise PirafuError(
            "PREVIEW_STOP_FAILED", "プレビュー停止を受け付けてもらえませんでした。", 6
        )
    # PIDによるkillはしない。制御先がない場合は期限切れセッションとして整理する。
    deadline = time.monotonic() + 5
    while _control(state):
        if time.monotonic() >= deadline:
            raise PirafuError("PREVIEW_STOP_TIMEOUT", "プレビュー停止の期限を超えました。", 6)
        time.sleep(0.05)
    path.unlink(missing_ok=True)
    result.data["stopped"] = True
    return result


def preview(
    config: Path,
    *,
    dry_run=False,
    background=False,
    run_command=False,
    yes=False,
    port=0,
    ttl=3600,
    spa=False,
) -> Result:
    if type(port) is not int or not 0 <= port <= 65535 or not 1 <= ttl <= 86400:
        raise PirafuError(
            "PREVIEW_OPTIONS_INVALID", "portは0〜65535、ttlは1〜86400秒で指定してください。"
        )
    config = config.absolute()
    manifest = validate(load_manifest(config))
    result = Result(
        command="preview", dryRun=dry_run, data={"mode": "vite" if run_command else "static"}
    )
    hosting = manifest["hosting"]
    if hosting["provider"] != "cloudflare-pages":
        result.outcome, result.exit_code = "needs_action", 5
        result.nextActions.append(
            Notice(
                code="PREVIEW_OUTPUT_REQUIRED",
                message="プレビューするローカル出力のhosting設定が必要です。",
            )
        )
        return result
    build_root = inside(
        config.parent, hosting["rootDirectory"], "hosting.rootDirectory", allow_dot=True
    )
    root = inside(build_root, hosting["outputDirectory"], "hosting.outputDirectory", allow_dot=True)
    if run_command:
        vite_argv(build_root, manifest, port or 5173)
        if not yes and not dry_run:
            result.outcome, result.exit_code = "needs_action", 5
            result.nextActions.append(
                Notice(
                    code="CONFIRM_PREVIEW_COMMAND",
                    message="local.previewの実行には--run-command --yesが必要です。",
                )
            )
            return result
    else:
        _check_output(root)
    result.changes = [
        {
            "action": "start_local_preview",
            "mode": result.data["mode"],
            "host": "127.0.0.1",
            "port": port,
            "ttl": ttl,
        }
    ]
    if dry_run:
        result.outcome = "planned"
        return result
    session = uuid.uuid4().hex
    app_port = available_port(port) if run_command else port
    argv = vite_argv(build_root, manifest, app_port) if run_command else None
    state = {"session": session, "token": secrets.token_urlsafe(32)}
    directory = state_directory()
    no_links(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt" and (directory.stat().st_mode & 0o077):
        raise PirafuError("PREVIEW_STATE_PERMISSIONS", "セッション保存先の権限が広すぎます。", 3)
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = subprocess.Popen(
        [sys.executable, "-I", "-m", "pirafu_cli.preview_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=child_environment(),
        **options,
    )
    payload = {
        **state,
        "root": str(root),
        "port": app_port,
        "ttl": ttl,
        "spa": spa,
        "argv": argv,
        "cwd": str(build_root),
    }
    state_file = state_path(session)
    try:
        process.stdin.write(json.dumps(payload).encode())
        process.stdin.close()
        startup = []
        thread = threading.Thread(
            target=lambda: startup.append(process.stdout.readline(2049)), daemon=True
        )
        thread.start()
        thread.join(timeout=STARTUP_TIMEOUT)
        if thread.is_alive() or not startup or len(startup[0]) > 2048:
            raise PirafuError(
                "PREVIEW_START_TIMEOUT", "プレビューの起動を期限内に確認できません。", 6
            )
        try:
            ready = json.loads(startup[0])
        except ValueError as exc:
            raise PirafuError(
                "PREVIEW_START_FAILED",
                "プレビューを起動できません。ポート・出力先を確認してください。",
                3,
            ) from exc
        # Windowsのvenv redirectorと実workerはPIDが異なる。所有する非公開pipeの
        # セッション一致で照合し、PIDは表示専用（停止のkill対象にはしない）。
        if (
            not isinstance(ready, dict)
            or ready.get("session") != session
            or type(ready.get("pid")) is not int
        ):
            raise PirafuError(
                "PREVIEW_START_FAILED", "起動したプロセスの同一性を確認できません。", 3
            )
        state.update(ready)
        if not _control(state):
            raise PirafuError("PREVIEW_START_FAILED", "プレビューの制御先を確認できません。", 3)
        result.data.update(
            {
                "session": session,
                "url": ready["url"],
                "pid": ready["pid"],
                "stopCommand": ["pirafu", "preview", "--stop", session],
            }
        )
        if background:
            return result
        # フォアグラウンドでもworkerを所有し、Ctrl+CとTTLで必ず停止・後始末する。
        print(f"Preview: {ready['url']} (Ctrl+Cで停止)", file=sys.stderr, flush=True)
        interrupted = False
        try:
            code = process.wait()
            if code != 0:
                raise PirafuError("PREVIEW_WORKER_EXIT", "プレビューが異常終了しました。", 3)
        except KeyboardInterrupt:
            interrupted = True
        stop_preview(session)
        result.data["stopped"] = True
        if interrupted:
            result.outcome, result.exit_code = "needs_action", 130
        return result
    except BaseException:
        stop_owned(process)
        state_file.unlink(missing_ok=True)
        raise
    finally:
        process.stdout.close()
