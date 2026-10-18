"""Release packaging contracts; native fixtures are compiled, never executed."""
import importlib.util
import marshal
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "merge-universal.py"


def pyc(source, filename="source.py", timestamp=1):
    return (importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, timestamp, len(source))
            + marshal.dumps(compile(source, filename, "exec")))


class PackageFixture(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "Universal merge/verification script is missing")
        spec = importlib.util.spec_from_file_location("universal_package", SCRIPT)
        self.package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.package)
        self.tmp = tempfile.TemporaryDirectory(prefix="daily-todo-universal-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.arm = self.root / "arm64.app"
        self.intel = self.root / "x86_64.app"
        self.out = self.root / "merged.app"
        for app in (self.arm, self.intel):
            (app / "Contents/Resources").mkdir(parents=True)
            (app / "Contents/Resources/index.html").write_text("same page")

    def write_pair(self, name, arm, intel=None):
        for app, value in ((self.arm, arm), (self.intel, arm if intel is None else intel)):
            path = app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)

    def merge(self):
        self.package.merge_bundles(self.arm, self.intel, self.out)


class UniversalPackageTests(PackageFixture):
    def test_copies_identical_resources_and_internal_symlinks_without_old_signature(self):
        self.write_pair("Contents/_CodeSignature/CodeResources", b"old arm signature", b"old intel signature")
        for app in (self.arm, self.intel):
            (app / "Contents/Resources/alias").symlink_to("index.html")
        self.merge()
        self.assertEqual((self.out / "Contents/Resources/index.html").read_text(), "same page")
        self.assertTrue((self.out / "Contents/Resources/alias").is_symlink())
        self.assertFalse((self.out / "Contents/_CodeSignature").exists())

    def test_rejects_different_html_and_removes_incomplete_output(self):
        self.write_pair("Contents/Resources/index.html", b"new page", b"old page")
        with self.assertRaisesRegex(ValueError, "index.html"):
            self.merge()
        self.assertFalse(self.out.exists())

    def test_rejects_missing_files_in_either_architecture(self):
        (self.arm / "Contents/Resources/only-arm64.so").write_bytes(b"missing runtime")
        with self.assertRaisesRegex(ValueError, "only-arm64.so"):
            self.merge()

    def test_rejects_external_symlinks(self):
        for app in (self.arm, self.intel):
            (app / "Contents/Resources/outside").symlink_to("../../../outside")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.merge()

    def test_accepts_pyc_timestamp_and_source_path_differences_in_nested_code(self):
        source = "def function():\n    return [x * 2 for x in range(4)]\n"
        self.write_pair("Contents/Resources/module.pyc", pyc(source, "/arm/source.py", 5),
                        pyc(source, "/intel/source.py", 20))
        self.merge()
        self.assertEqual((self.out / "Contents/Resources/module.pyc").read_bytes(),
                         (self.arm / "Contents/Resources/module.pyc").read_bytes())

    def test_rejects_real_pyc_code_changes(self):
        self.write_pair("Contents/Resources/module.pyc", pyc("def f(): return 1"),
                        pyc("def f(): return 2"))
        with self.assertRaisesRegex(ValueError, "module.pyc"):
            self.merge()

    def test_compares_python_zip_members_ignoring_container_metadata(self):
        for app, timestamp in ((self.arm, 10), (self.intel, 20)):
            with zipfile.ZipFile(app / "Contents/Resources/python.zip", "w") as archive:
                archive.writestr("module.pyc", pyc("value = 1", timestamp=timestamp))
                archive.writestr("resource.txt", b"same data")
                archive.comment = str(timestamp).encode()
        self.merge()
        with zipfile.ZipFile(self.out / "Contents/Resources/python.zip") as archive:
            self.assertEqual(archive.read("resource.txt"), b"same data")

    def test_rejects_python_zip_code_changes(self):
        for app, value in ((self.arm, 1), (self.intel, 2)):
            with zipfile.ZipFile(app / "Contents/Resources/python.zip", "w") as archive:
                archive.writestr("module.pyc", pyc(f"value = {value}"))
        with self.assertRaisesRegex(ValueError, "module.pyc"):
            self.merge()

    def test_rejects_native_code_hidden_in_zip(self):
        for app in (self.arm, self.intel):
            with zipfile.ZipFile(app / "Contents/Resources/python.zip", "w") as archive:
                archive.writestr("hidden.so", b"\xcf\xfa\xed\xfe" + b"\0" * 20)
        with self.assertRaisesRegex(ValueError, "hidden.so"):
            self.merge()

    def test_does_not_overwrite_an_existing_output(self):
        self.out.mkdir()
        (self.out / "keep.txt").write_text("user file")
        with self.assertRaises(FileExistsError):
            self.merge()
        self.assertEqual((self.out / "keep.txt").read_text(), "user file")


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("clang") and shutil.which("lipo"),
                     "Real Mach-O merge checks require Apple's developer tools")
