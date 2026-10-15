import copy
import io
import json

import pytest
import yaml
from PIL import Image, PngImagePlugin

from pirafu_cli import cli, publishing, registration, registration_io
from pirafu_cli.registration import (
    apply_registration,
    digest,
    plan_registration,
    prepare,
    serialized,
)
from pirafu_cli.registration_io import GitHub, Snapshot, blob_sha, external
from pirafu_cli.results import PirafuError, Result


@pytest.fixture
def showcase(pages_config):
    path, data = pages_config
    data["hosting"] = {"provider": "none", "mode": "none"}
    data["launch"] = {"type": "showcase"}
    data.pop("links")
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path, data


def save(path, data):
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def item(path, content):
    return {
        "path": path,
        "sha": blob_sha(content),
        "mode": "100644",
        "type": "blob",
        "size": len(content),
    }


class MemoryGitHub:
    repository = "example/portal"
    base = "main"

    def __init__(self, records=None):
        self.contents = {
            "catalog/categories.json": serialized(
                [{"id": name, "label": name} for name in ("apps", "games", "tools", "other")]
            )
        }
        self.contents.update(
            {
                f"catalog/projects/{key}.json": serialized(value)
                for key, value in (records or {}).items()
            }
        )
        self.base_tree = {path: item(path, content) for path, content in self.contents.items()}
        self.tree = copy.deepcopy(self.base_tree)
        self.base_sha = "a" * 40
        self.head_sha = None
        self.pull = None
        self.public = True
        self.at_base = False

    def public_repository(self, url):
        if not self.public:
            raise PirafuError("PRIVATE_REPOSITORY_LINK", "非公開です。", 5)

    def blob(self, tree, path, **kwargs):
        return self.contents[path]

    def ref(self, name):
        return self.base_sha if name == "main" else self.head_sha

    def snapshot(self, project_id):
        return Snapshot(
            self.base_sha,
            self.head_sha,
            copy.deepcopy(self.base_tree),
            copy.deepcopy(self.tree),
            {
                path: content
                for path, content in self.contents.items()
                if path.startswith("catalog/projects/")
            },
            self.contents["catalog/categories.json"],
            {"id": 1, "login": "example"},
            copy.deepcopy(self.pull),
            self.at_base,
        )


def plan(config, github, **kwargs):
    return plan_registration(
        config,
        portal_repo=github.repository,
        base=github.base,
        github_factory=lambda *_: github,
        **kwargs,
    )


def test_dry_run_is_read_only_and_plan_lists_exact_bytes(showcase, monkeypatch):
    config, data = showcase
    github = MemoryGitHub()
    before = {path: path.read_bytes() for path in config.parent.rglob("*") if path.is_file()}
    monkeypatch.setattr(
        registration.tempfile, "TemporaryDirectory", lambda **_: pytest.fail("temporary write")
    )
    monkeypatch.setattr(registration, "external", lambda *_, **__: pytest.fail("mutation"))
    result = plan(config, github, dry_run=True)
    assert result.result.outcome == "planned" and result.result.exit_code == 0
    assert result.result.data["readyToApply"]
    assert result.result.data["previewMayBePublic"]
    record = result.result.data["record"]
    assert "hosting" not in record and "local" not in record
    assert record["publication"] == {"visibility": "hidden", "launchEnabled": False}
    assert result.result.changes[0]["path"] == "catalog/projects/sample-work.json"
    assert apply_registration(result) is result.result
    assert before == {
        path: path.read_bytes() for path in config.parent.rglob("*") if path.is_file()
    }


def test_reencode_strips_metadata_preserves_transparency_and_is_stable(showcase):
    config, data = showcase
    meta = PngImagePlugin.PngInfo()
    meta.add_text("private", "internal fixture comment")
    Image.new("RGBA", (8, 6), (255, 0, 0, 100)).save(config.parent / "image.png", pnginfo=meta)
    data["catalog"] = {
        "thumbnail": {"path": "image.png", "alt": "画像"},
        "screenshots": [{"path": "image.png", "alt": "同じ画像"}],
    }
    save(config, data)
    record, assets, _ = prepare(config)
    assert len(assets) == 1
    content = next(iter(assets.values()))
    assert b"internal fixture" not in content
    with Image.open(io.BytesIO(content)) as image:
        assert image.mode == "RGBA" and image.getpixel((0, 0))[3] == 100
        assert image.info == {} and not image.getexif()
    assert record["catalog"]["thumbnail"]["path"].endswith(digest(content) + ".png")
    assert prepare(config)[1] == assets


