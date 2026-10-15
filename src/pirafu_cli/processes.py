import os
import shutil
import subprocess
import threading
from pathlib import Path

from pirafu_cli.results import PirafuError

TOOL_TIMEOUT_SECONDS = 15


def run_tool(name: str, arguments: list[str]) -> tuple[int, bytes]:
    executable = shutil.which(name)
    if not executable or Path(executable).suffix.lower() in {".cmd", ".bat"}:
        raise PirafuError("TOOL_MISSING", "必要な実行ファイルが見つかりません。", 3)
    environment = os.environ.copy()
    environment.pop("GH_DEBUG", None)
    environment.update(
        {
            "GH_PROMPT_DISABLED": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "NO_COLOR": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
        }
    )
    process = subprocess.Popen(
        [executable, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        shell=False,
    )
    chunks = []

    def read_output():
        content = process.stdout.read(4097)
        chunks.append(content)
        if len(content) > 4096:
            process.kill()

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=TOOL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(timeout=5)
        raise PirafuError("TOOL_TIMEOUT", "診断コマンドが期限内に終了しませんでした。", 6) from exc
    finally:
        reader.join(timeout=5)
        process.stdout.close()
    content = b"".join(chunks)
    if len(content) > 4096:
        raise PirafuError("TOOL_OUTPUT_LIMIT", "診断コマンドの出力上限を超えました。", 3)
    return code, content
