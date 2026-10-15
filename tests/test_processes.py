import sys

import pytest

from pirafu_cli import processes
from pirafu_cli.results import PirafuError


@pytest.fixture(autouse=True)
def python_tool(monkeypatch):
    monkeypatch.setattr(processes.shutil, "which", lambda name: sys.executable)


def test_tool_capture_has_a_hard_size_limit():
    with pytest.raises(PirafuError) as error:
        processes.run_tool("fixture", ["-c", "print('a' * 100_000)"])
    assert error.value.notice.code == "TOOL_OUTPUT_LIMIT"


def test_tool_timeout_is_reported_and_child_is_terminated(monkeypatch):
    monkeypatch.setattr(processes, "TOOL_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(PirafuError) as error:
        processes.run_tool("fixture", ["-c", "import time; time.sleep(60)"])
    assert error.value.exit_code == 6
    assert error.value.notice.code == "TOOL_TIMEOUT"


def test_diagnostic_disables_prompts_and_debug_logging(monkeypatch):
    monkeypatch.setenv("GH_DEBUG", "api")
    code, content = processes.run_tool(
        "fixture",
        [
            "-c",
            (
                "import os; assert 'GH_DEBUG' not in os.environ; "
                "assert os.environ['GH_PROMPT_DISABLED']=='1'; "
                "assert os.environ['GIT_TERMINAL_PROMPT']=='0'; print('ok')"
            ),
        ],
    )
    assert code == 0 and content.strip() == b"ok"
