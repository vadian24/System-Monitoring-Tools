#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" == 0 ]]; then
    printf '%s\n' 'Run this installer from your normal desktop account.'
    exit 1
fi

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app_dir="$HOME/.local/share/nobara-system-monitor"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
desktop_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

if ! /usr/bin/python3 -c 'import PySide6, psutil' 2>/dev/null; then
    printf '%s\n' 'Install the Nobara packages, then run this installer again:' \
        '  sudo dnf install python3-pyside6 python3-psutil'
    exit 1
fi

install -d "$app_dir" "$config_dir/systemd/user" "$desktop_dir"
install -m 644 "$source_dir/monitor.py" "$source_dir/dashboard.py" "$source_dir/network.py" "$source_dir/updater.py" "$source_dir/version.json" "$app_dir/"
install -m 644 "$source_dir/nobara-system-monitor.png" "$app_dir/"
install -m 644 "$source_dir/nobara-system-monitor.service" "$config_dir/systemd/user/"
/usr/bin/python3 - "$app_dir" "$desktop_dir" <<'PY'
from pathlib import Path
import sys

application, destination = map(Path, sys.argv[1:])
path = str(application / "monitor.py")
for character in ('\\', '"', '`', '$'):
    path = path.replace(character, '\\' + character)
path = path.replace('%', '%%').replace('\\', '\\\\')
icon = str(application / "nobara-system-monitor.png").replace("\\", "\\\\")
desktop = destination / "nobara-system-monitor.desktop"
desktop.write_text(
    '[Desktop Entry]\nType=Application\nName=Nobara System Monitor\n'
    'Comment=Live telemetry and hardware and system events\n'
    f'Exec=/usr/bin/python3 "{path}"\n'
    f'Icon={icon}\nTerminal=false\nCategories=System;Monitor;\n',
    encoding='utf-8',
)
PY
if systemctl --user daemon-reload &&
   systemctl --user enable nobara-system-monitor.service &&
   systemctl --user restart nobara-system-monitor.service; then
    printf '%s\n' 'Open Nobara System Monitor from the KDE application launcher.'
else
    printf '%s\n' 'The files were installed, but the user service could not start.' \
        'Open Nobara System Monitor from KDE to start the portable collector, or run this installer from a KDE terminal.'
    exit 1
fi
