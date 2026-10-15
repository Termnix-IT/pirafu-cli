import json
import os
import socket
import subprocess
from pathlib import Path

import pytest
import yaml
from PIL import Image

from pirafu_cli.cli import main
from pirafu_cli.files import load_manifest
from pirafu_cli.results import PirafuError


def invoke(capsys, *args):
    code = main([*map(str, args), "--json", "--non-interactive"])
    captured = capsys.readouterr()
    assert not captured.err
    result = json.loads(captured.out)
    assert set(result) == {
        "apiVersion",
        "command",
        "outcome",
        "dryRun",
        "data",
        "changes",
        "warnings",
        "errors",
        "nextActions",
    }
    return code, result


def init_options(path, template="showcase"):
    return [
        "init",
        "--config",
        path,
        "--template",
        template,
        "--id",
        "sample-work",
        "--title",
        "検証作品",
        "--description",
        "公開可能な紹介です。",
    ]


def create_config(tmp_path):
    root = tmp_path / "日本語 空白の作品"
    root.mkdir()
    data = {
        "schemaVersion": 1,
        "project": {
            "id": "sample-work",
            "title": "作品",
            "description": "説明",
            "category": "other",
            "status": "planned",
            "visibility": "hidden",
        },
        "hosting": {"provider": "none", "mode": "none"},
        "launch": {"type": "showcase"},
        "catalog": {},
    }
    path = root / "pirafu.yaml"
    save(path, data)
    return path, data


def save(path, data):
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_dry_run_creates_no_file_and_apply_needs_explicit_yes(tmp_path, capsys):
    path = tmp_path / "pirafu.yaml"
    code, result = invoke(capsys, *init_options(path), "--dry-run", "--yes")
    assert code == 0 and result["outcome"] == "planned"
    assert not path.exists()
    assert result["changes"][0]["content"]["project"]["visibility"] == "hidden"
    assert list(tmp_path.iterdir()) == []
    code, result = invoke(capsys, *init_options(path))
    assert code == 5 and result["nextActions"][0]["code"] == "CONFIRM_CREATE"
    assert not path.exists()
    code, result = invoke(capsys, *init_options(path), "--yes")
    assert code == 0 and path.is_file()
    assert result["data"]["created"]
    before = path.read_bytes()
    code, result = invoke(capsys, *init_options(path), "--yes", "--dry-run")
    assert code == 7 and result["errors"][0]["code"] == "CONFIG_EXISTS"
    assert path.read_bytes() == before


def test_detection_is_a_proposal_and_does_not_run_package_scripts(tmp_path, capsys):
    path = tmp_path / "pirafu.yaml"
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "devDependencies": {"vite": "*"},
                "scripts": {"build": "untrusted-command-must-never-run"},
            }
        )
    )
    code, result = invoke(capsys, "init", "--config", path)
    assert code == 5 and result["data"]["candidates"] == ["vite"]
    assert not path.exists()
    code, result = invoke(capsys, *init_options(path, "vite"), "--yes")
    assert code == 5 and "production-branch" in result["data"]["requiredOptions"]
    code, result = invoke(
        capsys,
        *init_options(path, "vite"),
        "--project-name",
        "pirafu-sample-work",
        "--production-branch",
        "production",
        "--yes",
    )
    assert code == 0
    assert load_manifest(path)["hosting"]["productionBranch"] == "production"
    assert load_manifest(path)["hosting"]["outputDirectory"] == "dist"


def test_html_template_does_not_require_node(tmp_path, capsys, monkeypatch):
    (tmp_path / "public").mkdir()
    (tmp_path / "public/index.html").write_text("<!doctype html><title>Sample</title>")
    monkeypatch.setenv("PATH", "")
    path = tmp_path / "pirafu.yaml"
    code, result = invoke(
        capsys,
        *init_options(path, "html"),
        "--project-name",
        "pirafu-html",
        "--production-branch",
        "release",
        "--yes",
    )
    assert code == 0
    assert load_manifest(path)["hosting"]["buildCommand"] == ""
    code, result = invoke(capsys, "check", "--config", path, "--target", "publish")
    assert code == 5 and result["data"]["outputEntries"] == 1
    assert result["data"]["readyToPublish"] is False


