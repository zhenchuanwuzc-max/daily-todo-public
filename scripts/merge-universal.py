#!/usr/bin/env python3
"""Merge independently built py2app bundles, rejecting unexplained differences.

Only .pyc headers/source filenames and ZIP container metadata may differ. Python
code and all other resources must agree. Native files are merged from the named
runner's slice, then every Mach-O is checked. The CLI signs the result afterwards.
Run with the same Python minor version used to build the bundles.
"""
import argparse
import importlib.util
import io
import marshal
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import types
import zipfile


ARCHITECTURES = {"arm64", "x86_64"}
MACHO_MAGICS = {bytes.fromhex(value) for value in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe",
    "cafebabe", "bebafeca", "cafebabf", "bfbafeca",
)}


def is_macho(data):
    return data[:4] in MACHO_MAGICS


def architectures(path):
    return set(subprocess.check_output(["lipo", "-archs", str(path)], text=True).split())


def normalized_code(code):
    if not isinstance(code, types.CodeType):
        raise ValueError("A .pyc must contain a Python code object")
    return code.replace(co_filename="", co_consts=tuple(
        normalized_code(value) if isinstance(value, types.CodeType) else value
        for value in code.co_consts
    ))


def read_pyc(data, name):
    if len(data) < 16 or data[:4] != importlib.util.MAGIC_NUMBER:
        raise ValueError(f"Python bytecode version mismatch: {name}; use the build's Python version")
    try:
        return normalized_code(marshal.loads(data[16:]))
    except (EOFError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid Python bytecode: {name}") from exc


def zip_members(data, name):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate ZIP entries: {name}")
        entries = {member: archive.read(member) for member in names}
    for member, content in entries.items():
        if is_macho(content):
            # py2app loads native modules from lib-dynload, never from a ZIP.
            # Refuse an unexpected layout instead of silently missing a library.
            raise ValueError(f"Native code inside ZIP must be packaged outside it: {name}/{member}")
    return entries


def compare_resources(arm, intel, name):
    if name.endswith(".zip"):
        a, b = zip_members(arm, name), zip_members(intel, name)
        if a.keys() != b.keys():
            raise ValueError(f"ZIP entries differ: {name}: {sorted(a.keys() ^ b.keys())}")
        for member in a:
            compare_resources(a[member], b[member], f"{name}/{member}")
    elif arm != intel:
        if not name.endswith(".pyc") or read_pyc(arm, name) != read_pyc(intel, name):
            raise ValueError(f"Resource differs between architectures: {name}")


def bundle_entries(root):
    return {path.relative_to(root): path for path in root.rglob("*")
            if "_CodeSignature" not in path.relative_to(root).parts}


def checked_symlink(path, root):
    target = os.readlink(path)
    if (Path(target).is_absolute() or not path.exists()
            or not path.resolve().is_relative_to(root.resolve())):
        raise ValueError(f"Unsafe or broken symlink: {path}")
    return target


def merge_native(arm, intel, output):
    with tempfile.TemporaryDirectory(prefix="daily-todo-lipo-") as temporary:
        slices = []
        for source, architecture in ((arm, "arm64"), (intel, "x86_64")):
            actual = architectures(source)
            if architecture not in actual:
                raise ValueError(f"Missing {architecture} slice: {source} ({sorted(actual)})")
            thin = Path(temporary) / architecture
            if len(actual) == 1:
                shutil.copyfile(source, thin)
            else:
                subprocess.run(["lipo", str(source), "-thin", architecture, "-output", str(thin)], check=True)
            slices.append(str(thin))
        subprocess.run(["lipo", "-create", *slices, "-output", str(output)], check=True)
        shutil.copymode(arm, output)


def merge_bundles(arm64, x86_64, output):
    arm64, x86_64, output = map(Path, (arm64, x86_64, output))
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Output already exists: {output}")
    if not arm64.is_dir() or not x86_64.is_dir():
        raise ValueError("Both input bundles must exist")
    a, b = bundle_entries(arm64), bundle_entries(x86_64)
    if a.keys() != b.keys():
        raise ValueError(f"Bundle paths differ: {sorted(str(p) for p in a.keys() ^ b.keys())}")
    shutil.copytree(arm64, output, symlinks=True,
                    ignore=shutil.ignore_patterns("_CodeSignature"))
    try:
        for name, source in a.items():
            other, destination = b[name], output / name
            if source.is_symlink() or other.is_symlink():
                if (not source.is_symlink() or not other.is_symlink()
                        or checked_symlink(source, arm64) != checked_symlink(other, x86_64)):
                    raise ValueError(f"Bundle symlinks differ: {name}")
            elif source.is_dir() and other.is_dir():
                continue
            elif not source.is_file() or not other.is_file():
                raise ValueError(f"Bundle file types differ: {name}")
            else:
                left, right = source.read_bytes(), other.read_bytes()
                if is_macho(left) or is_macho(right):
                    if not is_macho(left) or not is_macho(right):
                        raise ValueError(f"Native file types differ: {name}")
                    merge_native(source, other, destination)
                else:
                    compare_resources(left, right, str(name))
    except BaseException:
        shutil.rmtree(output)
        raise


def verify_architectures(app, expected):
    count = 0
    for path in Path(app).rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as file:
            native = is_macho(file.read(4))
        if native:
            actual = architectures(path)
            if not set(expected).issubset(actual):
                raise ValueError(f"Missing native architecture: {path}: {sorted(actual)}; expected {sorted(expected)}")
            count += 1
        elif path.suffix == ".zip":
            zip_members(path.read_bytes(), str(path))
    if not count:
        raise ValueError(f"No native runtime found: {app}")
    return count


def sign_bundle(app):
    # Sign leaf binaries before the enclosing frameworks/app resource seals.
    for path in Path(app).rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as file:
                native = is_macho(file.read(4))
            if native:
                subprocess.run(["codesign", "--force", "--sign", "-", str(path)], check=True)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", "--all-architectures", str(app)], check=True)


def verify_payload(app, source, version):
    app, source = Path(app), Path(source)
    resources = app / "Contents/Resources"
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if (info.get("CFBundleIdentifier") != "com.ocean.dailytodo"
            or info.get("CFBundleVersion") != version
            or info.get("CFBundleShortVersionString") != version
            or (resources / "VERSION").read_text().strip() != version):
        raise ValueError("Bundle identity/version does not match the release tag")
    for name in ("VERSION", "index.html", "desktop_app.py", "server.py", "runtime_upgrade.py",
                 "app_update.py", "sync/json-merge.py", "sync/sync-helper.py", "sync/sync.sh"):
        embedded = resources / name
        if not embedded.is_file() or embedded.read_bytes() != (source / name).read_bytes():
            raise ValueError(f"Bundled source resource is missing or stale: {name}")
    archives = list((resources / "lib").glob("python*.zip"))
    if len(archives) != 1:
        raise ValueError("Expected exactly one packaged Python bytecode archive")
    members = zip_members(archives[0].read_bytes(), str(archives[0]))
    for module in ("server", "runtime_upgrade", "app_update"):
        name = module + ".pyc"
        if name not in members:
            raise ValueError(f"Missing packaged Python module: {name}")
        # Check what Python will import, not just a source copy beside the ZIP.
        compiled = normalized_code(compile((source / (module + ".py")).read_bytes(),
                                           module + ".py", "exec", dont_inherit=True))
        if read_pyc(members[name], name) != compiled:
            raise ValueError(f"Packaged Python bytecode is stale: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    merge = commands.add_parser("merge", help="Merge, verify native slices, and sign a bundle")
    merge.add_argument("--arm64", required=True, type=Path)
    merge.add_argument("--x86_64", required=True, type=Path)
    merge.add_argument("--output", required=True, type=Path)
    verify = commands.add_parser("verify", help="Verify source/bytecode, version, native slices, and signature")
    verify.add_argument("app", type=Path)
    verify.add_argument("--source", required=True, type=Path)
    verify.add_argument("--version", required=True)
    verify.add_argument("--arch", action="append", required=True, choices=sorted(ARCHITECTURES))
    args = parser.parse_args()
    if args.command == "merge":
        merge_bundles(args.arm64, args.x86_64, args.output)
        count = verify_architectures(args.output, ARCHITECTURES)
        sign_bundle(args.output)
        print(f"Universal bundle verified and ad-hoc signed: {count} Mach-O files: {args.output}")
    else:
        verify_payload(args.app, args.source, args.version)
        count = verify_architectures(args.app, set(args.arch))
        subprocess.run(["codesign", "--verify", "--deep", "--strict", "--all-architectures", str(args.app)], check=True)
        print(f"Bundle payload, version, signature and {count} Mach-O files verified: {args.app}")


if __name__ == "__main__":
    main()