def test_jpeg_exif_orientation_is_applied_then_removed(showcase):
    config, data = showcase
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "private fixture author"
    Image.new("RGB", (12, 8)).save(config.parent / "photo.jpg", exif=exif)
    data["catalog"] = {"thumbnail": {"path": "photo.jpg", "alt": "画像"}}
    save(config, data)
    _, assets, _ = prepare(config)
    with Image.open(io.BytesIO(next(iter(assets.values())))) as image:
        assert image.size == (8, 12) and not image.getexif()


def test_update_requires_flag_preserves_portal_controls_and_noop(showcase):
    config, data = showcase
    old, _, _ = prepare(config)
    old["publication"] = {"visibility": "unlisted", "launchEnabled": False}
    github = MemoryGitHub({"sample-work": old})
    assert plan(config, github).result.outcome == "no_change"
    data["project"]["title"] = "更新作品"
    data["project"]["visibility"] = "public"
    save(config, data)
    with pytest.raises(PirafuError, match="--update"):
        plan(config, github)
    updated = plan(config, github, update=True)
    assert updated.result.data["record"]["publication"] == old["publication"]
    assert updated.result.data["operation"] == "update"


def test_repository_id_collision_and_private_repository_refused(showcase):
    config, data = showcase
    old, _, _ = prepare(config)
    github = MemoryGitHub({"sample-work": old})
    data["links"] = {"repository": "https://github.com/example/work"}
    save(config, data)
    with pytest.raises(PirafuError) as error:
        plan(config, github, update=True)
    assert error.value.notice.code == "ID_CONFLICT"
    github.public = False
    with pytest.raises(PirafuError) as error:
        plan(config, github, update=True)
    assert error.value.notice.code == "PRIVATE_REPOSITORY_LINK"


@pytest.mark.parametrize("change", ["input", "base", "head", "pr", "identity"])
def test_stale_plan_stops_before_any_git_write(showcase, monkeypatch, change):
    config, data = showcase
    github = MemoryGitHub()
    prepared = plan(config, github)
    if change == "input":
        data["project"]["title"] = "changed"
        save(config, data)
    elif change == "base":
        github.base_sha = "b" * 40
    elif change == "head":
        github.head_sha = "b" * 40
    elif change == "pr":
        github.pull = {"number": 1, "url": "https://github.com/example/portal/pull/1"}
    else:
        original = github.snapshot

        def changed(project_id):
            snap = original(project_id)
            snap.user = {"id": 2, "login": "other"}
            return snap

        github.snapshot = changed
    monkeypatch.setattr(registration, "external", lambda *_, **__: pytest.fail("must not mutate"))
    with pytest.raises(PirafuError) as error:
        apply_registration(prepared)
    assert error.value.notice.code == "REGISTER_PLAN_CHANGED"


def test_expected_plan_and_offline_cannot_mutate(showcase, monkeypatch):
    config, _ = showcase
    github = MemoryGitHub()
    prepared = plan(config, github)
    with pytest.raises(PirafuError) as error:
        apply_registration(prepared, expect_plan="0" * 64)
    assert error.value.notice.code == "REGISTER_PLAN_CHANGED"
    monkeypatch.setattr(
        registration_io, "external", lambda *_, **__: pytest.fail("offline network")
    )
    offline = plan(config, github, offline=True)
    assert offline.result.exit_code == 5 and not offline.result.data["readyToApply"]
    with pytest.raises(PirafuError):
        apply_registration(offline)


