"""Explicit, local installation and restoration of the development scaffold."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parent.parent
FILES = ("__init__.py", "box.py", "configuration.py", "cutting.py", "errors.py",
         "events.py", "extrusion.py", "feeding.py", "finishing.py", "firmware.py",
         "lifecycle.py", "loading.py", "motion.py", "native.py", "operations.py",
         "printing.py", "protocol.py", "recovery.py", "resume.py", "retraction.py",
         "runout.py", "state.py", "storage.py", "toolchange.py")
BOX_CACHE = re.compile(r"box\.cpython-[0-9]+(?:\.opt-[0-9]+)?\.pyc")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path):
    """Reject traversal and every existing symlink or Windows reparse point."""
    path = Path(path)
    if ".." in path.parts:
        raise ValueError("parent traversal is not allowed")
    path = Path(os.path.abspath(str(path)))
    for part in (path,) + tuple(path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("links and reparse points are not allowed: " + str(part))
    return path


def inputs(extras, backup):
    extras, backup = safe_path(extras), safe_path(backup)
    if not extras.is_dir() or not backup.parent.is_dir():
        raise ValueError("extras and the backup parent must exist")
    if extras == backup or extras in backup.parents or backup in extras.parents:
        raise ValueError("backup and extras must be separate")
    return extras, backup


def sources():
    source_dir = safe_path(ROOT / "src" / "cfs")
    if {path.name for path in source_dir.glob("*.py")} != set(FILES):
        raise ValueError("source Python files differ from the installation allowlist")
    paths = {"box.py": ROOT / "klipper" / "box.py"}
    paths.update({"cfs/" + name: source_dir / name for name in FILES})
    return {name: safe_path(path).read_bytes() for name, path in paths.items()}


def prepare(extras, backup):
    extras, backup = inputs(extras, backup)
    for path in extras.iterdir():
        name = path.name.lower()
        if name.startswith("box.") and (name.endswith(".so") or name.endswith(".pyd")):
            raise ValueError("native box entry point shadows box.py: " + path.name)
    box = safe_path(extras / "box.py")
    if not box.is_file() or (extras / "cfs").exists() or (extras / "cfs").is_symlink():
        raise ValueError("a regular box.py and absent cfs directory are required")
    if backup.exists() or backup.is_symlink():
        raise ValueError("backup already exists")
    box_caches(extras)
    info = box.stat()
    original = box.read_bytes()
    return extras, backup, original, info, sources()


def write_exclusive(path, data):
    with path.open("xb") as output:
        output.write(data)


def box_caches(extras):
    """Return only regular CPython entry-point caches, leaving other files alone."""
    cache = safe_path(extras / "__pycache__")
    if not cache.exists():
        return False, []
    if not cache.is_dir():
        raise ValueError("extras bytecode cache is not a directory")
    paths = []
    for path in cache.iterdir():
        if BOX_CACHE.fullmatch(path.name):
            safe_path(path)
            if not path.is_file():
                raise ValueError("invalid box bytecode cache: " + path.name)
            paths.append(path)
    return True, sorted(paths)


def cache_snapshot(extras):
    existed, paths = box_caches(extras)
    saved = {}
    for path in paths:
        info = path.stat()
        data = path.read_bytes()
        saved[path.name] = (data, {"sha256": digest(data), "mode": stat.S_IMODE(info.st_mode),
                                   "atime_ns": info.st_atime_ns, "mtime_ns": info.st_mtime_ns})
    return existed, saved


def put_caches(extras, saved):
    if saved:
        (extras / "__pycache__").mkdir(exist_ok=True)
    for name, (data, info) in saved.items():
        path = extras / "__pycache__" / name
        path.write_bytes(data)
        os.chmod(str(path), info["mode"])
        os.utime(str(path), ns=(info["atime_ns"], info["mtime_ns"]))


def install(extras, backup):
    extras, backup, original, info, files = prepare(extras, backup)
    cache_existed, saved_cache = cache_snapshot(extras)
    manifest = {"version": 2, "extras": str(extras), "original": digest(original),
                "mode": stat.S_IMODE(info.st_mode), "atime_ns": info.st_atime_ns,
                "mtime_ns": info.st_mtime_ns,
                "cache_existed": cache_existed,
                "box_caches": {name: metadata for name, (_, metadata) in saved_cache.items()},
                "installed": {name: digest(data) for name, data in files.items()}}
    backup.mkdir()
    write_exclusive(backup / "box.py.original", original)
    if saved_cache:
        (backup / "box-caches").mkdir()
        for name, (data, _) in saved_cache.items():
            write_exclusive(backup / "box-caches" / name, data)
    write_exclusive(backup / "manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
    created = []
    cfs_created = False
    box_changed = False
    removed_cache = []
    try:
        # Recheck immediately before modifying anything; never merge an existing tree.
        if digest(safe_path(extras / "box.py").read_bytes()) != manifest["original"]:
            raise ValueError("box.py changed during preparation")
        (extras / "cfs").mkdir()
        cfs_created = True
        for name in FILES:
            path = extras / "cfs" / name
            with path.open("xb") as output:
                created.append(path)
                output.write(files["cfs/" + name])
        # A same-second, same-size source can otherwise reuse the previous bytecode.
        for name in saved_cache:
            path = extras / "__pycache__" / name
            path.unlink()
            removed_cache.append(name)
        box_changed = True
        (extras / "box.py").write_bytes(files["box.py"])
    except Exception:
        if box_changed:
            (extras / "box.py").write_bytes(original)
            os.chmod(str(extras / "box.py"), manifest["mode"])
            os.utime(str(extras / "box.py"), ns=(manifest["atime_ns"], manifest["mtime_ns"]))
        put_caches(extras, {name: saved_cache[name] for name in removed_cache})
        for path in reversed(created):
            path.unlink()
        if cfs_created:
            (extras / "cfs").rmdir()
        raise


def restore(extras, backup):
    extras, backup = inputs(extras, backup)
    manifest = json.loads(safe_path(backup / "manifest.json").read_text(encoding="utf-8"))
    original = safe_path(backup / "box.py.original").read_bytes()
    names = {"box.py"} | {"cfs/" + name for name in FILES}
    if (manifest.get("version") != 2 or manifest.get("extras") != str(extras)
            or set(manifest.get("installed", {})) != names
            or digest(original) != manifest.get("original")
            or not isinstance(manifest.get("cache_existed"), bool)
            or not isinstance(manifest.get("box_caches"), dict)
            or not all(isinstance(manifest.get(key), int) and manifest[key] >= 0
                       for key in ("mode", "atime_ns", "mtime_ns"))):
        raise ValueError("invalid backup manifest or original")
    saved_cache = {}
    for name, metadata in manifest["box_caches"].items():
        if (not isinstance(name, str) or not BOX_CACHE.fullmatch(name)
                or not isinstance(metadata, dict)
                or not all(isinstance(metadata.get(key), int) and metadata[key] >= 0
                           for key in ("mode", "atime_ns", "mtime_ns"))):
            raise ValueError("invalid backup cache manifest")
        data = safe_path(backup / "box-caches" / name).read_bytes()
        if digest(data) != metadata.get("sha256"):
            raise ValueError("backup cache has changed: " + name)
        saved_cache[name] = (data, metadata)
    if saved_cache and not manifest["cache_existed"]:
        raise ValueError("invalid backup cache state")
    _, current_caches = box_caches(extras)
    cfs = safe_path(extras / "cfs")
    if not cfs.is_dir():
        raise ValueError("installed cfs directory has changed")
    entries = {p.name for p in cfs.iterdir()}
    if entries - {"__pycache__"} != set(FILES):
        raise ValueError("installed cfs directory has changed")
    cache_files = []
    if "__pycache__" in entries:
        cache = safe_path(cfs / "__pycache__")
        if not cache.is_dir():
            raise ValueError("invalid cfs bytecode cache")
        stems = {Path(name).stem for name in FILES}
        for path in cache.iterdir():
            safe_path(path)
            match = re.fullmatch(r"(.+)\.cpython-[0-9]+(?:\.opt-[0-9]+)?\.pyc", path.name)
            if not match or match.group(1) not in stems or not path.is_file():
                raise ValueError("unexpected cfs bytecode cache entry: " + path.name)
            cache_files.append(path)
    for name in names:
        path = safe_path(extras / name)
        if not path.is_file() or digest(path.read_bytes()) != manifest["installed"][name]:
            raise ValueError("installed file has changed: " + name)
    # Restore the irreplaceable original before removing our unchanged files.
    box = extras / "box.py"
    for path in current_caches:
        path.unlink()
    box.write_bytes(original)
    os.chmod(str(box), manifest["mode"])
    if digest(box.read_bytes()) != manifest["original"]:
        raise ValueError("restored box.py verification failed")
    os.utime(str(box), ns=(manifest["atime_ns"], manifest["mtime_ns"]))
    put_caches(extras, saved_cache)
    cache_dir = extras / "__pycache__"
    if not manifest["cache_existed"] and cache_dir.exists() and not any(cache_dir.iterdir()):
        cache_dir.rmdir()
    for name in FILES:
        (cfs / name).unlink()
    for path in cache_files:
        path.unlink()
    if "__pycache__" in entries:
        (cfs / "__pycache__").rmdir()
    cfs.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "install", "restore"), nargs="?", default="plan")
    parser.add_argument("--extras", required=True)
    parser.add_argument("--backup", required=True)
    args = parser.parse_args()
    try:
        if args.action == "plan":
            extras, backup, _, _, files = prepare(args.extras, args.backup)
            print("Target:", extras, "\nBackup:", backup)
            print("Replace box.py and create cfs/ with", len(files) - 1, "Python files.")
            print("Preview only; bootstrap still refuses activation.")
        elif args.action == "install":
            install(args.extras, args.backup)
            print("Installed scaffold; bootstrap still refuses activation.")
        else:
            restore(args.extras, args.backup)
            print("Restored original box.py; backup retained.")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Installation error:", error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
