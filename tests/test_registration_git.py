"""Real Git plumbing/push integration. GitHub HTTP/PR calls stay local fixtures."""

import copy

import pytest
import yaml

from pirafu_cli import registration
from pirafu_cli.registration import apply_registration, plan_registration, serialized
from pirafu_cli.registration_io import Snapshot, external


@pytest.fixture
def git_registration(tmp_path, pages_config, monkeypatch):
    config, data = pages_config
    data["hosting"] = {"provider": "none", "mode": "none"}
    data["launch"] = {"type": "showcase"}
    data.pop("links")
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    bare = tmp_path / "日本語 bare remote.git"
    environment = {
        "GIT_AUTHOR_NAME": "example",
        "GIT_COMMITTER_NAME": "example",
        "GIT_AUTHOR_EMAIL": "1+example@users.noreply.github.com",
        "GIT_COMMITTER_EMAIL": "1+example@users.noreply.github.com",
    }

    def git(*args, content=None, check=True):
        code, output = external(
            "git", [f"--git-dir={bare}", *args], content=content, environment=environment
        )
        assert not check or code == 0, args
        return output.strip()

    git("init", "--bare", "--initial-branch=main")
    categories = serialized([{"id": "apps", "label": "apps"}])
    sha = git("hash-object", "-w", "--stdin", content=categories).decode()
    git("update-index", "--add", "--cacheinfo", f"100644,{sha},catalog/categories.json")
    tree = git("write-tree").decode()
    base_sha = git("commit-tree", tree, content=b"Initial fixture\n").decode()
    git("update-ref", "refs/heads/main", base_sha)

    class LocalGitHub:
        repository = "example/portal"
        base = "main"
        pull = None
        fail_pr = False
        pr_calls = 0
        calls = []
        at_base = False
        reconcile = False

        def public_repository(self, url):
            pytest.fail("unexpected public URL")

        def ref(self, branch):
            code, output = external(
                "git", [f"--git-dir={bare}", "rev-parse", "--verify", f"refs/heads/{branch}"]
            )
            return output.strip().decode() if code == 0 else None

        def tree(self, sha):
            content = git("ls-tree", "-r", "-z", sha)
            result = {}
            for raw in content.split(b"\0"):
                if not raw:
                    continue
                info, path = raw.split(b"\t", 1)
                mode, kind, blob = info.decode().split()
                name = path.decode()
                result[name] = {"mode": mode, "type": kind, "sha": blob, "path": name}
            return result

        def blob(self, tree, path, **kwargs):
            return git("cat-file", "blob", tree[path]["sha"]) + b"\n"

        def snapshot(self, project_id):
            base = self.ref("main")
            head = self.ref(f"pirafu/register/{project_id}")
            base_tree = self.tree(base)
            tree = self.tree(base if self.at_base else head or base)
            return Snapshot(
                base,
                head,
                base_tree,
                tree,
                {
                    path: self.blob(tree, path)
                    for path in tree
                    if path.startswith("catalog/projects/")
                },
                self.blob(tree, "catalog/categories.json"),
                {"id": 1, "login": "example"},
                copy.deepcopy(self.pull),
                self.at_base,
                self.reconcile,
            )

    github = LocalGitHub()

    def adapter(tool, arguments, **kwargs):
        github.calls.append((tool, list(arguments)))
        if tool == "gh":
            assert arguments[:2] == ["pr", "create"]
            assert "--head" in arguments and "--base" in arguments and "--body-file" in arguments
            assert "--dry-run" not in arguments
            body = arguments[arguments.index("--body-file") + 1]
            from pathlib import Path

            assert "\n\n" in Path(body).read_text(encoding="utf-8")
            github.pr_calls += 1
            if github.fail_pr:
                return 1, b"secret output must not escape"
            github.pull = {"number": 1, "url": "https://github.com/example/portal/pull/1"}
            return 0, b"https://github.com/example/portal/pull/1\n"
        if "remote" in arguments and "add" in arguments:
            arguments = [*arguments[:-1], str(bare)]
        assert "--force" not in arguments and not any(
            "force-with-lease" in value for value in arguments
        )
        return external(tool, arguments, **kwargs)

    monkeypatch.setattr(registration, "external", adapter)

    def plan(update=False):
        return plan_registration(
            config,
            portal_repo=github.repository,
            base="main",
            update=update,
            github_factory=lambda *_: github,
        )

    return config, data, github, plan, git


