from base64 import b64encode
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
root = Path(__file__).resolve().parent.parent
prefix = 'System-Monitor-Windows/'
build = root / 'installer' / '.build'
build.mkdir(exist_ok=True)
script = Path('bootstrap.ps1').read_text(encoding='utf-8')
encoded = b64encode(script.encode('utf-16-le')).decode('ascii')
assert len(encoded) < 30000
(build / 'bootstrap-command.bin').write_bytes((encoded+'\0').encode('utf-16-le'))
with ZipFile(build / 'monitor-payload.zip','w',ZIP_DEFLATED,compresslevel=9) as z:
    for name in ('monitor.py','dashboard.py','windows_platform.py','network.py','updater.py','version.json','system-monitor.ico','install_windows.py'):
        z.write(root/name, prefix+name)
    requirements = (root/'requirements.txt').read_text().replace('PySide6>=','PySide6-Essentials>=')
    z.writestr(prefix+'requirements.txt',requirements)
    readme = (root/'README.txt').read_text()
    start = readme.index('Install\n-------')
    end = readme.index('Drive health\n')
    readme = readme[:start] + '''Install
-------
Double-click SystemMonitorSetup.exe. If needed, setup downloads and
installs CPython for your account, then installs the dashboard and adds
a Start menu shortcut. Internet access is required. Setup adds a
background collector at sign-in. To disable collector startup, open
shell:startup from Windows Run and remove the Windows System Monitor
Collector shortcut.

When upgrading, close the old dashboard before setup. Setup restarts
the background collector and keeps existing history.

''' + readme[end:]
    z.writestr(prefix+'README.txt',readme)
