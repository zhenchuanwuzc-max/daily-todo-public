#!/usr/bin/env python3
"""Install one verified universal release, or publish after local acceptance.

Uses this checkout's transaction runner so 1.1.4 can migrate even though that
older bundle did not contain app_update.py. No old app/data tree is deleted.
"""
import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app_update
import runtime_upgrade

REPO = "zhenchuanwuzc-max/daily-todo-public"


class InstallerSystem(app_update.NativeSystem):
    def __init__(self):
        self.runtime = runtime_upgrade.NativeSystem()

    def listener_pids(self, port):
        return self.runtime.listener_pids(port)

    def job(self, label):
        return self.runtime.job(label)

    def health(self, port):
        return self.runtime.health(port) or {}

    def command(self, pid):
        return subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True).strip()


def validate_release(release, version):
    if release.get("tagName") != "v" + version or release.get("isPrerelease") is not False:
        raise ValueError("Expected this exact stable version, not a prerelease")
    filename = f"daily-todo-{version}-macos-universal.zip"
    if sorted(asset["name"] for asset in release.get("assets", [])) != sorted([filename, "SHA256SUMS.txt"]):
        raise ValueError("Release must contain exactly one universal ZIP and SHA256SUMS.txt")
    return filename


def verify_checksum(directory, filename):
    directory = Path(directory)
    lines = (directory / "SHA256SUMS.txt").read_text().strip().splitlines()
    if len(lines) != 1:
        raise ValueError("Unexpected SHA-256 manifest entries")
    fields = lines[0].split(maxsplit=1)
    if (len(fields) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0])
            or fields[1].lstrip("*") != filename):
        raise ValueError("SHA-256 manifest does not name the requested ZIP")
    digest = hashlib.sha256()
    with (directory / filename).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != fields[0].lower():
        raise ValueError("Downloaded ZIP failed SHA-256 verification")
    return digest.hexdigest()


def configured_data(home):
    home = Path(home)
    agents = home / "Library/LaunchAgents"
    server = runtime_upgrade.read_configuration(agents / "com.ocean.daily-todo-server.plist")
    sync = runtime_upgrade.read_configuration(agents / "com.ocean.daily-todo-sync.plist")
    default = home / "daily-todo-data"
    if not default.is_dir():
        default = home / "daily-todo"
    return runtime_upgrade.select_data_dir(server, sync, default, home)


def identify_server(app, data, home, port, system):
    listeners = system.listener_pids(port)
    if not listeners:
        return {"server_pid": 0, "server_source": ""}
    if len(listeners) != 1:
        raise RuntimeError("Unknown listener; refusing to stop any process")
    health = system.health(port)
    if (health.get("ok") is not True or health.get("app") != "daily-todo"
            or health.get("preview") is not False
            or Path(health.get("data_file", "")).resolve() != (data / "todos.json").resolve()):
        raise RuntimeError("Current service identity/data directory does not match the configured app")
    pid = next(iter(listeners))
    command = system.command(pid)
    binary = str(app / "Contents/MacOS/每日待办")
    if command in (binary, binary + " --server-only"):
        return {"server_pid": pid, "server_source": ""}
    # launchd may report a resolved Python executable differently from ps. Its
    # registered argument vector and PID must still match the owned plist.
    config = runtime_upgrade.read_configuration(home / "Library/LaunchAgents/com.ocean.daily-todo-server.plist")
    job = system.job(runtime_upgrade.SERVER_LABEL)
    args = (config or {}).get("ProgramArguments", [])
    if not (job and job.get("pid") == pid and job.get("arguments") == args):
        args = shlex.split(command)
    if (runtime_upgrade.trusted_server_arguments(args, app, home) and len(args) == 2
            and Path(args[1]).name == "server.py" and command.endswith(" " + args[1])):
        return {"server_pid": pid, "server_source": args[1]}
    raise RuntimeError("Unknown listener command; refusing to stop any process")


def check_publish_health(health, version, data):
    plan = {"version": version, "data_dir": str(data)}
    if not isinstance(health, dict) or not app_update.matches_health(health, plan):
        raise RuntimeError("Publish requires the exact stable version running with the configured data directory")


def release_info(version):
    output = subprocess.check_output([
        "gh", "release", "view", "v" + version, "--repo", REPO,
        "--json", "tagName,assets,isDraft,isPrerelease",
    ], text=True)
    return json.loads(output)


def support_directory(home):
    return home / "Library/Application Support/daily-todo"


