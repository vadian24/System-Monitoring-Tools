"""Install the Windows monitor for the current account and create shortcuts."""
import argparse
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from tempfile import mkdtemp
import venv


FILES = ("monitor.py", "dashboard.py", "network.py", "updater.py", "version.json", "windows_platform.py", "system-monitor.ico", "README.txt")
TITLE = "System Monitor for Windows"
SHORTCUT_SCRIPT = """
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$folder = $shell.SpecialFolders.Item('Programs')
$startup = $shell.SpecialFolders.Item('Startup')
if (-not $folder -or -not $startup) { throw 'Windows shortcut folders were not available.' }
function New-MonitorShortcut($path, $collector) {
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $env:MONITOR_PYTHONW
    $shortcut.Arguments = '"' + $env:MONITOR_MAIN + '"'
    if ($collector) { $shortcut.Arguments += ' --collect' }
    $shortcut.WorkingDirectory = $env:MONITOR_APP
    $shortcut.IconLocation = (Join-Path $env:MONITOR_APP 'system-monitor.ico') + ',0'
    $shortcut.Description = 'Local hardware, service, network, and event monitor'
    $shortcut.Save()
}
New-MonitorShortcut (Join-Path $folder 'System Monitor for Windows.lnk') $false
$collectorLink = Join-Path $startup 'System Monitor for Windows Collector.lnk'
if ($env:MONITOR_BOOT -eq '1') {
    New-MonitorShortcut $collectorLink $true
} elseif (Test-Path -LiteralPath $collectorLink) {
    Remove-Item -LiteralPath $collectorLink
}
"""


def apply_files():
    import updater

    source = Path(__file__).resolve().parent
    root = (Path(os.environ["LOCALAPPDATA"]) / "SystemMonitorWindows").resolve()
    app = root / "App"
    state = root / "History"
    upgrading = (app / "monitor.py").is_file()
    app.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    with updater.exclusive_lock(state / "update.lock") as locked:
        if not locked:
            raise RuntimeError("Another update is already running.")
        stage = Path(mkdtemp(prefix=".system-monitor-setup-", dir=root))
        try:
            next_app = stage / "next"
            next_app.mkdir()
            for name in FILES:
                shutil.copyfile(source / name, next_app / name)
            updater.install_staged(stage, app, state, upgrading, setup=True)
        except Exception:
            if not (stage / "previous").is_dir():
                shutil.rmtree(stage, ignore_errors=True)
            raise
        else:
            shutil.rmtree(stage, ignore_errors=True)


def install(startup):
    source = Path(__file__).resolve().parent
    root = Path(os.environ["LOCALAPPDATA"]) / "SystemMonitorWindows"
    app = root / "App"
    env_path = root / "Python"
    interpreter = env_path / "Scripts" / "python.exe"
    windowed = interpreter.with_name("pythonw.exe")
    for name in (*FILES, "requirements.txt"):
        if not (source / name).is_file():
            raise FileNotFoundError(f"Extract the entire ZIP first. Missing {name}.")
    root.mkdir(parents=True, exist_ok=True)
    if not interpreter.is_file():
        print("Creating the monitor's private Python environment…", flush=True)
        venv.EnvBuilder(with_pip=True).create(env_path)
    print("Installing dashboard dependencies…", flush=True)
    subprocess.run([str(interpreter), "-m", "pip", "install", "--disable-pip-version-check",
                    "--only-binary=:all:", "-r", str(source / "requirements.txt")], check=True)
    subprocess.run([str(interpreter), str(source / "install_windows.py"), "--apply-files"], check=True)
    env = {**os.environ, "MONITOR_PYTHONW": str(windowed), "MONITOR_MAIN": str(app / "monitor.py"),
           "MONITOR_APP": str(app), "MONITOR_BOOT": "1" if startup else "0"}
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", SHORTCUT_SCRIPT],
                   check=True, env=env)
    subprocess.Popen([str(windowed), str(app / "monitor.py")], cwd=app,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
    print(f"Installed. Open {TITLE} from the Start menu.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-startup", action="store_true", help="Leave the collector out of the Windows Startup folder.")
    parser.add_argument("--apply-files", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.exit(1, "Run this installer on Windows.\n")
    if not (sys.version_info >= (3, 12) and struct.calcsize("P") == 8):
        parser.exit(1, "Install 64-bit Python 3.12 or later and rerun the installer.\n")
    try:
        if args.apply_files:
            apply_files()
        else:
            install(not args.no_startup)
    except (OSError, RuntimeError, subprocess.SubprocessError, KeyError) as error:
        parser.exit(1, f"Installation could not finish: {error}\n")


if __name__ == "__main__":
    main()
