"""User-level GitHub updates for System Monitor."""
import argparse
from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from zipfile import ZipFile

import psutil

BASE_URL = "https://raw.githubusercontent.com/vadian24/System-Monitoring-Tools/main/downloads/"
WINDOWS = sys.platform == "win32"
PLATFORM = "windows" if WINDOWS else "linux"
PACKAGES = {"windows": ("System-Monitor-Windows", ("windows_platform.py", "system-monitor.ico", "README.txt")),
            "linux": ("Nobara-System-Monitor", ("nobara-system-monitor.png",))}
COMMON_FILES = ("monitor.py", "dashboard.py", "network.py", "updater.py", "version.json")
SERVICE = "nobara-system-monitor.service"
MAX_PACKAGE = 12 * 1024 * 1024


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path, value):
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(value), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def version_key(version):
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", version):
        raise ValueError("Invalid update version.")
    return tuple(map(int, version.split(".")))


def installed_version(app):
    data = read_json(app / "version.json")
    version = data.get("version") if isinstance(data, dict) else None
    version_key(version)
    return version


def refresh_desktop_name(app):
    if app != (Path.home() / ".local/share/nobara-system-monitor").resolve():
        return
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    desktop = data / "applications/nobara-system-monitor.desktop"
    if desktop.is_symlink():
        return
    pending = desktop.with_name(f".{desktop.name}.{os.getpid()}.tmp")
    try:
        original = desktop.read_text(encoding="utf-8")
        updated = original.replace("Name=Nobara System Monitor\n", "Name=Linux System Monitor\n", 1)
        if updated != original:
            pending.write_text(updated, encoding="utf-8")
            pending.chmod(desktop.stat().st_mode & 0o777)
            pending.replace(desktop)
    except (OSError, UnicodeError):
        pass
    finally:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass


def download(name, limit):
    request = Request(BASE_URL + name, headers={"User-Agent": "System-Monitor-Update"})
    with urlopen(request, timeout=12) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("GitHub update exceeded the download limit.")
    return data


def check_update(app):
    manifest = json.loads(download("update-manifest.json", 64 * 1024))
    if manifest["schema"] != 1:
        raise ValueError("Unsupported update manifest.")
    offer = manifest["platforms"][PLATFORM]
    if offer["file"] != PACKAGES[PLATFORM][0] + ".zip" or not re.fullmatch(r"[a-f0-9]{64}", offer["sha256"]):
        raise ValueError("Invalid update package details.")
    return offer if version_key(offer["version"]) > version_key(installed_version(app)) else None


def stage_update(app, offer):
    package = download(offer["file"], MAX_PACKAGE)
    if hashlib.sha256(package).hexdigest() != offer["sha256"]:
        raise ValueError("The update checksum did not match. Try the download again.")
    prefix, extra = PACKAGES[PLATFORM]
    stage = Path(tempfile.mkdtemp(prefix=".system-monitor-update-", dir=app.parent))
    try:
        next_app = stage / "next"
        next_app.mkdir()
        with ZipFile(BytesIO(package)) as archive:
            for name in (*COMMON_FILES, *extra):
                info = archive.getinfo(f"{prefix}/{name}")
                if info.file_size > MAX_PACKAGE:
                    raise ValueError(f"Update file is too large: {name}")
                data = archive.read(info)
                if name.endswith(".py"):
                    compile(data, name, "exec")
                (next_app / name).write_bytes(data)
        if installed_version(next_app) != offer["version"]:
            raise ValueError("The package version did not match the update notice.")
        shutil.copyfile(app / "updater.py", stage / "updater.py")
        return stage
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


@contextmanager
def exclusive_lock(path):
    with path.open("a+b") as lock:
        try:
            if WINDOWS:
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
        else:
            yield True


def identity():
    return {"pid": os.getpid(), "started": psutil.Process().create_time()}


def matching_process(token):
    try:
        process = psutil.Process(token["pid"])
        if process.create_time() == token["started"] and process.status() != psutil.STATUS_ZOMBIE:
            return process
    except (KeyError, TypeError, ValueError, psutil.NoSuchProcess):
        pass
    return None


def wait_for_exit(token, timeout):
    deadline = time.monotonic() + timeout
    while matching_process(token) is not None:
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for the monitor process to exit.")
        time.sleep(0.15)


@contextmanager
def collector_registration(state, app):
    token = {**identity(), "app": str(app)}
    path = state / "collector-process.json"
    write_json(path, token)
    try:
        yield token
    finally:
        path.unlink(missing_ok=True)


def stop_requested(state, token):
    return token is not None and read_json(state / "update-stop.json") == token


def spawn(args, cwd):
    options = {}
    if WINDOWS:
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **options)


