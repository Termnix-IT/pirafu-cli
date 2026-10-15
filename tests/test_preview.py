import json
import os
import socket
import subprocess
import time

import httpx
import pytest
from test_cli import invoke

from pirafu_cli import preview as preview_module
from pirafu_cli.preview import child_environment, preview, state_path, stop_preview, vite_argv
from pirafu_cli.results import PirafuError


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    path = tmp_path / "sessions"
    monkeypatch.setenv("PIRAFU_PREVIEW_STATE_DIR", str(path))
    return path


def test_dry_run_opens_no_socket_process_or_state(pages_config, monkeypatch, isolated_state):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("process created"))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("socket opened"))
    result = preview(pages_config[0], dry_run=True)
    assert result.exit_code == 0 and result.outcome == "planned"
    assert not isolated_state.exists()


def test_static_json_start_stop_and_second_stop_are_safe(pages_config, capsys):
    code, result = invoke(capsys, "preview", "--config", pages_config[0], "--ttl", "10")
    assert code == 0, result
    assert result["data"]["url"].startswith("http://127.0.0.1:")
    session = result["data"]["session"]
    try:
        state = json.loads(state_path(session).read_text(encoding="utf-8"))
        assert state["token"] not in json.dumps(result)
        if os.name != "nt":
            assert state_path(session).stat().st_mode & 0o077 == 0
        with httpx.Client(trust_env=False) as client:
            response = client.get(result["data"]["url"])
            assert response.status_code == 200 and "Preview fixture" in response.text
            assert client.get(result["data"]["url"] + "_pirafu/session").status_code == 403
            assert (
                client.get(result["data"]["url"], headers={"Host": "malicious.example"}).status_code
                == 403
            )
        planned = stop_preview(session, dry_run=True)
        assert planned.outcome == "planned" and state_path(session).exists()
    finally:
        stopped = stop_preview(session)
    assert stopped.exit_code == 0 and stopped.data["stopped"]
    assert stop_preview(session).outcome == "no_change"


def test_static_requests_deny_listing_metadata_traversal_and_late_links(pages_config, tmp_path):
    root = pages_config[0].parent
    result = preview(pages_config[0], background=True, ttl=10, spa=True)
    session = result.data["session"]
    try:
        (root / "dist/empty").mkdir()
        (root / "dist/.env").write_text("must-not-leak")
        (root / "dist/pirafu.yaml").write_text("must-not-leak")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.txt").write_text("must-not-leak")
        link = root / "dist/linked"
        if os.name == "nt":
            environment = os.environ.copy()
            environment.update({"PIRAFU_TEST_LINK": str(link), "PIRAFU_TEST_TARGET": str(outside)})
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "New-Item -ItemType Junction -Path $env:PIRAFU_TEST_LINK "
                    "-Target $env:PIRAFU_TEST_TARGET | Out-Null",
                ],
                env=environment,
                check=True,
                timeout=20,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            link.symlink_to(outside, target_is_directory=True)
        with httpx.Client(trust_env=False) as client:
            for path in ("empty/", ".env", "pirafu.yaml", "%2e%2e/pirafu.yaml", "linked/leak.txt"):
                response = client.get(result.data["url"] + path)
                assert response.status_code == 404 and "must-not-leak" not in response.text
            assert client.get(result.data["url"] + "projects/sample-work").status_code == 200
    finally:
        stop_preview(session)


def test_background_ttl_closes_listener_and_removes_state(pages_config):
    result = preview(pages_config[0], background=True, ttl=1)
    session = result.data["session"]
    deadline = time.monotonic() + 5
    while state_path(session).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not state_path(session).exists()
    with httpx.Client(trust_env=False, timeout=0.5) as client:
        with pytest.raises(httpx.HTTPError):
            client.get(result.data["url"])


def test_occupied_port_fails_without_state(pages_config, isolated_state):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        with pytest.raises(PirafuError) as error:
            preview(pages_config[0], background=True, port=sock.getsockname()[1])
    assert error.value.exit_code == 3
    assert not list(isolated_state.glob("*.json"))


def test_missing_output_and_unsafe_session_are_rejected(pages_config):
    (pages_config[0].parent / "dist/index.html").unlink()
    with pytest.raises(PirafuError) as error:
        preview(pages_config[0], background=True)
    assert error.value.notice.code == "INDEX_MISSING"
    with pytest.raises(PirafuError):
        stop_preview("../another-file")


def test_command_preview_requires_explicit_yes(pages_config, monkeypatch):
    monkeypatch.setattr(preview_module, "vite_argv", lambda *args: ["unused"])
    result = preview(pages_config[0], background=True, run_command=True)
    assert result.exit_code == 5 and result.nextActions[0].code == "CONFIRM_PREVIEW_COMMAND"


@pytest.mark.parametrize(
    "script", ["arbitrary-command", "vite && echo unsafe", "vite --host 0.0.0.0"]
)
def test_arbitrary_or_public_commands_are_not_executed(pages_config, script):
    root = pages_config[0].parent
    pages_config[1]["local"] = {"preview": ["npm", "run", "dev"]}
    (root / "package.json").write_text(json.dumps({"scripts": {"dev": script}}))
    with pytest.raises(PirafuError) as error:
        vite_argv(root, pages_config[1], 5173)
    assert error.value.exit_code == 5


def test_child_preview_does_not_inherit_cloud_tokens(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "must-not-leak")
    monkeypatch.setenv("GH_TOKEN", "must-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    environment = child_environment()
    assert all(
        key not in environment
        for key in ("CLOUDFLARE_API_TOKEN", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY")
    )


def test_saved_pid_is_never_used_to_kill_an_unrelated_process(tmp_path):
    session = "a" * 32
    path = state_path(session)
    path.parent.mkdir(mode=0o700)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    path.write_text(
        json.dumps({"session": session, "pid": os.getpid(), "controlPort": port, "token": "a" * 43})
    )
    result = stop_preview(session)
    assert result.exit_code == 5 and result.data["stopped"] is False
