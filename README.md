# System Monitoring Tools

Local desktop monitors for Windows and Linux. Both versions offer a live dashboard for CPU, memory, storage, GPU metrics for detected display adapters where supported, running processes, services, system events, and network activity. History is stored on the computer running the monitor.

| Platform | Download | Source and documentation |
| --- | --- | --- |
| Windows 10/11, x64 | [SystemMonitorSetup.exe](https://raw.githubusercontent.com/vadian24/System-Monitoring-Tools/main/downloads/SystemMonitorSetup.exe) | [Windows source](windows/) · [Windows guide](windows/README.txt) |
| Linux desktop | [Linux-System-Monitor.zip](https://raw.githubusercontent.com/vadian24/System-Monitoring-Tools/main/downloads/Linux-System-Monitor.zip) | [Linux source](linux/) · [Linux guide](linux/README.md) |

These links download the files directly. If the GitHub file view shows **Error Loading Page**, use the download links above or the ones in the [downloads folder](downloads/).

## Demo

This walkthrough shows the Linux dashboard, including live GPU charts, system events, and network activity.

[![Animated preview of the System Monitor walkthrough](docs/demo/system-monitor-preview.gif)](docs/demo/system-monitor-demo.mp4)

[Watch or download the full two-minute demo](docs/demo/system-monitor-demo.mp4)

## Windows

1. Download and run `SystemMonitorSetup.exe` on Windows. Internet access is required.
2. Setup reuses a compatible Python installation or downloads and verifies Python from python.org. It installs the dashboard for your account, creates a Start menu shortcut, and starts the background collector at sign-in.
3. To open the dashboard again, search Start for **Windows System Monitor**.

The installer is unsigned and may show a Windows SmartScreen warning. The earlier Windows monitor was tested in a Windows VM. When upgrading, close the old dashboard before setup. Setup restarts the background collector and preserves existing history. The [manual Windows package](https://raw.githubusercontent.com/vadian24/System-Monitoring-Tools/main/downloads/System-Monitor-Windows.zip) is also available for users who already have Python.

## Linux

1. Extract `Linux-System-Monitor.zip`.
2. Open a terminal in the extracted `Nobara-System-Monitor` folder and run `bash install.sh`.
3. Open **Linux System Monitor** from your desktop application menu. The installer creates a user service that records after you close the dashboard. To preview the dashboard from the extracted folder, run `python3 monitor.py`.

The [Linux guide](linux/README.md) lists dependencies and explains the user service.

## Dashboard updates

Install version 1.1.0 or later once to add the updater. Both dashboards check GitHub when they open. Use **Check for updates** in the header to check again. When **Update Available** appears, click **Update**. The dashboard verifies the download, replaces the per-user application files, restarts the collector as needed, and reopens. Monitoring history and settings stay in the local history folder.

If an older Windows dashboard says its running collector does not support automatic restart, close the dashboard and run the current Windows setup once. Future updates can then run from the dashboard.

## Network activity and log access

Both editions show Receive, Send, Packet Drops, and separate router and internet Latency and Probe Loss charts in Overview and Network Traffic. Linux graphs TCP retransmission timeouts. Windows graphs TCP segments retransmitted. Router probes use the IPv4 default gateway. You can change or disable the default internet probe (`1.1.1.1`) in Network Traffic.

Both editions show observed remote sockets and associated processes where the operating system provides access. The optional country watchlist uses a local GeoLite2 Country database. Country matches are leads for investigation, since VPNs and hosting services can affect the visible peer address. Windows shows interface traffic totals and peer history. Linux can also show per-peer TCP byte counts when available.

## Build the Windows installer

The [installer source](windows/installer/) includes the native launcher and PowerShell bootstrap. On a Linux build host with Python 3 and LLVM's `clang`, `llvm-dlltool`, `llvm-rc`, and `lld-link`, run:

```bash
bash windows/installer/build.sh
```

The output is `windows/installer/.build/SystemMonitorSetup.exe`. Set `LLVM_BIN` to the LLVM binaries directory if those commands are outside your `PATH`.

For a dashboard update, increase the version in each affected platform's `version.json`. Rebuild its download and publish the ZIP, its SHA-256, and the matching version in `downloads/update-manifest.json` together. For Windows, rebuild the setup executable as well. Publish the same Linux ZIP under both the public download name and the manifest filename so older dashboards can still update.

[SHA-256 checksums for downloads](downloads/SHA256SUMS)