def test_cli_plan_apply_gate_and_unknown_target(showcase, monkeypatch, capsys):
    config, _ = showcase
    github = MemoryGitHub()
    monkeypatch.setattr(
        cli,
        "plan_registration",
        lambda config, **kwargs: plan(config, github, dry_run=kwargs["dry_run"]),
    )
    monkeypatch.setattr(cli, "apply_registration", lambda *_, **__: pytest.fail("no approval"))
    for flags, expected in (([], 0), (["--apply"], 5), (["--apply", "--yes", "--dry-run"], 0)):
        code = cli.main(
            [
                "register",
                "--config",
                str(config),
                "--portal-repo",
                "example/portal",
                "--base",
                "main",
                "--json",
                *flags,
            ]
        )
        result = json.loads(capsys.readouterr().out)
        assert code == expected and result["command"] == "register"
    monkeypatch.undo()
    code = cli.main(["register", "--config", str(config), "--offline", "--json"])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["errors"][0]["code"] == "PORTAL_TARGET_REQUIRED"


def test_publish_offline_and_showcase_never_deploy(showcase, pages_config, monkeypatch):
    monkeypatch.setattr(registration_io, "external", lambda *_, **__: pytest.fail("deploy"))
    assert publishing.publish(showcase[0], offline=True).outcome == "no_change"
    path, data = showcase
    data["hosting"] = {
        "provider": "cloudflare-pages",
        "mode": "git",
        "projectName": "sample-work",
        "productionBranch": "main",
        "rootDirectory": ".",
        "outputDirectory": "dist",
        "buildCommand": "npm run build",
    }
    data["launch"] = {"type": "external", "url": "https://sample-work.pages.dev/"}
    save(path, data)
    result = publishing.publish(path, offline=True, dry_run=True)
    assert result.exit_code == 5 and not result.data["performedDeployment"]
    import pirafu_cli.status as status_module

    def missing(*args, **kwargs):
        raise PirafuError("PAGES_NOT_FOUND", "missing", 5)

    monkeypatch.setattr(status_module, "status", missing)
    result = publishing.publish(path)
    assert result.exit_code == 5
    assert result.nextActions[0].code == "CONNECT_GIT_MANUALLY"
    monkeypatch.setattr(
        status_module,
        "status",
        lambda *_, **__: Result(
            command="status", data={"productionUrl": "https://sample-work.pages.dev/"}
        ),
    )
    result = publishing.publish(path)
    assert result.exit_code == 0 and not result.data["performedDeployment"]


def test_external_registration_needs_verified_hosting(pages_config, monkeypatch):
    path, _ = pages_config
    import pirafu_cli.status as status_module

    monkeypatch.setattr(
        status_module,
        "status",
        lambda *_, **__: Result(command="status", outcome="needs_action", exit_code=5),
    )
    result = plan(path, MemoryGitHub())
    assert result.result.exit_code == 5 and result.github is None


def test_unmanaged_requires_explicit_operator_confirmation(pages_config):
    path, data = pages_config
    data["hosting"] = {"provider": "external", "mode": "unmanaged"}
    data.pop("links")
    save(path, data)
    assert plan(path, MemoryGitHub()).result.exit_code == 5
    prepared = plan(path, MemoryGitHub(), confirm_external=True)
    assert prepared.result.exit_code == 0 and prepared.result.outcome == "planned"
    assert prepared.confirm_external
    assert any(item.code == "EXTERNAL_CONFIRMED_BY_OPERATOR" for item in prepared.result.warnings)


def test_pages_cannot_bypass_auth_with_operator_confirmation(pages_config, monkeypatch):
    path, data = pages_config
    import pirafu_cli.status as status_module

    def denied(*_, **__):
        raise PirafuError("CLOUDFLARE_AUTH_REQUIRED", "認証が必要です。", 4)

    monkeypatch.setattr(status_module, "status", denied)
    with pytest.raises(PirafuError) as error:
        plan(path, MemoryGitHub(), confirm_external=True)
    assert error.value.exit_code == 4


def test_catalog_duplicate_filename_and_category_rejected(showcase):
    config, _ = showcase
    old, _, _ = prepare(config)
    github = MemoryGitHub({"wrong-filename": old})
    with pytest.raises(PirafuError) as error:
        plan(config, github)
    assert error.value.notice.code == "ID_CONFLICT"


