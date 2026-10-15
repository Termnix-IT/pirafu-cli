import json
import math
import re
import stat
from pathlib import Path
from typing import Any

import yaml
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent

from pirafu_cli.results import PirafuError

MAX_INPUT = 256 * 1024
RESERVED = re.compile(r"^(?:con|prn|aux|nul|com[0-9]|lpt[0-9]|conin|conout)$", re.I)


def safe_relative(value: str, field: str, allow_dot: bool = False) -> None:
    if allow_dot and value == ".":
        return
    parts = value.split("/")
    if (
        not value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(c in value for c in ':\\<>"|?*')
        or any(
            not p
            or p in {".", ".."}
            or p.endswith((".", " "))
            or RESERVED.fullmatch(p.split(".")[0])
            for p in parts
        )
    ):
        raise PirafuError("UNSAFE_PATH", "安全な相対パスを指定してください。", 3, field)


def no_links(path: Path) -> None:
    # resolve()より先に確認し、リンク先の参照による境界検査の迂回を防ぐ。
    for part in reversed([path, *path.parents]):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise PirafuError("LINK_FORBIDDEN", "symlink/junctionは参照できません。", 3)


def inside(root: Path, relative: str, field: str, *, allow_dot: bool = False) -> Path:
    safe_relative(relative, field, allow_dot)
    path = root / relative
    no_links(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise PirafuError("PATH_ESCAPE", "リポジトリ外への参照を拒否しました。", 3, field)
    return path


def read_bytes(path: Path, limit: int = MAX_INPUT) -> bytes:
    no_links(path)
    try:
        if not path.is_file():
            raise PirafuError("FILE_MISSING", "必要なファイルが見つかりません。", 3)
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
    except OSError as exc:
        raise PirafuError("FILE_UNREADABLE", "ファイルを読み取れません。", 3) from exc
    if len(content) > limit:
        raise PirafuError("INPUT_TOO_LARGE", "ファイルが容量上限を超えています。", 3)
    return content


def _mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise PirafuError("YAML_KEY_INVALID", "YAMLの重複キー・非文字列キーを拒否しました。")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


class StrictLoader(yaml.SafeLoader):
    pass


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _json_values(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PirafuError("YAML_TYPE_INVALID", "文字列以外のキーは使用できません。")
            _json_values(item)
    elif isinstance(value, list):
        for item in value:
            _json_values(item)
    elif type(value) not in {str, int, float, bool, type(None)} or (
        isinstance(value, float) and not math.isfinite(value)
    ):
        raise PirafuError("YAML_TYPE_INVALID", "JSONへ変換できないYAML型を拒否しました。")


def load_manifest(path: Path) -> dict:
    content = read_bytes(path)
    try:
        text = content.decode("utf-8-sig")
        depth = 0
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, AliasEvent) or getattr(event, "anchor", None):
                raise PirafuError("YAML_ALIAS_FORBIDDEN", "アンカー・エイリアスは使用できません。")
            if getattr(event, "tag", None):
                raise PirafuError("YAML_TAG_FORBIDDEN", "明示的なYAMLタグは使用できません。")
            if isinstance(event, CollectionStartEvent):
                depth += 1
                if depth > 64:
                    raise PirafuError("YAML_TOO_DEEP", "YAMLの入れ子が深すぎます。")
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
        data = yaml.load(text, Loader=StrictLoader)
        _json_values(data)
        if not isinstance(data, dict):
            raise PirafuError("MANIFEST_INVALID", "設定は単一のオブジェクトにしてください。")
        return data
    except (yaml.YAMLError, UnicodeError, RecursionError, ValueError) as exc:
        # parser例外は値や原文を含むため出力しない。
        raise PirafuError("YAML_INVALID", "UTF-8の単一YAML文書として読み取れません。") from exc


def read_json(path: Path) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        return json.loads(read_bytes(path).decode("utf-8-sig"), object_pairs_hook=unique)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PirafuError("JSON_INVALID", "JSONを読み取れません。") from exc
