"""Lossless saved-run compression with separate logical and stored-byte identities."""

from __future__ import annotations

import gzip
import hashlib
import lzma
import shutil
import time
from pathlib import Path
from typing import Any, BinaryIO


def stored_path(path: Path) -> Path:
    """Resolve an optional lossless sibling without changing a declared logical name."""
    if path.exists() or path.suffix in {".gz", ".xz"}:
        return path
    candidates = [path.with_name(path.name + suffix) for suffix in (".gz", ".xz")]
    found = [p for p in candidates if p.exists()]
    if len(found) > 1:
        raise ValueError("ambiguous compressed artifacts for logical path")
    return found[0] if found else path


def open_saved(path: Path) -> BinaryIO:
    """Open logical bytes from an ordinary, gzip or XZ saved artifact."""
    path = stored_path(path)
    if path.suffix == ".gz":
        return gzip.open(path, "rb")
    return lzma.open(path, "rb") if path.suffix == ".xz" else path.open("rb")


def read_saved(path: Path) -> bytes:
    """Read original bytes, checking the archive's integrity on decompression."""
    with open_saved(path) as stream:
        return stream.read()


def stored_sha256(path: Path) -> str:
    """Hash the physical stored bytes, independently of logical JSON/event hashes."""
    digest = hashlib.sha256()
    with stored_path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def events_for_result(path: Path) -> Path:
    """Find the raw event stream for ordinary or losslessly archived results."""
    logical = path.with_suffix("") if path.suffix in {".gz", ".xz"} else path
    return stored_path(logical.with_suffix(".events.jsonl"))


def compress_file(path: Path, codec: str = "gzip") -> dict[str, Any]:
    """Verify an archive against original bytes before removing only that original."""
    if codec not in {"gzip", "xz"}:
        raise ValueError("unsupported lossless archive codec")
    target = path.with_name(path.name + (".gz" if codec == "gzip" else ".xz"))
    logical_sha = stored_sha256(path)
    size = path.stat().st_size
    if not target.exists():
        pending = target.with_name(target.name + ".pending")
        with pending.open("xb") as stream:
            writer = (gzip.GzipFile(filename="", fileobj=stream, mode="wb", compresslevel=6, mtime=0)
                      if codec == "gzip" else lzma.LZMAFile(stream, mode="wb", preset=6))
            with writer as archived:
                with path.open("rb") as original:
                    shutil.copyfileobj(original, archived, length=1024 * 1024)
        pending.replace(target)
    digest = hashlib.sha256()
    with open_saved(target) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != logical_sha:
        raise ValueError("archive differs from original; original preserved")
    result = {"logical_sha256": logical_sha, "stored_sha256": stored_sha256(target),
              "logical_bytes": size, "stored_bytes": target.stat().st_size}
    path.unlink()
    return result


def archive_run(path: Path, codec: str = "gzip") -> tuple[Path, dict[str, Any]]:
    """Compress completed result/events/CAS after worker disposal, preserving all data."""
    import json

    path = path.resolve()
    if path.suffix in {".gz", ".xz"}:
        raise ValueError("run is already archived")
    started = time.perf_counter()
    run = json.loads(path.read_text(encoding="utf-8"))
    paths = [path, events_for_result(path)]
    for entry in (run.get("control") or {}).get("planning_inputs", {}).values():
        if "path" not in entry:
            continue
        target = (path.parent / entry["path"]).resolve()
        if Path(entry["path"]).is_absolute() or not target.is_relative_to(path.parent.resolve()):
            raise ValueError("CAS archive path escapes run directory")
        if target.exists():
            paths.append(target)
    records = {str(p.relative_to(path.parent)): compress_file(p, codec) for p in dict.fromkeys(paths)}
    return stored_path(path), {
        "archive_wall_seconds": time.perf_counter() - started, "archive_files": records,
        "archive_bytes": sum(r["stored_bytes"] for r in records.values()),
        "logical_bytes": sum(r["logical_bytes"] for r in records.values()),
    }