def install_release(version):
    home = Path.home().resolve()
    app = home / "Applications/每日待办.app"
    if not runtime_upgrade.installed_app(app, home) or app.is_symlink():
        raise RuntimeError("Expected an existing app at ~/Applications/每日待办.app")
    data = configured_data(home)
    system = InstallerSystem()
    identity = identify_server(app, data, home, 8766, system)
    release = release_info(version)
    filename = validate_release(release, version)
    root = support_directory(home) / "update-backups" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    download = root / "download"
    download.mkdir(parents=True)
    subprocess.run(["gh", "release", "download", "v" + version, "--repo", REPO,
                    "--pattern", filename, "--pattern", "SHA256SUMS.txt", "--dir", str(download)], check=True)
    digest = verify_checksum(download, filename)
    with zipfile.ZipFile(download / filename) as archive:
        for name in archive.namelist():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or path.parts[0] not in ("每日待办.app", "__MACOSX"):
                raise ValueError("Unsafe path in release ZIP")
    extracted = root / "extracted"
    subprocess.run(["ditto", "-x", "-k", str(download / filename), str(extracted)], check=True)
    staged = extracted / "每日待办.app"
    app_update.validate_bundle(staged, version)
    for path in staged.rglob("*"):
        if path.is_symlink() and (not path.exists() or not path.resolve().is_relative_to(staged.resolve())):
            raise ValueError("External or broken symlink in release bundle")
    spec = importlib.util.spec_from_file_location("universal_package", ROOT / "scripts/merge-universal.py")
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    package.verify_architectures(staged, {"arm64", "x86_64"})
    system.verify_signature(staged)
    # The old package need not contain an updater. Run the current checkout's
    # transaction after saving the complete installed bundle for rollback.
    backup = root / "每日待办.app"
    shutil.copytree(app, backup, symlinks=True)
    identity = identify_server(app, data, home, 8766, system)
    plan = {"app": str(app), "staged_app": str(staged), "backup_app": str(backup),
            "data_dir": str(data), "home": str(home), "port": 8766, "version": version, **identity}
    (root / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    receipt = app_update.apply_update(plan, system=system)
    check_publish_health(system.health(8766), version, data)
    receipt.update(zip_sha256=digest, asset=filename, app=str(app), data_dir=str(data))
    receipts = support_directory(home) / "release-verification"
    receipts.mkdir(parents=True, exist_ok=True)
    temporary = receipts / (version + ".tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    temporary.replace(receipts / (version + ".json"))
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


def publish_release(version):
    home = Path.home().resolve()
    app = home / "Applications/每日待办.app"
    data = configured_data(home)
    system = InstallerSystem()
    release = release_info(version)
    filename = validate_release(release, version)
    if release.get("isDraft") is not True:
        raise RuntimeError("Only an accepted draft can be published by this command")
    receipt = json.loads((support_directory(home) / "release-verification" / (version + ".json")).read_text())
    asset = next(asset for asset in release["assets"] if asset["name"] == filename)
    if (receipt.get("status") != "installed" or receipt.get("version") != version
            or receipt.get("app") != str(app) or receipt.get("data_dir") != str(data)
            or asset.get("digest") != "sha256:" + receipt.get("zip_sha256", "")):
        raise RuntimeError("This release ZIP has not passed local installation acceptance")
    app_update.validate_bundle(app, version)
    system.verify_signature(app)
    check_publish_health(system.health(8766), version, data)
    identity = identify_server(app, data, home, 8766, system)
    if not identity["server_pid"] or identity["server_source"]:
        raise RuntimeError("Publish requires the installed app's server, not a source checkout")
    subprocess.run(["gh", "release", "edit", "v" + version, "--repo", REPO,
                    "--draft=false", "--prerelease=false", "--latest"], check=True)
    published = release_info(version)
    validate_release(published, version)
    if published.get("isDraft"):
        raise RuntimeError("Release is still a draft")
    latest = json.loads(subprocess.check_output(["gh", "api", f"repos/{REPO}/releases/latest"], text=True))
    if latest.get("tag_name") != "v" + version:
        raise RuntimeError("Release published but latest pointer did not match; inspect GitHub before retrying")
    print(f"Published v{version} as the stable latest release after local acceptance")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "publish"))
    parser.add_argument("version")
    args = parser.parse_args()
    version = args.version.removeprefix("v")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        parser.error("version must be an explicit x.y.z")
    if args.action == "install":
        install_release(version)
    else:
        publish_release(version)


if __name__ == "__main__":
    main()
