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

install -d "$app_dir" "$config_dir/systemd/user" "$desktop_dir"
python=/usr/bin/python3
venv_python="$app_dir/venv/bin/python"
if ! "$python" -c 'import PySide6, psutil' 2>/dev/null; then
    if ! "$venv_python" -c 'import PySide6, psutil' 2>/dev/null; then
        if ! "$python" -m venv "$app_dir/venv" ||
           ! "$venv_python" -m pip install PySide6-Essentials psutil; then
            printf '%s\n' 'Install Python venv support and Qt runtime libraries for your distribution, then retry.' \
                'You can also install PySide6 and psutil through your distribution and rerun setup.'
            exit 1
        fi
    fi
    python=$venv_python
fi
install -m 644 "$source_dir/monitor.py" "$source_dir/dashboard.py" "$source_dir/network.py" "$source_dir/updater.py" "$source_dir/version.json" "$app_dir/"
install -m 644 "$source_dir/nobara-system-monitor.png" "$app_dir/"
install -m 644 "$source_dir/nobara-system-monitor.service" "$config_dir/systemd/user/"
if [[ "$python" == "$venv_python" ]]; then
    sed -i 's|^ExecStart=/usr/bin/python3 |ExecStart=%h/.local/share/nobara-system-monitor/venv/bin/python |' \
        "$config_dir/systemd/user/nobara-system-monitor.service"
fi
"$python" - "$app_dir" "$desktop_dir" "$python" <<'PY'
from pathlib import Path
import sys

application, destination, interpreter = map(Path, sys.argv[1:])

def quote(argument):
    value = str(argument)
    for character in ('\\', '"', '`', '$'):
        value = value.replace(character, '\\' + character)
    return '"' + value.replace('%', '%%').replace('\\', '\\\\') + '"'

icon = str(application / "nobara-system-monitor.png").replace("\\", "\\\\")
desktop = destination / "nobara-system-monitor.desktop"
desktop.write_text(
    '[Desktop Entry]\nType=Application\nName=Linux System Monitor\n'
    'Comment=Live telemetry and hardware and system events\n'
    f'Exec={quote(interpreter)} {quote(application / "monitor.py")}\n'
    f'Icon={icon}\nTerminal=false\nCategories=System;Monitor;\n',
    encoding='utf-8',
)
PY
if systemctl --user daemon-reload &&
   systemctl --user enable nobara-system-monitor.service &&
   systemctl --user restart nobara-system-monitor.service; then
    printf '%s\n' 'Open Linux System Monitor from your application launcher.'
else
    printf '%s\n' 'The files were installed, but the user service could not start.' \
        'Open Linux System Monitor to start the collector, or rerun setup from a desktop terminal with a systemd user session.'
    exit 1
fi
