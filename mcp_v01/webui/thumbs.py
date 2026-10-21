"""Derived preview thumbnails. Strictly read-only over originals: PDF page 1 is
rasterized with PyMuPDF, images are downscaled with Pillow (EXIF transpose on
the in-memory copy only). Cache lives outside the repository and is keyed by
file_id + sha256; cache misses never touch original bytes."""
import threading

from . import catalog

_lock = threading.Lock()
_WIDTH = 520


def thumb_path(config, file_row):
    cache_dir = config.base.parent / "local" / "webui-cache" / "thumbs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{file_row['file_id']}-{file_row['sha256'][:12]}.png"


def ensure_thumbnail(config, file_row):
    """Return cached PNG path or None if the type cannot be previewed."""
    ext = "." + (file_row["extension"] or "").lower()
    if ext == ".pdf":
        pass
    elif ext in (".png", ".jpg", ".jpeg"):
        pass
    else:
        return None
    target = thumb_path(config, file_row)
    if target.is_file() and target.stat().st_size > 0:
        return target
    with _lock:
        if target.is_file() and target.stat().st_size > 0:
            return target
        tmp = target.with_name(target.stem + ".pending.png")
        try:
            if ext == ".pdf":
                import pymupdf
                with pymupdf.open(str(file_row["path"])) as doc:
                    page = doc.load_page(0)
                    scale = min(_WIDTH / max(page.rect.width, 1), 1.5)
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                    pix.save(str(tmp))
            else:
                from PIL import Image, ImageOps
                with Image.open(file_row["path"]) as img:
                    img = ImageOps.exif_transpose(img)
                    img.thumbnail((_WIDTH, _WIDTH * 2))
                    img.convert("RGB").save(tmp, "PNG")
            tmp.replace(target)
        except Exception as error:
            config.log("webui.thumb_failed", file_id=file_row["file_id"], error=str(error))
            tmp.unlink(missing_ok=True)
            return None
    return target


def openable(file_row):
    return ("." + (file_row["extension"] or "").lower()) in catalog.OPENABLE_EXTS
