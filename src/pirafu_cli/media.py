import io
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from pirafu_cli.files import inside, read_bytes
from pirafu_cli.results import PirafuError


def check_image(root: Path, image: dict) -> dict:
    path = inside(root, image["path"], "catalog.image.path")
    content = read_bytes(path, 2 * 1024 * 1024)
    return check_image_bytes(content, path.suffix.lower())


def check_image_bytes(content: bytes, suffix: str) -> dict:
    expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with Image.open(io.BytesIO(content)) as source:
                width, height = source.size
                if (
                    source.format != expected.get(suffix)
                    or not 0 < width <= 4096
                    or not 0 < height <= 4096
                    or getattr(source, "is_animated", False)
                    or getattr(source, "n_frames", 1) != 1
                ):
                    raise PirafuError(
                        "IMAGE_INVALID", "画像の形式・寸法・静止画制約に違反します。", 3
                    )
                source.verify()
            with Image.open(io.BytesIO(content)) as source:
                source.load()
        return {"width": width, "height": height}
    except (OSError, ValueError, Warning, UnidentifiedImageError) as exc:
        raise PirafuError("IMAGE_DECODE_FAILED", "画像を安全にデコードできません。", 3) from exc