def test_godot_and_unknown_stacks_require_explicit_template(tmp_path, capsys):
    (tmp_path / "project.godot").write_text("; test project")
    code, result = invoke(capsys, "init", "--config", tmp_path / "pirafu.yaml")
    assert code == 5 and result["data"]["candidates"] == ["godot-manual"]
    code, result = invoke(capsys, *init_options(tmp_path / "pirafu.yaml", "manual"), "--yes")
    assert code == 0


def test_showcase_local_check_reports_unchecked_catalog_and_is_read_only(tmp_path, capsys):
    path, data = create_config(tmp_path)
    data["local"] = {"build": ["untrusted-command", "&& must-not-run"]}
    save(path, data)
    before = path.read_bytes()
    code, result = invoke(capsys, "check", "--config", path, "--dry-run", "--offline")
    assert code == 0 and result["data"]["catalogIdCheck"] == "not_checked"
    assert result["data"]["readyToPublish"] is False
    assert path.read_bytes() == before
    assert len(list(path.parent.iterdir())) == 1


@pytest.mark.parametrize(
    "text,error",
    [
        ("schemaVersion: 1\nschemaVersion: 1", "YAML_KEY_INVALID"),
        ("a: &a value\nb: *a", "YAML_ALIAS_FORBIDDEN"),
        ("a: !!python/object/apply:os.system []", "YAML_TAG_FORBIDDEN"),
        ("true: value", "YAML_KEY_INVALID"),
        ("a: .nan", "YAML_TYPE_INVALID"),
        ("a: 2026-10-15", "YAML_TYPE_INVALID"),
        ("a: 1\n---\nb: 2", "YAML_INVALID"),
        ("a: [", "YAML_INVALID"),
        ("[" * 65 + "0" + "]" * 65, "YAML_TOO_DEEP"),
    ],
)
def test_strict_yaml_and_no_input_leak(tmp_path, capsys, text, error):
    path = tmp_path / "pirafu.yaml"
    path.write_text(text + "\n# secret-marker-not-for-output", encoding="utf-8")
    code, result = invoke(capsys, "check", "--config", path)
    assert code == 2 and result["errors"][0]["code"] == error
    assert "secret-marker-not-for-output" not in json.dumps(result)


def test_oversize_and_unsupported_version_are_errors(tmp_path, capsys):
    path, data = create_config(tmp_path)
    path.write_bytes(b"#" * (256 * 1024 + 1))
    code, result = invoke(capsys, "check", "--config", path)
    assert code == 3 and result["errors"][0]["code"] == "INPUT_TOO_LARGE"
    data["schemaVersion"] = 2
    save(path, data)
    code, result = invoke(capsys, "check", "--config", path)
    assert code == 2 and result["errors"][0]["code"] == "SCHEMA_INVALID"


@pytest.mark.parametrize("relative", ["../outside.png", "C:/image.png", "a\\b.png", "con.png"])
def test_path_escape_is_rejected(tmp_path, capsys, relative):
    path, data = create_config(tmp_path)
    data["catalog"]["thumbnail"] = {"path": relative, "alt": "検証"}
    save(path, data)
    code, result = invoke(capsys, "check", "--config", path)
    assert code == 3 and result["errors"][0]["code"] == "UNSAFE_PATH"


