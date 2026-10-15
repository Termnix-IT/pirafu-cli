import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from importlib.resources import files
from ipaddress import ip_address
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from pirafu_cli.files import RESERVED, safe_relative
from pirafu_cli.results import PirafuError


@lru_cache(maxsize=1)
def bundle() -> tuple[dict, dict]:
    root = files("pirafu_cli").joinpath("contracts")
    source = json.loads(root.joinpath("source.json").read_text(encoding="utf-8"))
    schemas = {}
    for relative, expected in source["files"].items():
        content = root.joinpath(relative).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise PirafuError("CONTRACT_INTEGRITY", "同梱契約のハッシュが一致しません。", 1)
        if relative.startswith("v1/"):
            schema = json.loads(content)
            Draft202012Validator.check_schema(schema)
            schemas[schema["$id"]] = schema
    return schemas, source


def safe_url(value: str) -> None:
    def reject():
        raise PirafuError("UNSAFE_URL", "公開可能なHTTPSのURLを指定してください。", 3)

    if (
        not value.startswith("https://")
        or "?" in value.split("#", 1)[0]
        or "\\" in value
        or any(ord(c) <= 32 or ord(c) == 127 for c in value)
    ):
        reject()
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower().removesuffix(".")
        _ = url.port
        if not host or url.username is not None or url.password is not None:
            reject()
        try:
            ip = ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            if ip.version == 4:
                a, b, c, _ = ip.packed
                reserved = (
                    a in {0, 10, 127}
                    or a >= 224
                    or (a == 100 and 64 <= b <= 127)
                    or (a == 169 and b == 254)
                    or (a == 172 and 16 <= b <= 31)
                    or (a == 192 and (b in {0, 168} or (b == 88 and c == 99)))
                    or (a == 198 and (b in {18, 19} or (b == 51 and c == 100)))
                    or (a == 203 and b == 0 and c == 113)
                )
            else:
                first = int(ip) >> 112
                second = (int(ip) >> 96) & 0xFFFF
                reserved = (
                    first >> 12 not in {2, 3}
                    or first in {0x2002, 0x3FFF}
                    or (first == 0x2001 and (second < 0x200 or second == 0xDB8))
                )
            if reserved:
                reject()
        else:
            # WHATWG URLがIPに解釈する短縮・整数・16進形式を曖昧なホストとして拒否する。
            parts = host.split(".")
            if (
                len(parts) < 2
                or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", parts[-1])
                or re.search(
                    r"(^|\.)(localhost|local|localdomain|internal|intranet|lan|home|invalid|test|onion)$",
                    host,
                )
                or host == "home.arpa"
                or host.endswith(".home.arpa")
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", p) for p in parts)
            ):
                reject()
    except ValueError as exc:
        raise PirafuError("UNSAFE_URL", "URLのホスト・ポートが不正です。", 3) from exc


def validate(data: dict, kind: str = "manifest") -> dict:
    schemas, _ = bundle()
    schema_id = f"https://pirafu2411.example/contracts/v1/{kind}.schema.json"
    registry = Registry().with_resources(
        (key, Resource.from_contents(value)) for key, value in schemas.items()
    )
    validator = Draft202012Validator(
        schemas[schema_id], registry=registry, format_checker=FormatChecker()
    )
    error = next(validator.iter_errors(data), None)
    if error is not None:
        field = ".".join(str(part) for part in error.absolute_path) or "/"
        raise PirafuError("SCHEMA_INVALID", "同梱v1スキーマに適合しません。", 2, field)
    project = data["project"]
    if RESERVED.fullmatch(project["id"]):
        raise PirafuError("RESERVED_ID", "Windowsの予約名はIDに使用できません。", 3)
    tags = [unicodedata.normalize("NFKC", t).strip().lower() for t in project.get("tags", [])]
    if not all(tags) or len(set(tags)) != len(tags):
        raise PirafuError("TAGS_INVALID", "タグは正規化後も空でなく一意にしてください。", 3)
    for value in [data["launch"].get("url"), *data.get("links", {}).values()]:
        if value:
            safe_url(value)
    for image in images(data):
        safe_relative(image["path"], "catalog.image.path")
        if kind == "catalog" and not image["path"].startswith(f"media/{project['id']}/"):
            raise PirafuError("IMAGE_PATH_INVALID", "作品のメディア領域を指定してください。", 3)
    hosting = data.get("hosting", {})
    if hosting.get("provider") == "cloudflare-pages":
        for field in ("rootDirectory", "outputDirectory"):
            safe_relative(hosting[field], f"hosting.{field}", allow_dot=True)
        if any(ord(c) < 32 or ord(c) == 127 for c in hosting["buildCommand"]):
            raise PirafuError("BUILD_COMMAND_INVALID", "ビルドコマンドに制御文字は使えません。", 3)
    for argv in data.get("local", {}).values():
        if not argv[0].strip() or argv[0] != argv[0].strip() or any("\0" in a for a in argv):
            raise PirafuError("ARGV_INVALID", "ローカルコマンドの引数配列が不正です。", 3)
    return data


def images(data: dict) -> list[dict]:
    catalog = data["catalog"]
    return ([catalog["thumbnail"]] if "thumbnail" in catalog else []) + catalog.get(
        "screenshots", []
    )
