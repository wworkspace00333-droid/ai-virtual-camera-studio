"""Portable profile bundles (.vcsprofile): the profile JSON plus every asset it references, in one zip.

Layout:  profile.json            (same document as a plain profile export)
         assets/<file>           (files from the app's assets folder, by their stored name)
Import treats the archive as untrusted input: only those two kinds of entries are accepted, names are
validated (no paths, no traversal), sizes are bounded, and identical existing assets are reused.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

from vcstudio.core.profiles import PROFILE_EXPORT_KIND, ProfileError

BUNDLE_SUFFIX = ".vcsprofile"
MAX_ASSET_BYTES = 2 * 1024**3          # per file
MAX_TOTAL_BYTES = 8 * 1024**3
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export_bundle(ctx, name: str, dest: Path) -> Path:
    doc = ctx.profiles.get(name)
    doc["kind"] = PROFILE_EXPORT_KIND
    dest = Path(dest)
    if dest.suffix != BUNDLE_SUFFIX:
        dest = dest.with_suffix(BUNDLE_SUFFIX)
    missing = []
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("profile.json", json.dumps(doc, indent=2, ensure_ascii=False))
        for a in doc["body"].get("assets", []):
            p = ctx.assets.dir / a["file"]
            if p.exists():
                # videos and images are already compressed: store them without re-compressing
                z.write(p, f"assets/{a['file']}", compress_type=zipfile.ZIP_STORED)
            else:
                missing.append(a["file"])
    if missing:
        raise ProfileError(f"Exported, but these asset files were missing: {', '.join(missing)}")
    return dest


def import_bundle(ctx, src: Path) -> dict:
    src = Path(src)
    try:
        z = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise ProfileError("Not a valid .vcsprofile file")
    with z:
        names = z.namelist()
        if "profile.json" not in names:
            raise ProfileError("Bundle has no profile.json")
        total = 0
        assets = []
        for info in z.infolist():
            if info.filename == "profile.json" or info.filename == "assets/":
                continue
            parts = info.filename.split("/")
            if len(parts) != 2 or parts[0] != "assets" or not _SAFE_NAME.match(parts[1]) or parts[1] in (".", ".."):
                raise ProfileError(f"Unexpected entry in bundle: {info.filename!r}")
            if info.file_size > MAX_ASSET_BYTES:
                raise ProfileError(f"Asset too large: {parts[1]}")
            total += info.file_size
            assets.append((info, parts[1]))
        if total > MAX_TOTAL_BYTES:
            raise ProfileError("Bundle too large")
        try:
            doc = json.loads(z.read("profile.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProfileError("profile.json is not valid JSON")
        referenced = {a.get("file") for a in doc.get("body", {}).get("assets", []) if isinstance(a, dict)}
        ctx.assets.dir.mkdir(parents=True, exist_ok=True)
        written = []
        try:
            for info, fname in assets:
                if fname not in referenced:
                    continue                                    # ignore files the profile does not use
                target = ctx.assets.dir / fname
                tmp = ctx.assets.dir / f".import-{fname}"
                with z.open(info) as fsrc, open(tmp, "wb") as fdst:
                    while chunk := fsrc.read(1 << 20):
                        fdst.write(chunk)
                if target.exists() and _sha256(target) == _sha256(tmp):
                    tmp.unlink()                                # identical asset already present: reuse
                    continue
                if target.exists():                             # same name, different content: keep both
                    raise ProfileError(f"A different asset named {fname} already exists")
                tmp.rename(target)
                written.append(target)
            missing = [f for f in referenced if f and not (ctx.assets.dir / f).exists()]
            if missing:
                raise ProfileError(f"Bundle is missing asset files: {', '.join(sorted(missing))}")
            tmp_json = ctx.assets.dir / ".import-profile.json"
            tmp_json.write_text(json.dumps(doc))
            try:
                return ctx.profiles.import_file(tmp_json)
            finally:
                tmp_json.unlink(missing_ok=True)
        except Exception:
            for p in written:                                   # roll back partially imported assets
                p.unlink(missing_ok=True)
            for p in ctx.assets.dir.glob(".import-*"):
                p.unlink(missing_ok=True)
            raise