def test_real_git_first_registration_noop_and_pending_pr_update(git_registration):
    config, data, github, plan, git = git_registration
    before = config.read_bytes()
    result = apply_registration(plan())
    assert result.exit_code == 0, result.model_dump()
    assert github.pr_calls == 1 and result.data["pullRequest"]["number"] == 1
    first_head = github.ref("pirafu/register/sample-work")
    assert git("rev-parse", f"{first_head}^").decode() == github.ref("main")
    assert config.read_bytes() == before
    assert plan().result.outcome == "no_change"
    assert apply_registration(plan()).outcome == "no_change"
    assert github.pr_calls == 1
    data["project"]["title"] = "更新する作品"
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    result = apply_registration(plan())
    assert result.exit_code == 0, result.model_dump()
    second_head = github.ref("pirafu/register/sample-work")
    assert second_head != first_head
    assert git("rev-parse", f"{second_head}^").decode() == first_head
    assert github.pr_calls == 1
    assert github.ref("main") == result.data["baseSha"]
    assert all("checkout" not in args for _, args in github.calls)


def test_real_git_pr_failure_recovers_branch_without_duplicate_push(git_registration):
    config, data, github, plan, git = git_registration
    github.fail_pr = True
    failed = apply_registration(plan())
    assert failed.exit_code == 6
    assert failed.data["branchMayExist"]
    assert "secret output" not in failed.model_dump_json()
    first_head = github.ref("pirafu/register/sample-work")
    push_count = sum("push" in args for _, args in github.calls)
    github.fail_pr = False
    recovered = apply_registration(plan())
    assert recovered.exit_code == 0, recovered.model_dump()
    assert github.ref("pirafu/register/sample-work") == first_head
    assert sum("push" in args for _, args in github.calls) == push_count
    assert github.pr_calls == 2


def test_real_git_merged_branch_can_fast_forward_for_next_update(git_registration):
    config, data, github, plan, git = git_registration
    assert apply_registration(plan()).exit_code == 0
    old = github.ref("pirafu/register/sample-work")
    # The retained registration branch is an ancestor after a fast-forward merge.
    git("update-ref", "refs/heads/main", old)
    github.pull = None
    github.at_base = True
    data["project"]["title"] = "次の更新"
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    result = apply_registration(plan(update=True))
    assert result.exit_code == 0, result.model_dump()
    assert git("rev-parse", f"{github.ref('pirafu/register/sample-work')}^").decode() == old
    assert github.pr_calls == 2


def test_real_git_squash_merge_reconciles_with_two_parents(git_registration):
    config, data, github, plan, git = git_registration
    assert apply_registration(plan()).exit_code == 0
    old_head = github.ref("pirafu/register/sample-work")
    old_base = github.ref("main")
    tree = git("rev-parse", f"{old_head}^{{tree}}").decode()
    squash = git("commit-tree", tree, "-p", old_base, content=b"Squash registration\n").decode()
    git("update-ref", "refs/heads/main", squash)
    github.pull = None
    github.at_base = True
    github.reconcile = True
    data["project"]["title"] = "squash後の更新"
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    result = apply_registration(plan(update=True))
    assert result.exit_code == 0, result.model_dump()
    head = github.ref("pirafu/register/sample-work")
    assert git("rev-parse", f"{head}^1").decode() == old_head
    assert git("rev-parse", f"{head}^2").decode() == squash
    assert github.ref("main") == squash
