import copy
import json
from importlib.resources import files

import pytest

from pirafu_cli.contracts import bundle, safe_url, validate
from pirafu_cli.results import PirafuError

ROOT = files("pirafu_cli").joinpath("contracts/fixtures")
CASES = json.loads(ROOT.joinpath("cases.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_shared_contract_fixtures(case):
    data = json.loads(ROOT.joinpath(case["base"]).read_text(encoding="utf-8"))
    for change in case["changes"]:
        parent = data
        for part in change["path"][:-1]:
            parent = parent[part]
        parent[change["path"][-1]] = copy.deepcopy(change["value"])
    kind = "catalog" if case["kind"] == "record" else "manifest"
    if case["valid"]:
        validate(data, kind)
    else:
        with pytest.raises(PirafuError):
            validate(data, kind)


def test_bundle_has_pinned_revision_and_all_resources():
    schemas, source = bundle()
    assert len(source["commit"]) == 40
    assert len(schemas) == 3
    assert len(source["files"]) == 6


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "javascript:alert(1)",
        "https://user:password@example.com/",
        "https://example.com/?",
        "https://example.com/?#fragment",
        "https://example.com/?q=test#f",
        "https://localhost/",
        "https://machine.local/",
        "https://router.home.arpa/",
        "https://10.0.0.1/",
        "https://2130706433/",
        "https://0x7f000001/",
        "https://127.1/",
        "https://169.254.169.254/",
        "https://100.64.1.1/",
        "https://192.168.1.1/",
        "https://[::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://[fc00::1]/",
        "https://[2001:db8::1]/",
        "https://example.com\\@localhost/",
        "https://example.com:99999/",
    ],
)
def test_unsafe_urls_are_rejected_without_requests(url):
    with pytest.raises(PirafuError):
        safe_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/app/#start",
        "https://example.com/#/search?q=test",
        "https://8.8.8.8/",
        "https://[2606:4700:4700::1111]/",
    ],
)
def test_public_https_urls_and_fragment_queries(url):
    safe_url(url)
