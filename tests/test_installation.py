import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("installer", Path(__file__).resolve().parents[1] / "tools" / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.extras = self.root / "extras"
        self.extras.mkdir()
        self.box = self.extras / "box.py"
        self.original = b"\x00\xffstock\r\n"
        self.box.write_bytes(self.original)
        self.backup = self.root / "backup"
        (self.extras / "box_wrapper.so").write_bytes(b"native")
        (self.extras / "serial_485.so").write_bytes(b"serial")
        (self.root / "printer.cfg").write_bytes(b"config")

    def test_preview_and_roundtrip(self):
        os.chmod(str(self.box), 0o600)
        original_stat = self.box.stat()
        installer.prepare(self.extras, self.backup)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.box.read_bytes(), self.original)
        installer.install(self.extras, self.backup)
        installer.restore(self.extras, self.backup)
        restored = self.box.stat()
        self.assertEqual(self.box.read_bytes(), self.original)
        self.assertEqual(restored.st_mode, original_stat.st_mode)
        self.assertEqual(restored.st_mtime_ns, original_stat.st_mtime_ns)
        self.assertFalse((self.extras / "cfs").exists())
        self.assertEqual((self.extras / "box_wrapper.so").read_bytes(), b"native")
        self.assertEqual((self.extras / "serial_485.so").read_bytes(), b"serial")
        self.assertEqual((self.root / "printer.cfg").read_bytes(), b"config")
        self.assertTrue(self.backup.exists())

    def test_partial_install_rolls_back(self):
        real_open = Path.open
        def fail(path, *args, **kwargs):
            if path.name == "errors.py" and args and args[0] == "xb":
                raise PermissionError("simulated denial")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", fail):
            with self.assertRaises(PermissionError):
                installer.install(self.extras, self.backup)
        self.assertEqual(self.box.read_bytes(), self.original)
        self.assertFalse((self.extras / "cfs").exists())

    def test_partial_box_write_rolls_back(self):
        real_write = Path.write_bytes
        failed = []
        cache = self.extras / "__pycache__"
        cache.mkdir()
        prior = cache / "box.cpython-38.pyc"
        prior.write_bytes(b"original cache")
        legacy = self.extras / "box.pyc"
        legacy.write_bytes(b"original legacy cache")
        legacy_stat = legacy.stat()
        def fail_once(path, data):
            if path == self.box and not failed:
                failed.append(True)
                real_write(path, b"partial")
                raise PermissionError("simulated interrupted write")
            return real_write(path, data)
        with mock.patch.object(Path, "write_bytes", fail_once):
            with self.assertRaises(PermissionError):
                installer.install(self.extras, self.backup)
        self.assertEqual(self.box.read_bytes(), self.original)
        self.assertEqual(prior.read_bytes(), b"original cache")
        self.assertEqual(legacy.read_bytes(), b"original legacy cache")
        self.assertEqual(legacy.stat().st_mtime_ns, legacy_stat.st_mtime_ns)
        self.assertFalse((self.extras / "cfs").exists())
        self.assertEqual((self.backup / "box.py.original").read_bytes(), self.original)

    def test_import_validates_configuration_then_restore_removes_bytecode(self):
        installer.install(self.extras, self.backup)
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from extras.box import load_config; "
            "Config = type('Config', (), {'error': lambda self, msg: RuntimeError(msg), "
            "'get': lambda self, name, default=None: default, "
            "'getboolean': lambda self, name, default=None: default}); "
            "load_config(Config())"
        )
        result = subprocess.run([sys.executable, "-c", code, str(self.root)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cfs: set the serial_485 section name in [box] bus", result.stderr)
        self.assertTrue((self.extras / "cfs" / "__pycache__").is_dir())
        installer.restore(self.extras, self.backup)
        self.assertEqual(self.box.read_bytes(), self.original)
        self.assertFalse((self.extras / "cfs").exists())

    def test_entry_bytecode_collision_in_both_directions(self):
        source = (installer.ROOT / "klipper" / "box.py").read_bytes()
        original = b"VALUE = 1\n" + b" " * (len(source) - len(b"VALUE = 1\n"))
        self.box.write_bytes(original)
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import extras.box; "
                "print(getattr(extras.box, 'VALUE', 'scaffold'))")
        def load():
            return subprocess.check_output([sys.executable, "-c", code, str(self.root)],
                                           text=True).strip()
        self.assertEqual(load(), "1")
        cache = self.extras / "__pycache__"
        original_cache = next(cache.glob("box.cpython-*.pyc"))
        original_data = original_cache.read_bytes()
        original_stat = original_cache.stat()
        other = cache / "other.cpython-38.pyc"
        other.write_bytes(b"unrelated")
        source_stat = self.box.stat()
        installer.install(self.extras, self.backup)
        # Force the collision explicitly, independent of test execution speed.
        os.utime(str(self.box), ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        self.assertEqual(load(), "scaffold")
        installer.restore(self.extras, self.backup)
        self.assertEqual(original_cache.read_bytes(), original_data)
        self.assertEqual(original_cache.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertEqual(other.read_bytes(), b"unrelated")
        self.assertEqual(load(), "1")

    def test_generated_entry_cache_removed_when_original_had_none(self):
        installer.install(self.extras, self.backup)
        code = "import sys; sys.path.insert(0, sys.argv[1]); import extras.box"
        subprocess.check_call([sys.executable, "-c", code, str(self.root)])
        cache = self.extras / "__pycache__"
        self.assertTrue(list(cache.glob("box.cpython-*.pyc")))
        other = cache / "other.pyc"
        other.write_bytes(b"unrelated")
        installer.restore(self.extras, self.backup)
        self.assertFalse(list(cache.glob("box.cpython-*.pyc")))
        self.assertEqual(other.read_bytes(), b"unrelated")

    def test_mixed_cache_roundtrip_restores_original_bytes_mode_and_time(self):
        legacy = self.extras / "box.pyc"
        legacy.write_bytes(b"original legacy cache")
        os.chmod(str(legacy), 0o600)
        os.utime(str(legacy), ns=(1600000000123456700, 1600000000987654300))
        legacy_stat = legacy.stat()
        modern = self.extras / "__pycache__"
        modern.mkdir()
        prior = modern / "box.cpython-38.pyc"
        prior.write_bytes(b"original modern cache")
        prior_stat = prior.stat()
        unrelated = self.extras / "other.pyc"
        unrelated.write_bytes(b"user cache")
        installer.install(self.extras, self.backup)
        self.assertFalse(legacy.exists())
        self.assertFalse(prior.exists())
        manifest = json.loads((self.backup / "manifest.json").read_text())
        self.assertEqual(manifest["version"], 3)
        self.assertEqual(set(manifest["box_caches"]), {"box.pyc", prior.name})
        legacy.write_bytes(b"replacement legacy cache")
        prior.write_bytes(b"replacement modern cache")
        package = self.extras / "cfs"
        for name in installer.FILES:
            (package / Path(name).with_suffix(".pyc")).write_bytes(b"replacement package cache")
        (package / "__pycache__").mkdir()
        (package / "__pycache__" / "box.cpython-38.pyc").write_bytes(b"modern package cache")
        installer.restore(self.extras, self.backup)
        self.assertEqual(legacy.read_bytes(), b"original legacy cache")
        self.assertEqual(legacy.stat().st_mode, legacy_stat.st_mode)
        self.assertEqual(legacy.stat().st_mtime_ns, legacy_stat.st_mtime_ns)
        self.assertEqual(prior.read_bytes(), b"original modern cache")
        self.assertEqual(prior.stat().st_mtime_ns, prior_stat.st_mtime_ns)
        self.assertEqual(unrelated.read_bytes(), b"user cache")
        self.assertFalse(package.exists())

    def test_legacy_import_collision_in_both_directions(self):
        source = (installer.ROOT / "klipper" / "box.py").read_bytes()
        original = b"VALUE = 1\n" + b" " * (len(source) - len(b"VALUE = 1\n"))
        self.box.write_bytes(original)
        # Model the target's sibling-cache loader in a fresh process; installed
        # sources and caches are genuinely imported instead of merely named.
        code = """
import importlib._bootstrap_external as external
from pathlib import Path
import sys
root = Path(sys.argv[1])
original = external.cache_from_source
def sibling(path, *args, **kwargs):
    if root / 'extras' in Path(path).parents:
        return path + 'c'
    return original(path, *args, **kwargs)
external.cache_from_source = sibling
sys.path.insert(0, str(root))
import extras.box
print(getattr(extras.box, 'VALUE', 'extension'))
"""
        def load():
            return subprocess.check_output([sys.executable, "-c", code, str(self.root)], text=True).strip()
        self.assertEqual(load(), "1")
        cache = self.extras / "box.pyc"
        original_cache = cache.read_bytes()
        original_stat = cache.stat()
        source_stat = self.box.stat()
        installer.install(self.extras, self.backup)
        os.utime(str(self.box), ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        self.assertEqual(load(), "extension")
        self.assertTrue(list((self.extras / "cfs").glob("*.pyc")))
        installer.restore(self.extras, self.backup)
        self.assertEqual(cache.read_bytes(), original_cache)
        self.assertEqual(cache.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertEqual(load(), "1")

    def test_generated_legacy_caches_removed_when_original_had_none(self):
        installer.install(self.extras, self.backup)
        entry_cache = self.extras / "box.pyc"
        entry_cache.write_bytes(b"generated entry cache")
        for name in installer.FILES:
            (self.extras / "cfs" / Path(name).with_suffix(".pyc")).write_bytes(b"generated package cache")
        unrelated = self.extras / "other.pyc"
        unrelated.write_bytes(b"user cache")
        installer.restore(self.extras, self.backup)
        self.assertFalse(entry_cache.exists())
        self.assertFalse((self.extras / "cfs").exists())
        self.assertFalse((self.extras / "__pycache__").exists())
        self.assertEqual(unrelated.read_bytes(), b"user cache")

    def test_tampered_saved_legacy_cache_blocks_restore_before_changes(self):
        entry_cache = self.extras / "box.pyc"
        entry_cache.write_bytes(b"original legacy cache")
        installer.install(self.extras, self.backup)
        entry_cache.write_bytes(b"generated entry cache")
        package_cache = self.extras / "cfs" / "box.pyc"
        package_cache.write_bytes(b"generated package cache")
        installed = self.box.read_bytes()
        (self.backup / "box-caches" / "box.pyc").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "backup cache"):
            installer.restore(self.extras, self.backup)
        self.assertEqual(self.box.read_bytes(), installed)
        self.assertEqual(entry_cache.read_bytes(), b"generated entry cache")
        self.assertEqual(package_cache.read_bytes(), b"generated package cache")

    def test_unknown_legacy_package_cache_blocks_restore_before_changes(self):
        installer.install(self.extras, self.backup)
        own_cache = self.extras / "cfs" / "box.pyc"
        own_cache.write_bytes(b"owned cache")
        other_cache = self.extras / "cfs" / "other.pyc"
        other_cache.write_bytes(b"user file")
        installed = self.box.read_bytes()
        with self.assertRaisesRegex(ValueError, "directory has changed"):
            installer.restore(self.extras, self.backup)
        self.assertEqual(self.box.read_bytes(), installed)
        self.assertEqual(own_cache.read_bytes(), b"owned cache")
        self.assertEqual(other_cache.read_bytes(), b"user file")

    def test_legacy_cache_directories_rejected_before_changes(self):
        legacy = self.extras / "box.pyc"
        legacy.mkdir()
        with self.assertRaisesRegex(ValueError, "legacy box bytecode cache"):
            installer.prepare(self.extras, self.backup)
        self.assertFalse(self.backup.exists())
        legacy.rmdir()
        installer.install(self.extras, self.backup)
        (self.extras / "cfs" / "state.pyc").mkdir()
        installed = self.box.read_bytes()
        with self.assertRaisesRegex(ValueError, "legacy cfs bytecode cache"):
            installer.restore(self.extras, self.backup)
        self.assertEqual(self.box.read_bytes(), installed)

    def test_version_two_modern_backup_remains_compatible(self):
        cache = self.extras / "__pycache__"
        cache.mkdir()
        original_cache = cache / "box.cpython-38.pyc"
        original_cache.write_bytes(b"version two modern cache")
        installer.install(self.extras, self.backup)
        path = self.backup / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["version"] = 2
        path.write_text(json.dumps(manifest))
        installer.restore(self.extras, self.backup)
        self.assertEqual(self.box.read_bytes(), self.original)
        self.assertEqual(original_cache.read_bytes(), b"version two modern cache")

    def test_version_two_backup_rejects_unrecorded_legacy_entry(self):
        installer.install(self.extras, self.backup)
        path = self.backup / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["version"] = 2
        path.write_text(json.dumps(manifest))
        legacy = self.extras / "box.pyc"
        legacy.write_bytes(b"unrecorded entry cache")
        installed = self.box.read_bytes()
        with self.assertRaisesRegex(ValueError, "version 2 backup"):
            installer.restore(self.extras, self.backup)
        self.assertEqual(self.box.read_bytes(), installed)
        self.assertEqual(legacy.read_bytes(), b"unrecorded entry cache")

    def test_version_two_manifest_cannot_claim_a_legacy_cache_backup(self):
        (self.extras / "box.pyc").write_bytes(b"original legacy cache")
        installer.install(self.extras, self.backup)
        path = self.backup / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["version"] = 2
        path.write_text(json.dumps(manifest))
        installed = self.box.read_bytes()
        with self.assertRaisesRegex(ValueError, "backup cache manifest"):
            installer.restore(self.extras, self.backup)
        self.assertEqual(self.box.read_bytes(), installed)

    def test_legacy_cache_symlinks_rejected_before_changes(self):
        legacy = self.extras / "box.pyc"
        try:
            legacy.symlink_to(self.box)
        except OSError as error:
            self.skipTest("creating symlinks unavailable: " + str(error))
        with self.assertRaises(ValueError):
            installer.prepare(self.extras, self.backup)
        legacy.unlink()
        installer.install(self.extras, self.backup)
        (self.extras / "cfs" / "box.pyc").symlink_to(self.box)
        installed = self.box.read_bytes()
        with self.assertRaises(ValueError):
            installer.restore(self.extras, self.backup)
        self.assertEqual(self.box.read_bytes(), installed)

    def test_tampered_saved_cache_blocks_restore(self):
        cache = self.extras / "__pycache__"
        cache.mkdir()
        (cache / "box.cpython-38.pyc").write_bytes(b"original cache")
        installer.install(self.extras, self.backup)
        (self.backup / "box-caches" / "box.cpython-38.pyc").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "backup cache"):
            installer.restore(self.extras, self.backup)

    def test_unexpected_cache_entry_blocks_restore(self):
        installer.install(self.extras, self.backup)
        cache = self.extras / "cfs" / "__pycache__"
        cache.mkdir()
        (cache / "other.cpython-38.pyc").write_bytes(b"user data")
        with self.assertRaises(ValueError):
            installer.restore(self.extras, self.backup)

    def test_linked_target_rejected(self):
        link = self.root / "linked-extras"
        try:
            link.symlink_to(self.extras, target_is_directory=True)
        except OSError as error:
            self.skipTest("creating symlinks unavailable: " + str(error))
        with self.assertRaises(ValueError):
            installer.prepare(link, self.backup)

    def test_reparse_point_rejected(self):
        real_lstat = Path.lstat
        def reparse(path):
            if path == self.extras:
                return mock.Mock(st_mode=0o40755, st_file_attributes=0x400)
            return real_lstat(path)
        with mock.patch.object(Path, "lstat", reparse):
            with self.assertRaisesRegex(ValueError, "reparse"):
                installer.prepare(self.extras, self.backup)

    def test_linked_cache_entry_rejected(self):
        installer.install(self.extras, self.backup)
        cache = self.extras / "cfs" / "__pycache__"
        cache.mkdir()
        try:
            (cache / "box.cpython-38.pyc").symlink_to(self.box)
        except OSError as error:
            self.skipTest("creating symlinks unavailable: " + str(error))
        with self.assertRaises(ValueError):
            installer.restore(self.extras, self.backup)

    def test_linked_entry_cache_rejected(self):
        cache = self.extras / "__pycache__"
        cache.mkdir()
        try:
            (cache / "box.cpython-38.pyc").symlink_to(self.box)
        except OSError as error:
            self.skipTest("creating symlinks unavailable: " + str(error))
        with self.assertRaises(ValueError):
            installer.prepare(self.extras, self.backup)

    def test_allowlist_must_match_sources(self):
        with mock.patch.object(installer, "FILES", installer.FILES[:-1]):
            with self.assertRaisesRegex(ValueError, "allowlist"):
                installer.sources()

    def test_tampering_and_extra_files_block_restore(self):
        installer.install(self.extras, self.backup)
        installed = self.box.read_bytes()
        extra = self.extras / "cfs" / "other"
        extra.write_bytes(b"user data")
        with self.assertRaises(ValueError):
            installer.restore(self.extras, self.backup)
        extra.unlink()
        (self.extras / "cfs" / "box.py").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            installer.restore(self.extras, self.backup)
        self.assertEqual(self.box.read_bytes(), installed)

    def test_backup_tampering_blocks_restore(self):
        installer.install(self.extras, self.backup)
        (self.backup / "box.py.original").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            installer.restore(self.extras, self.backup)

    def test_collisions_and_nested_backup_rejected(self):
        for name in ("box.so", "box.cpython-38-x86_64-linux-gnu.so", "box.pyd", "box.cp38-win_amd64.pyd"):
            with self.subTest(name=name):
                native = self.extras / name
                native.write_bytes(b"stock")
                with self.assertRaisesRegex(ValueError, "shadows"):
                    installer.install(self.extras, self.backup)
                self.assertEqual(native.read_bytes(), b"stock")
                self.assertEqual(self.box.read_bytes(), self.original)
                self.assertFalse(self.backup.exists())
                native.unlink()
        (self.extras / "cfs").mkdir()
        with self.assertRaises(ValueError):
            installer.prepare(self.extras, self.backup)
        (self.extras / "cfs").rmdir()
        with self.assertRaises(ValueError):
            installer.prepare(self.extras, self.extras / "backup")
        self.backup.mkdir()
        with self.assertRaises(ValueError):
            installer.prepare(self.extras, self.backup)

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):
            installer.prepare(self.extras / ".." / "extras", self.backup)


if __name__ == "__main__":
    unittest.main()