@pytest.mark.parametrize("value", ["--all", "https://github.com/a/b", "a/../x", "a/b/c"])
def test_invalid_portal_repo(value):
    with pytest.raises(PirafuError):
        registration_io.repository_name(value)


@pytest.mark.parametrize(
    "value", ["--all", "main..x", "x.lock", "a//b", "@{x}", "a/../b", "x:main", "/main"]
)
def test_invalid_base(value):
    with pytest.raises(PirafuError):
        registration_io.branch_name(value)


def test_remote_blob_integrity_and_private_url_guard(monkeypatch):
    github = GitHub("example/portal", "main")
    content = b"fixture"
    tree = {"file": item("file", content)}
    monkeypatch.setattr(github, "api", lambda _: {"encoding": "base64", "content": "YmFk"})
    with pytest.raises(PirafuError) as error:
        github.blob(tree, "file")
    assert error.value.notice.code == "GITHUB_BLOB_MISMATCH"
    with pytest.raises(PirafuError) as error:
        github.public_repository("https://example.com/repository")
    assert error.value.exit_code == 5


@pytest.mark.parametrize("status,expected", [(401, 4), (403, 4), (404, 5), (429, 6), (503, 6)])
def test_github_read_failure_codes_and_secret_output_suppression(monkeypatch, status, expected):
    monkeypatch.setattr(
        registration_io,
        "external",
        lambda *_, **__: (
            1,
            json.dumps({"status": str(status), "message": "secret fixture"}).encode(),
        ),
    )
    with pytest.raises(PirafuError) as error:
        GitHub("example/portal", "main").api("user")
    assert error.value.exit_code == expected
    assert "secret fixture" not in str(error.value)


class SnapshotAPI(GitHub):
    """Exercise the actual REST snapshot reader without network or mutations."""

    def __init__(self):
        super().__init__("example/portal", "main")
        from importlib.resources import files

        self.base_sha, self.head_sha = "a" * 40, "b" * 40
        self.contents = {"catalog/categories.json": b"[]"}
        for filename in ("common.schema.json", "catalog.schema.json"):
            self.contents[f"contracts/v1/{filename}"] = (
                files("pirafu_cli").joinpath(f"contracts/v1/{filename}").read_bytes()
            )
        self.base_tree = {path: item(path, content) for path, content in self.contents.items()}
        self.head_tree = copy.deepcopy(self.base_tree)
        self.head_tree["catalog/projects/sample-work.json"] = item(
            "catalog/projects/sample-work.json", b"{}"
        )
        self.contents["catalog/projects/sample-work.json"] = b"{}"
        self.pulls = []
        self.comparison = {
            "status": "ahead",
            "ahead_by": 1,
            "commits": [
                {
                    "author": {"id": 1},
                    "commit": {"message": "Register sample-work\n\nPirafu-Register: sample-work\n"},
                }
            ],
        }

    def ref(self, branch):
        return self.base_sha if branch == "main" else self.head_sha

    def api(self, endpoint):
        if endpoint == "user":
            return {"id": 1, "login": "example"}
        if "/pulls?" in endpoint:
            return self.pulls
        if "/compare/" in endpoint:
            return self.comparison
        pytest.fail(endpoint)

    def tree(self, sha):
        return self.base_tree if sha == self.base_sha else self.head_tree

    def blob(self, tree, path, **kwargs):
        return self.contents[path]


