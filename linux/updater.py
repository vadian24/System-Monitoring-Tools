"""User-level GitHub updates for System Monitor."""
import argparse
from base64 import b64decode
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


BROWSER_ADDON_NAME = "System-Monitor-Video-FPS.zip"
# Embedded archive lets the previous updater deliver the add-on with its existing file set.
BROWSER_ADDON_ZIP = (
    b"UEsDBBQAAAAIAFpHd119ewmg6gIAAOIFAAAiAAAAU3lzdGVtLU1vbml0b3ItVmlkZW8tRlBTL1JFQURNRS5tZIVUXW4cRR"
    b"B+n1OUxENgNNsbO5FAPMZJJAujWPGCBAhpa7vLnsY9Xa2umllvDsABOCInQd0zu8YmwOOMqrq+v6ov4OYgSgN8z9ErZ/jR"
    b"O2J4f33TNJveC3BSzxEDXPSZBz8OgM6tOIJQdALBTwQp4GGH9r60AUYHCJlD8PEOXOaUyK1uMw4ElseocJt5AKxdpWSz92"
    b"p74Aw/8bgZdwRTxaD8HNuZeWW+KYUBlbJpmjMDl/FZVQe2ZxaCtv0wUZ487eHP3/+Aq4K0ALwhHVP99SbzXigv88qfD4ni"
    b"keAtB0e5bQ28e9CMVmE7T1otk1ZVq9X76xvzyadtAYxLFxx4BIsR7omSKcTqFwZh+MXxPgZGB17B+UxWw+HXL3vVJN+u1x"
    b"n35s5rP+5GoWw5KkU1lof1hM5jPH+9fgrDx7vVhjnIekAf18fXZf1faL8yzXkVr9pKHbxzd9TBm4wTdVBzMGFwvgMukmhP"
    b"sJvVeiFAD0pRPEeBhKVNxxyBI7TtW5oocKIMAztq267mQSiQVWjbq0J7jAntPbki7cXsVXl/Ua4wRh9LMrYDRn9LouY34b"
    b"g1zavP+l0R/pvbcnL7gtNhsTqhL7KBrRjNbHvBcGIGidOYOkgoOsMrtc/YXKMv+WheG/hI1dFS+M88K+7mRsWsp215jIXM"
    b"HXsfHe8F9r0P9HfJqyY+jiSQKXHWgv20dHW1xDTNhmFMDpUA40lksJwOXSFWI1xejbSHny+vwUflhbWX+uYj/dmM7pHrTL"
    b"Bti81PlbKYXVf+Rcj/o4KB74hSmVRKpJyExfWUSShPJE8BLUYVcj2dLg8OKSyVJX8v5GjrUZJ6ZyjLYlc5VHWgV4KAOwod"
    b"DIQyZnIlJLOtnztVi0CBLQZwKP2OMTtAhe3Z+dfmpXlpzrYGLnWZErkigh8+XkkH6jWQdLONhU7vRTkf6oKhnUc4UvRBDL"
    b"z1grtwvH4lu/551gseG44Lk8mSnyhX9PSQfKZFk8d0m+YvUEsDBBQAAAAIADJHd13oGbBtUwMAANEGAAAmAAAAU3lzdGVt"
    b"LU1vbml0b3ItVmlkZW8tRlBTL2JhY2tncm91bmQuanOdVF2P2zYQfPev2AhFIKE6yncJkFaGYqQFCjToJUHvXgrDBShxZb"
    b"GWSIFcWVV9/u8FqQ9fcn3qo8jZ4ezsjgqtLIGVhBYyOAcVUWvTJOn7nlEvqagYnYIUgkf/EcTwFWTQHXU5skI3DvSH7h67"
    b"HIPLZlV44rbmg1QHyEBhD/e8DaPNqkYCizUWhGL8ag2epO7sZrUqKqMbZMRzy7T6HRt9QsG4EL9JS6jQhMRzyN7P1ExgjY"
    b"TuNIqu9aZTJBtkWt2jtfyAX1FwO6gCQsubtsYYLCqBJnKs5xXAKN3qzhQI2WjObsQwbeRBqv1mgXk1E4NTvWVSuFtZQvhq"
    b"4nh6glefuiZHw6T9VREexjaiCAxSZ9RmtfAp3UMGLZpSm4arApnSvXMNoNQGwhG1kyIGg1xIddiDLmc3It/A+LpjuplBzL"
    b"kB7+Hter2OvvVOCs9/mWSPtmyZVLwgecKZ9D8c92WAtUVfuTT5i1SScGEqWxvB69fwwoQZIIxubfTtQxbJvRLDeZmUNzQG"
    b"103qvLo8lzBWz5a6hlYASQJfDJZooNRFZ1F4+pwXxxg0VWh6aRGOiC1QhctiwkkK1NBXskaQBAZbbciyZVAzWwY7xtgkec"
    b"9KqUQY7q7T8Ws1T2Hsg0210XWNTtLKvMb/xTbVerZFfjYL3LLdeg/bLYSzrRW34YyLYHutSWcZS81ccsTBhhFT+DeFETvx"
    b"usNn4ic9bm8n/AHp+sQShxn39HQtybIl/dGz0c1nkM1QnxEywzTl8eXzEYcLZMB7Lgmm7FvSxiW+1gWvvZTgiEPgdUxKkj"
    b"93/KZc3/y4P7+5u3yXMEJL4REHpy0IoudSYGIvkYoq9L+/NElu796xNVuz2/Ttm9t3PyR+XYJ4UgfQIFVapBB8+fzwGMTT"
    b"qUEhDRaUQoDGaLNcWHlQvE7hQ64NPfgPH1ndUXjnMjsDK+QCjU3hHHzoqNJG/sNJauV+vz8hN2gggO/hiEMMwc9aESq6eR"
    b"xadADetrUsPD75y2oVXGbaXIshhY8Pnz8xS0aqgyyH8DzGLb0u3BS/srXpi51ubQw+xy+u/Okl8k/NgS04FdXkVpLAw2AJ"
    b"G7jXSpI20PABcoSi1i5i2kxpJKyxQbcEE0BIy/MaBfNpd9z/AlBLAwQUAAAACAAyR3ddsTGKZiUBAADnAQAAJgAAAFN5c3"
    b"RlbS1Nb25pdG9yLVZpZGVvLUZQUy9tYW5pZmVzdC5qc29uXZHBasMwEETv/opljyHIcXso+AN6KxQSCqUEo8ibWImtFdLa"
    b"JoT8e5HtEtKr5u3uzOiWAWCnnT1SlGqgEC07LOF1nQSnO8IScHuNQh18sLPCAb5sTQzvn1ucqMcUFqpQm/m1pmiC9bIo24"
    b"ZHaO1AsButmAa0q+Gb+11/IPCtvh60uaSdk1AH9p5qOAbdUQTr4NmCmm94Cp2N6XjEEn4wCgd9ItxPasNRqv9II+LLPC9e"
    b"3tRGbVSRrxY63T8F7l2NJdwwUhisoWrkcKGQEjwAdY54n4a0WfLdsKaj7lupPPveJ95rG1QjXbuwhp2Qk2quZXJzywCm/s"
    b"U09PAXyzwfx1HJ1JSSIV/hGp6kK/fSH0gZ7v4SAOB53jGk/0km9xnAfZ/ds19QSwMEFAAAAAgAAUR3XTJ1tobyAQAAmQMA"
    b"ACIAAABTeXN0ZW0tTW9uaXRvci1WaWRlby1GUFMvcGFpci5odG1sfVNNb9swDL33V3AadqvjuBiywpF9GbDTBhQotrts0T"
    b"FXW9IkOh8L9t8HSWmyXnaSyPfIJz7a8p22PZ8cwsjz1N7JeMCkzK4RaERMoNLtHYCckRX0o/IBuRELD8WjuAFGzdiIPeHB"
    b"Wc8CemsYDTfiQJrHRuOeeixScA9kiElNRejVhE2V2zDxhO3zKTDO8M0aYuvhB2m08OXpWZYZj8zAp3wD6Kw+wRkGa7iG6q"
    b"M7QkgNioXuISgTioCehi0k5RoeNmt33MKs/I5MDdUmRr2drC9CP+KMNUy0Gxm08i9b+JNExuoiUQT6jTVUn2JVxsi4heEM"
    b"nT1GlMyuhs56jb7o7PGqW63XH26yG3eENVQPsY1TWqeqza1ptzBbA+dLQeHjk2p4vDHeB1a8hEghU4yYCdWqwjlTZHlxSZ"
    b"Z5gzJalewbq/+4PFaJ49rP1p2ARwSnyJPZQW81wuDtHBfgrdm1X2mPsQoC8uKiYkoDGXgrsJKlS20H6+e8ODmpDicYrG/E"
    b"C55E+/SPjCwTemFmi0lnIsSvtRFOhXCwXgtQC9vezm5CxkbYYRDg8ddCHvWlwcXOXBiWbibOerLMUPuWkgORJBcT5xft93"
    b"ReC5LDr9NIl6h5JQK8nfAata+jh96TYwi+j48nv/qZwJyOa8r7kWX+Ef8CUEsDBBQAAAAIAAFEd13ZzNfmTgEAAOwCAAAg"
    b"AAAAU3lzdGVtLU1vbml0b3ItVmlkZW8tRlBTL3BhaXIuanOtkj9PwzAQxfd8isNlcCTqonZCUVigGwMSYkIgGefSWEnsYl"
    b"8CUZXvjuxUhaGthMTkP3p393vPVtZ4Ak+SOg85FFZ1LRoSHx264QkbVGQdZ7NJwdIsUbGi1NgU5wpqHII6UZWzLQpP1skN"
    b"isYq2YgNEmdRIahCw/muxmFMIb+FXQKgS+A1DumeSxB+0Z01hIYgB/YotdNmA8oWCF72WAiWJWOaJadoSutalgpZFOseDT"
    b"1oT2jQcea791YTuwLpB6MA+zhioogHsXVxvcdSdg3xNEsApghqHCCfghC9bDoU5HQ7KYKDi8Xbi5yX1/Ob191qOV4uBKGn"
    b"aCyNA+CEv7UhdEAVwmo5V5V0UoWL7S/bwXBo4JA6Z8J+TADkp9QERxP3SPuQg/gXdBgYm/1X1rPOBNKjcatGq/qQNj88+B"
    b"lwh63tcf9b/sb+HEF+iL8BUEsDBBQAAAAIADJHd10UC2KtZQIAAKcFAAAhAAAAU3lzdGVtLU1vbml0b3ItVmlkZW8tRlBT"
    b"L3ZpZGVvLmpznVRLj9MwEL73V0x7qBxITbjsoUtWQkgrOKzEQ4LDag9uMmksHDv4Uaia/nfkR9IHi4TwyfP+5puxCcmgvI"
    b"PDDECghR2vUUEJ0glxm3S9xh1XzlypW26s0nso4fHJ6yoljQWNvdIWSjCs6wX63FWrVYdUO2l5h9SgrB/QGLZFEp0yWjFb"
    b"tSRBOWa3sxmAQftBWtQ7JsgJ5FinF2yP2henlNaqch1KS3841PsvKLCySr8VgixCQ4vsKYQC0IYLi5pwi51POfcX2jNnsI"
    b"blMsko6ygGaYv2neAo7WesrCEZFSi3toXlMiUFsPseVTO5f/VFPwq237Dq+yfHBLd7KMsSFo2TleVKLrIRkFHaEsJy2IQe"
    b"N7QKtb7x2rbwYpLfI9+2FlbArhzYhUP2WIRhAPAGyDyylCXmojIwkqVBkQOXrLJ8h2uw2qHnPrpeb4I/z2yCP9eb4I9G67"
    b"SM0vFsbgerLBOBoHvNOjRrCJocaq36HusLU9IdoUwD/xu7JOGORfye+RDUjdIdkxVSqX6SDF7B66IoTgSlLZqXZep3GGA+"
    b"dTkMERu8mTqnUTEMI7Rz26gahtNieCSrs3Av38FNcZrJSHTE8gzVh0uC8pD0+D/k+ww+4wh09Qf2cxab4PvAbEs79osUeW"
    b"Jjdc3GKib29JLnGk6zSUhp70xLDt4QezLHSwcoJ9f0WnvFpfWvI1xGElOpmyKF/yNl4+Inrpre5OkawKyn4hprVyEhxnV5"
    b"rBxeqHEdvExIQkQORZZPwzR8I3AN8+lPanldoxwdGlX5v2YNJzsz915J4pcQyTjmYVOz29kx87v9G1BLAQIUAxQAAAAIAF"
    b"pHd119ewmg6gIAAOIFAAAiAAAAAAAAAAAAAACkgQAAAABTeXN0ZW0tTW9uaXRvci1WaWRlby1GUFMvUkVBRE1FLm1kUEsB"
    b"AhQDFAAAAAgAMkd3XegZsG1TAwAA0QYAACYAAAAAAAAAAAAAAKSBKgMAAFN5c3RlbS1Nb25pdG9yLVZpZGVvLUZQUy9iYW"
    b"NrZ3JvdW5kLmpzUEsBAhQDFAAAAAgAMkd3XbEximYlAQAA5wEAACYAAAAAAAAAAAAAAKSBwQYAAFN5c3RlbS1Nb25pdG9y"
    b"LVZpZGVvLUZQUy9tYW5pZmVzdC5qc29uUEsBAhQDFAAAAAgAAUR3XTJ1tobyAQAAmQMAACIAAAAAAAAAAAAAAKSBKggAAF"
    b"N5c3RlbS1Nb25pdG9yLVZpZGVvLUZQUy9wYWlyLmh0bWxQSwECFAMUAAAACAABRHdd2czX5k4BAADsAgAAIAAAAAAAAAAA"
    b"AAAApIFcCgAAU3lzdGVtLU1vbml0b3ItVmlkZW8tRlBTL3BhaXIuanNQSwECFAMUAAAACAAyR3ddFAtirWUCAACnBQAAIQ"
    b"AAAAAAAAAAAAAApIHoCwAAU3lzdGVtLU1vbml0b3ItVmlkZW8tRlBTL3ZpZGVvLmpzUEsFBgAAAAAGAAYA5QEAAIwOAAAA"
    b"AA=="
)


def install_browser_addon(state):
    directory = state / "add-ons"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / BROWSER_ADDON_NAME
    data = b64decode(BROWSER_ADDON_ZIP)
    try:
        if destination.is_file() and destination.stat().st_size == len(data) and destination.read_bytes() == data:
            return destination
    except FileNotFoundError:
        pass
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
        pending = Path(output.name)
        try:
            output.write(data)
            output.close()
            pending.replace(destination)
        finally:
            pending.unlink(missing_ok=True)
    return destination


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