def hand_off(stage, app, state, autostart):
    token = identity()
    try:
        spawn([sys.executable, str(stage / "updater.py"), str(app), str(state),
               str(token["pid"]), repr(token["started"]), str(int(autostart))], stage)
    except OSError:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def systemctl(action, *, check):
    return subprocess.run(["systemctl", "--user", action, SERVICE], capture_output=True, text=True,
                          timeout=25, check=check).returncode == 0


def managed_collector(app, state):
    app_dir = Path.home() / ".local/share/nobara-system-monitor"
    default_state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "nobara-system-monitor"
    if WINDOWS or app != app_dir.resolve() or state != default_state.resolve():
        return False
    try:
        return systemctl("is-active", check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False


def collector_running(state):
    with exclusive_lock(state / "collector.lock") as locked:
        return not locked


def stop_collector(app, state, managed):
    if managed:
        systemctl("stop", check=True)
        return
    if not collector_running(state):
        return
    token = read_json(state / "collector-process.json")
    if matching_process(token) is None:
        raise RuntimeError("The running collector does not support automatic restart. Sign out and back in, then retry.")
    if Path(token.get("app", "")).resolve() != app:
        raise RuntimeError("This history folder is in use by another installation. Update from that dashboard.")
    # Wait for the sampler to finish and close its SQLite connection.
    request = state / "update-stop.json"
    write_json(request, token)
    try:
        wait_for_exit(token, 40)
    finally:
        request.unlink(missing_ok=True)


def start_collector(app, state, managed):
    if managed:
        systemctl("start", check=True)
    else:
        spawn([sys.executable, str(app / "monitor.py"), "--collect", "--state-dir", str(state)], app)


def wait_for_sample(app, state, started):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        sample = read_json(state / "live.json")
        token = read_json(state / "collector-process.json")
        if (isinstance(sample, dict) and sample.get("ts", 0) > started and isinstance(token, dict)
                and token.get("app") == str(app) and matching_process(token) is not None):
            return
        time.sleep(0.3)
    raise RuntimeError("The updated collector did not produce a live sample.")


def install_staged(stage, app, state, autostart):
    next_app = stage / "next"
    if version_key(installed_version(next_app)) <= version_key(installed_version(app)):
        return
    names = [name for name in (*COMMON_FILES, *PACKAGES[PLATFORM][1]) if name != "version.json"]
    names.append("version.json")
    backup = stage / "previous"
    backup.mkdir()
    managed = managed_collector(app, state)
    was_running = managed or collector_running(state)
    replaced = []
    restart_attempted = False
    stopped = False
    try:
        stop_collector(app, state, managed)
        stopped = True
        with exclusive_lock(state / "collector.lock") as locked:
            if not locked:
                raise RuntimeError("The collector was started by another process. Try again.")
            for name in names:
                path = app / name
                if path.is_symlink():
                    raise ValueError(f"Update requires a regular file at {name}.")
                if path.exists():
                    shutil.copy2(path, backup / name)
            for name in names:
                (next_app / name).replace(app / name)
                replaced.append(name)
        if was_running or autostart:
            started = time.time()
            restart_attempted = True
            start_collector(app, state, managed)
            wait_for_sample(app, state, started)
    except Exception:
        if restart_attempted:
            stop_collector(app, state, managed)
        if replaced:
            with exclusive_lock(state / "collector.lock") as locked:
                if not locked:
                    raise RuntimeError(f"Couldn't roll back while the collector was running. Backup: {backup}")
                for name in reversed(replaced):
                    old = backup / name
                    if old.exists():
                        old.replace(app / name)
                    else:
                        (app / name).unlink(missing_ok=True)
        if stopped and was_running and not collector_running(state):
            start_collector(app, state, managed)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    parser.add_argument("state", type=Path)
    parser.add_argument("pid", type=int)
    parser.add_argument("started", type=float)
    parser.add_argument("autostart", type=int, choices=(0, 1))
    args = parser.parse_args()
    stage = Path(__file__).resolve().parent
    result = args.state / "update-result.json"
    clean = False
    with exclusive_lock(args.state / "update.lock") as locked:
        if not locked:
            return
        try:
            wait_for_exit({"pid": args.pid, "started": args.started}, 25)
        except TimeoutError:
            write_json(result, {"error": "The dashboard did not close. The update was not applied."})
            return
        try:
            install_staged(stage, args.app, args.state, bool(args.autostart))
            outcome = {"version": installed_version(args.app)}
            clean = True
        except Exception as error:
            outcome = {"error": str(error)[:500]}
        try:
            write_json(result, outcome)
        except OSError:
            pass
        command = [sys.executable, str(args.app / "monitor.py"), "--state-dir", str(args.state)]
        if not args.autostart:
            command.append("--no-start")
        spawn(command, args.app)
    if clean:
        os.chdir(args.state)
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    main()