class NativeUniversalPackageTests(PackageFixture):
    def compile_fixture(self, app, arch, name="native.dylib"):
        path = app / "Contents/Resources" / name
        subprocess.run(["clang", "-arch", arch, "-dynamiclib", "-x", "c", "-", "-o", str(path)],
                       input="int answer(void) { return 42; }", text=True, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return path

    def test_merges_real_native_libraries_and_verifies_both_architectures(self):
        self.compile_fixture(self.arm, "arm64")
        self.compile_fixture(self.intel, "x86_64")
        self.merge()
        binary = self.out / "Contents/Resources/native.dylib"
        self.assertEqual(set(subprocess.check_output(["lipo", "-archs", str(binary)], text=True).split()),
                         {"arm64", "x86_64"})
        self.assertEqual(self.package.verify_architectures(self.out, {"arm64", "x86_64"}), 1)

    def test_rejects_input_with_the_wrong_native_architecture(self):
        self.compile_fixture(self.arm, "arm64")
        self.compile_fixture(self.intel, "arm64")
        with self.assertRaisesRegex(ValueError, "x86_64"):
            self.merge()

    def test_verifier_rejects_one_thin_library_among_universal_files(self):
        self.compile_fixture(self.arm, "arm64")
        self.compile_fixture(self.intel, "x86_64")
        self.merge()
        self.compile_fixture(self.out, "arm64", "forgotten.dylib")
        with self.assertRaisesRegex(ValueError, "forgotten.dylib"):
            self.package.verify_architectures(self.out, {"arm64", "x86_64"})


class PayloadVerificationTests(PackageFixture):
    def setUp(self):
        super().setUp()
        self.source = self.root / "source"
        self.source.mkdir()
        self.resources = self.arm / "Contents/Resources"
        self.files = {
            "VERSION": "1.2.0\n", "index.html": "<html>new page</html>",
            "desktop_app.py": "import server\nimport runtime_upgrade\nimport app_update\n",
            "server.py": "VERSION = '1.2.0'\nRELEASE_CHANNEL = 'stable'\n",
            "runtime_upgrade.py": "def upgrade(): return True\n",
            "app_update.py": "def install(): return True\n",
            "sync/json-merge.py": "def merge(): return True\n",
            "sync/sync-helper.py": "def sync(): return True\n",
            "sync/sync.sh": "#!/bin/bash\nexit 0\n",
        }
        for name, text in self.files.items():
            for directory in (self.source, self.resources):
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        (self.arm / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.ocean.dailytodo",
            "CFBundleShortVersionString": "1.2.0", "CFBundleVersion": "1.2.0",
        }))
        (self.resources / "lib").mkdir()
        self.write_archive()

    def write_archive(self, stale=False):
        with zipfile.ZipFile(self.resources / "lib/python-test.zip", "w") as archive:
            for name in ("server.py", "runtime_upgrade.py", "app_update.py"):
                source = self.files[name]
                if stale and name == "server.py":
                    source = "VERSION = '1.0.22'\nRELEASE_CHANNEL = 'stable'\n"
                archive.writestr(name + "c", pyc(source))

    def verify(self):
        self.package.verify_payload(self.arm, self.source, "1.2.0")

    def test_accepts_matching_version_resources_and_compiled_modules(self):
        self.verify()

    def test_rejects_missing_bundled_sync_helper(self):
        (self.resources / "sync/sync-helper.py").unlink()
        with self.assertRaisesRegex(ValueError, "sync-helper.py"):
            self.verify()

    def test_rejects_stale_bytecode_even_when_source_resource_is_current(self):
        self.write_archive(stale=True)
        with self.assertRaisesRegex(ValueError, "server.pyc"):
            self.verify()

    def test_rejects_stale_info_plist_version(self):
        info = self.arm / "Contents/Info.plist"
        metadata = plistlib.loads(info.read_bytes())
        metadata["CFBundleShortVersionString"] = "1.1.4"
        info.write_bytes(plistlib.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "version"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