def test_image_is_actually_decoded_and_extension_checked(tmp_path, capsys):
    path, data = create_config(tmp_path)
    image = path.parent / "thumb.png"
    data["catalog"]["thumbnail"] = {"path": "thumb.png", "alt": "検証"}
    save(path, data)
    Image.new("RGB", (32, 16)).save(image)
    code, result = invoke(capsys, "check", "--config", path)
    assert code == 0 and result["data"]["imageCount"] == 1
    image.write_bytes(b"not-a-png")
    code, result = invoke(capsys, "check", "--config", path)
    assert code == 3 and result["errors"][0]["code"] == "IMAGE_DECODE_FAILED"
    Image.new("RGB", (32, 16)).save(image, format="JPEG")
    code, result = invoke(capsys, "check", "--config", path)
    assert code == 3 and result["errors"][0]["code"] == "IMAGE_INVALID"
    Image.new("RGB", (4097, 1)).save(image, format="PNG")
    code, result = invoke(capsys, "check", "--config", path)
    assert code == 3


def test_animated_image_is_not_accepted(tmp_path, capsys):
    path, data = create_config(tmp_path)
    data["catalog"]["thumbnail"] = {"path": "animated.png", "alt": "検証"}
    save(path, data)
    Image.new("RGB", (8, 8), "red").save(
        path.parent / "animated.png",
        save_all=True,
        append_images=[Image.new("RGB", (8, 8), "blue")],
        duration=100,
        loop=0,
    )
    code, result = invoke(capsys, "check", "--config", path)
    assert code == 3


def test_symlink_is_rejected(tmp_path, capsys):
    path, data = create_config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "thumb.png"
    Image.new("RGB", (8, 8)).save(target)
    link = path.parent / "linked"
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
    data["catalog"]["thumbnail"] = {"path": "linked/thumb.png", "alt": "リンク"}
    save(path, data)
    code, result = invoke(capsys, "check", "--config", path)
    assert code == 3 and result["errors"][0]["code"] == "LINK_FORBIDDEN"


def test_catalog_collision_and_snapshot_scope(tmp_path, capsys):
    path, data = create_config(tmp_path)
    catalog = tmp_path / "catalog"
    (catalog / "projects").mkdir(parents=True)
    record = {key: value for key, value in data.items() if key != "hosting"}
    record["publication"] = {"visibility": "hidden", "launchEnabled": False}
    target = catalog / "projects/sample-work.json"
    target.write_text(json.dumps(record), encoding="utf-8")
    code, result = invoke(capsys, "check", "--config", path, "--catalog", catalog)
    assert code == 7 and result["errors"][0]["code"] == "ID_CONFLICT"
    record["links"] = data["links"] = {"repository": "https://github.com/example/sample-work"}
    target.write_text(json.dumps(record), encoding="utf-8")
    save(path, data)
    code, result = invoke(capsys, "check", "--config", path, "--catalog", catalog)
    assert code == 0 and result["data"]["catalogIdCheck"] == "existing_same_repository"
    record["project"]["id"] = "another-work"
    target.unlink()
    (target.parent / "another-work.json").write_text(json.dumps(record), encoding="utf-8")
    code, result = invoke(capsys, "check", "--config", path, "--catalog", catalog)
    assert code == 0 and result["data"]["catalogIdCheck"] == "unique_in_snapshot"


def test_unsupported_options_have_one_sanitized_json_result(capsys):
    code, result = invoke(capsys, "check", "--unexpected=secret-marker")
    assert code == 2 and result["errors"][0]["code"] == "ARGUMENT_INVALID"
    assert "secret-marker" not in json.dumps(result)


def test_doctor_offline_and_dry_run_do_not_query_auth_or_create_files(
    tmp_path,
    capsys,
    monkeypatch,
):
    from pirafu_cli import services

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(services.shutil, "which", lambda name: f"/tools/{name}")
    calls = []

    def run(name, args):
        calls.append((name, args))
        return 0, f"{name} version 2.0.0\nsecret-marker".encode()

    monkeypatch.setattr(services, "run_tool", run)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network"))
    code, result = invoke(capsys, "doctor", "--offline", "--dry-run")
    assert code == 0 and result["data"]["githubAuth"] == "not_checked"
    assert result["data"]["cloudflareAuth"] == "not_checked"
    assert all(args == ["--version"] for _, args in calls)
    assert not list(tmp_path.iterdir())
    assert "secret-marker" not in json.dumps(result)


