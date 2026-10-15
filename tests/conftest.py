import pytest
import yaml


@pytest.fixture
def pages_config(tmp_path):
    root = tmp_path / "日本語 空白の作品"
    root.mkdir()
    (root / "dist").mkdir()
    (root / "dist/index.html").write_text(
        "<!doctype html><title>Preview fixture</title>", encoding="utf-8"
    )
    data = {
        "schemaVersion": 1,
        "project": {
            "id": "sample-work",
            "title": "作品",
            "description": "説明",
            "category": "apps",
            "status": "planned",
            "visibility": "hidden",
        },
        "hosting": {
            "provider": "cloudflare-pages",
            "mode": "git",
            "projectName": "sample-work",
            "productionBranch": "main",
            "rootDirectory": ".",
            "outputDirectory": "dist",
            "buildCommand": "npm run build",
        },
        "launch": {"type": "external", "url": "https://sample-work.pages.dev/"},
        "links": {"repository": "https://github.com/example/sample-work"},
        "catalog": {},
    }
    config = root / "pirafu.yaml"
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return config, data
