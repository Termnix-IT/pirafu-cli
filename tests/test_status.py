import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from test_cli import invoke

from pirafu_cli import status as status_module
from pirafu_cli.cloudflare import PagesReader
from pirafu_cli.results import PirafuError
from pirafu_cli.status import deployment, inspect_project, status

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def api_project():
    current = {
        "id": "11111111-1111-4111-8111-111111111111",
        "environment": "production",
        "latest_stage": {"name": "deploy", "status": "success"},
        "deployment_trigger": {"metadata": {"commit_hash": SHA_A}},
        "created_on": "2026-10-15T00:00:00Z",
        "env_vars": {"SECRET": "must-not-leak"},
    }
    return {
        "name": "sample-work",
        "subdomain": "sample-work.pages.dev",
        "production_branch": "main",
        "source": {
            "type": "github",
            "config": {
                "owner": "example",
                "repo_name": "sample-work",
                "production_branch": "main",
            },
        },
        "build_config": {
            "build_command": "npm run build",
            "root_dir": "/",
            "destination_dir": "dist",
        },
        "canonical_deployment": current,
        "latest_deployment": copy.deepcopy(current),
        "deployment_configs": {"production": {"env_vars": {"KEY": "must-not-leak"}}},
    }


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token-not-real")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "0" * 32)


def test_api_reads_only_named_project_and_does_not_follow_redirects(credentials):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.cloudflare.com"
        assert request.url.path.endswith("/pages/projects/sample-work")
        return httpx.Response(302, headers={"location": "https://example.com/"})

    reader = PagesReader(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PirafuError) as error:
            reader.get_project("sample-work")
        assert error.value.exit_code == 6 and len(calls) == 1
    finally:
        reader.close()


@pytest.mark.parametrize("code,exit_code", [(401, 4), (403, 4), (404, 5), (429, 6), (500, 6)])
def test_api_errors_are_classified_and_secret_bodies_are_discarded(
    credentials, monkeypatch, code, exit_code
):
    import pirafu_cli.cloudflare as module

    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code, text="must-not-leak")

    reader = PagesReader(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PirafuError) as error:
            reader.get_project("sample-work")
        assert error.value.exit_code == exit_code
        assert "must-not-leak" not in str(error.value)
        assert len(calls) == (3 if code in {429, 500} else 1)
    finally:
        reader.close()


def test_api_retry_recovers_and_limits_response(credentials, monkeypatch):
    import pirafu_cli.cloudflare as module

    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    replies = [
        httpx.Response(429),
        httpx.Response(200, json={"success": True, "result": {"name": "sample-work"}}),
    ]
    reader = PagesReader(transport=httpx.MockTransport(lambda request: replies.pop(0)))
    assert reader.get_project("sample-work")["name"] == "sample-work"
    reader.close()
    reader = PagesReader(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"a" * (1024 * 1024 + 1))
        )
    )
    with pytest.raises(PirafuError) as error:
        reader.get_project("sample-work")
    assert error.value.notice.code == "CLOUDFLARE_RESPONSE_LIMIT"
    reader.close()


@pytest.mark.parametrize(
    "payload", [b"not-json", b"[]", b'{"success":false}', b'{"success":true,"result":[]}']
)
def test_api_invalid_payload(credentials, payload):
    reader = PagesReader(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    )
    with pytest.raises(PirafuError) as error:
        reader.get_project("sample-work")
    assert error.value.exit_code == 6
    reader.close()


def test_api_network_timeout_is_sanitized(credentials):
    def handler(request):
        raise httpx.ReadTimeout("must-not-leak")

    reader = PagesReader(transport=httpx.MockTransport(handler))
    with pytest.raises(PirafuError) as error:
        reader.get_project("sample-work")
    assert error.value.exit_code == 6 and "must-not-leak" not in str(error.value)
    reader.close()


def test_missing_credentials_does_not_start_client(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("network client created"))
    with pytest.raises(PirafuError) as error:
        PagesReader()
    assert error.value.exit_code == 4


def test_success_projection_contains_no_management_data(api_project, pages_config):
    result = inspect_project(api_project, pages_config[1])
    assert result.exit_code == 0
    assert result.data["productionUrl"] == "https://sample-work.pages.dev/"
    assert result.data["currentProduction"]["commit"] == SHA_A
    text = result.model_dump_json()
    assert (
        "must-not-leak" not in text and "env_vars" not in text and "deployment_configs" not in text
    )