@pytest.mark.parametrize(
    "change,code",
    [
        ("foreign-file", "REGISTER_OTHER_CHANGES"),
        ("foreign-author", "REGISTER_OTHER_CHANGES"),
        ("missing-marker", "REGISTER_OTHER_CHANGES"),
        ("diverged", "REGISTER_BASE_CHANGED"),
        ("incomplete", "REGISTER_HISTORY_INCOMPLETE"),
        ("schema", "CONTRACT_REVISION_MISMATCH"),
    ],
)
def test_snapshot_rejects_foreign_or_stale_changes(change, code):
    github = SnapshotAPI()
    if change == "foreign-file":
        github.head_tree["src/app.tsx"] = item("src/app.tsx", b"changed")
    elif change == "foreign-author":
        github.comparison["commits"][0]["author"]["id"] = 2
    elif change == "missing-marker":
        github.comparison["commits"][0]["commit"]["message"] = "unrelated change"
    elif change == "diverged":
        github.comparison["status"] = "diverged"
    elif change == "incomplete":
        github.comparison["ahead_by"] = 2
    else:
        github.contents["contracts/v1/catalog.schema.json"] = b"{}"
    with pytest.raises(PirafuError) as error:
        github.snapshot("sample-work")
    assert error.value.notice.code == code


def test_merged_ancestor_branch_reused_without_force_or_recreation():
    github = SnapshotAPI()
    github.comparison = {"status": "behind"}
    github.pulls = [
        {
            "head": {"ref": "pirafu/register/sample-work"},
            "base": {"ref": "main"},
            "state": "closed",
            "merged_at": "timestamp",
        }
    ]
    result = github.snapshot("sample-work")
    assert result.at_base and result.tree == github.base_tree and result.head == "b" * 40


def test_squash_reconciliation_requires_exact_merged_head_and_base_ancestry():
    github = SnapshotAPI()
    original = github.api
    github.comparison = {"status": "diverged"}
    merged_sha = "c" * 40
    github.pulls = [
        {
            "head": {"ref": "pirafu/register/sample-work", "sha": github.head_sha},
            "base": {"ref": "main"},
            "state": "closed",
            "merged_at": "timestamp",
            "merge_commit_sha": merged_sha,
        }
    ]

    def api(endpoint):
        if f"compare/{merged_sha}..." in endpoint:
            return {"status": "ahead"}
        return original(endpoint)

    github.api = api
    result = github.snapshot("sample-work")
    assert result.at_base and result.reconcile and result.tree == github.base_tree
    github.pulls[0]["head"]["sha"] = "d" * 40
    with pytest.raises(PirafuError) as error:
        github.snapshot("sample-work")
    assert error.value.notice.code == "REGISTER_BASE_CHANGED"


@pytest.mark.parametrize("issue", ["owner", "head", "repository", "closed"])
def test_existing_pr_identity_must_match(issue):
    github = SnapshotAPI()
    row = {
        "head": {
            "ref": "pirafu/register/sample-work",
            "sha": github.head_sha,
            "repo": {"full_name": "example/portal"},
        },
        "base": {"ref": "main", "repo": {"full_name": "example/portal"}},
        "user": {"id": 1},
        "state": "open",
        "number": 1,
    }
    github.pulls = [row]
    if issue == "owner":
        row["user"]["id"] = 2
    elif issue == "head":
        row["head"]["sha"] = "d" * 40
    elif issue == "repository":
        row["head"]["repo"]["full_name"] = "other/portal"
    else:
        row["state"] = "closed"
    with pytest.raises(PirafuError) as error:
        github.snapshot("sample-work")
    assert error.value.exit_code == 7


def test_external_process_output_limit_timeout_and_secrets(monkeypatch):
    import sys

    monkeypatch.setattr(registration_io.shutil, "which", lambda _: sys.executable)
    with pytest.raises(PirafuError) as error:
        external("fixture", ["-c", "print('x' * 100000)"], limit=100)
    assert error.value.notice.code == "REMOTE_OUTPUT_LIMIT"
    with pytest.raises(PirafuError) as error:
        external("fixture", ["-c", "import time; time.sleep(60)"], timeout=0.1)
    assert error.value.exit_code == 6
    monkeypatch.setenv("GH_DEBUG", "api")
    monkeypatch.setenv("GIT_TRACE", "1")
    code, content = external(
        "fixture",
        [
            "-c",
            "import os; assert 'GH_DEBUG' not in os.environ; "
            "assert 'GIT_TRACE' not in os.environ; print('ok')",
        ],
    )
    assert code == 0 and content.strip() == b"ok"