@pytest.mark.parametrize(
    "storage,expected",
    [
        ("keyring", "os_store"),
        ("GH_TOKEN", "environment"),
        ("file", "unverified_or_plaintext"),
    ],
)
def test_doctor_uses_auth_state_and_never_reports_raw_sources(
    capsys, monkeypatch, storage, expected
):
    from pirafu_cli import services

    monkeypatch.setattr(services.shutil, "which", lambda name: f"/tools/{name}")

    def run(name, args):
        if args == ["--version"]:
            return 0, b"v1.2.3"
        return 0, json.dumps({"state": "success", "tokenSource": storage}).encode()

    monkeypatch.setattr(services, "run_tool", run)
    code, result = invoke(capsys, "doctor")
    assert code == 0 and result["data"]["credentialStorage"] == expected


def test_doctor_missing_tools_is_not_success(capsys, monkeypatch):
    monkeypatch.setattr("pirafu_cli.services.shutil.which", lambda name: None)
    code, result = invoke(capsys, "doctor", "--offline")
    assert code == 3 and result["outcome"] == "error"


def test_unexpected_exception_does_not_disclose_traceback(capsys, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("secret-marker")

    monkeypatch.setattr("pirafu_cli.cli.doctor", fail)
    code, result = invoke(capsys, "doctor")
    assert code == 1 and "secret-marker" not in json.dumps(result)


def test_init_exclusive_create_handles_race(tmp_path, monkeypatch):
    from pirafu_cli.services import initialize

    original = Path.open

    def race(path, mode="r", *args, **kwargs):
        if mode == "x":
            path.write_text("other-user-change", encoding="utf-8")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", race)
    path = tmp_path / "pirafu.yaml"
    with pytest.raises(PirafuError) as error:
        initialize(
            path,
            template="showcase",
            project_id="sample-work",
            title="作品",
            description="説明",
            category="other",
            project_name=None,
            branch=None,
            output=None,
            repository=None,
            dry_run=False,
            yes=True,
        )
    assert error.value.exit_code == 7
    assert path.read_text(encoding="utf-8") == "other-user-change"


def test_malformed_package_fields_are_a_configuration_error(tmp_path, capsys):
    (tmp_path / "package.json").write_text('{"dependencies": "invalid"}')
    code, result = invoke(capsys, "init", "--config", tmp_path / "pirafu.yaml")
    assert code == 2 and result["errors"][0]["code"] == "PACKAGE_INVALID"


def test_missing_output_and_private_artifacts_are_not_publishable(tmp_path, capsys):
    path, data = create_config(tmp_path)
    data["launch"] = {"type": "external"}
    data["hosting"] = {
        "provider": "cloudflare-pages",
        "mode": "git",
        "projectName": "pirafu-sample-work",
        "productionBranch": "main",
        "rootDirectory": ".",
        "outputDirectory": "dist",
        "buildCommand": "",
    }
    save(path, data)
    code, result = invoke(capsys, "check", "--config", path, "--target", "publish")
    assert code == 3 and result["errors"][0]["code"] == "OUTPUT_MISSING"
    output = path.parent / "dist"
    output.mkdir()
    (output / "index.html").write_text("<!doctype html><title>Example</title>")
    (output / ".env").write_text("secret-marker")
    code, result = invoke(capsys, "check", "--config", path, "--target", "publish")
    assert code == 3 and result["errors"][0]["code"] == "PRIVATE_OUTPUT"
    assert "secret-marker" not in json.dumps(result)


def test_version_and_help_entry_points(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"
    assert main(["--help"]) == 0
    assert "doctor" in capsys.readouterr().out