@pytest.mark.parametrize(
    "environment,state,expected_code",
    [
        ("preview", "failure", 0),
        ("preview", "active", 0),
        ("production", "failure", 3),
        ("production", "active", 5),
    ],
)
def test_latest_attempt_does_not_replace_current_production(
    api_project, pages_config, environment, state, expected_code
):
    api_project["latest_deployment"].update(
        {"environment": environment, "latest_stage": {"name": "build", "status": state}}
    )
    api_project["latest_deployment"]["deployment_trigger"]["metadata"]["commit_hash"] = SHA_B
    result = inspect_project(api_project, pages_config[1])
    assert result.exit_code == expected_code
    assert result.data["currentProduction"]["commit"] == SHA_A


def test_rollback_difference_is_reported_without_guessing_current_sha(api_project, pages_config):
    api_project["latest_deployment"]["deployment_trigger"]["metadata"]["commit_hash"] = SHA_B
    result = inspect_project(api_project, pages_config[1])
    assert result.exit_code == 0 and result.data["productionDiffersFromLatestAttempt"]
    assert result.data["currentProduction"]["commit"] == SHA_A


def test_missing_git_or_canonical_is_not_success(api_project, pages_config):
    api_project["source"] = None
    assert inspect_project(api_project, pages_config[1]).exit_code == 5
    api_project["source"] = {
        "type": "github",
        "config": {"production_branch": "main", "owner": "example", "repo_name": "sample-work"},
    }
    api_project["canonical_deployment"] = None
    assert inspect_project(api_project, pages_config[1]).exit_code == 5


@pytest.mark.parametrize(
    "field,value",
    [
        ("production_branch", "another"),
        ("subdomain", "localhost"),
        ("name", "another"),
        (
            "build_config",
            {"build_command": "different", "root_dir": "/", "destination_dir": "dist"},
        ),
    ],
)
def test_configuration_or_identity_mismatch_is_rejected(api_project, pages_config, field, value):
    api_project[field] = value
    try:
        result = inspect_project(api_project, pages_config[1])
        assert result.exit_code == 7
    except PirafuError as error:
        assert error.exit_code == 6


def test_preview_url_is_not_accepted_as_production_launch(api_project, pages_config):
    pages_config[1]["launch"]["url"] = "https://deadbeef.sample-work.pages.dev/"
    result = inspect_project(api_project, pages_config[1])
    assert result.exit_code == 7 and "launch.url" in result.data["mismatchedFields"]


def test_unknown_and_skipped_status_are_explicit():
    assert (
        deployment({"environment": "production", "latest_stage": {"status": "future"}})["state"]
        == "unknown"
    )
    assert deployment({"is_skipped": True})["state"] == "skipped"
    assert (
        deployment({"latest_stage": {"name": "build", "status": "success"}})["state"] == "building"
    )


def test_status_offline_and_cli_missing_auth(pages_config, monkeypatch, capsys):
    path, _ = pages_config
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    code, result = invoke(capsys, "status", "--config", path, "--offline", "--wait")
    assert code == 5 and result["data"]["deployment"] == "unknown"
    code, result = invoke(capsys, "status", "--config", path)
    assert code == 4 and result["errors"][0]["code"] == "CLOUDFLARE_CREDENTIALS_REQUIRED"


def test_wait_is_bounded_and_closes_reader(api_project, pages_config, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        status_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )

    class Reader:
        closed = False

        def get_project(self, *args, **kwargs):
            return api_project

        def close(self):
            self.closed = True

    reader = Reader()
    with pytest.raises(PirafuError) as error:
        status(
            pages_config[0],
            wait=True,
            timeout=2,
            interval=1,
            expected=SHA_B,
            reader_factory=lambda: reader,
        )
    assert error.value.exit_code == 6 and reader.closed and clock[0] == 2


def test_wait_observes_expected_canonical_commit(api_project, pages_config, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        status_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )

    class Reader:
        def get_project(self, *args, **kwargs):
            if clock[0] > 0:
                api_project["canonical_deployment"]["deployment_trigger"]["metadata"][
                    "commit_hash"
                ] = SHA_B
            return api_project

        def close(self):
            pass

    result = status(
        pages_config[0], wait=True, timeout=2, interval=1, expected=SHA_B, reader_factory=Reader
    )
    assert result.exit_code == 0 and result.data["currentProduction"]["commit"] == SHA_B


def test_online_check_uses_status_and_stays_json(pages_config, monkeypatch, capsys, api_project):
    class Reader:
        def get_project(self, *args, **kwargs):
            return api_project

        def close(self):
            pass

    monkeypatch.setattr(status_module, "PagesReader", Reader)
    code, result = invoke(capsys, "check", "--config", pages_config[0], "--online")
    assert code == 0 and result["data"]["deploymentCheck"]["configuration"] == "matched"
    assert "must-not-leak" not in json.dumps(result)
