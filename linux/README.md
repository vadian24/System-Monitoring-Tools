# Linux System Monitor

A local desktop monitor for Linux desktops. The dashboard refreshes roughly every two seconds. The collector records events independently of the window, so closing or pausing the display does not interrupt an installed collector.

## Privacy

System Monitor stores and processes your metrics, event logs, observed network connections, and settings on your device. Your monitoring data stays on your device unless you choose to share it, such as by sharing an exported log.

The dashboard contacts GitHub to check for and download updates. Network Traffic sends test pings to your router and to an internet probe target, which defaults to 1.1.1.1. You can change or disable the internet probe in Network Traffic. Setup may also download software dependencies.

## Start now

Extract the ZIP and open a terminal in the extracted **Nobara-System-Monitor** folder. Run:

```bash
python3 monitor.py
```

The installer uses system Python with PySide6 and psutil when available.
Otherwise, it creates a private Python environment and downloads those
packages from PyPI. The fallback requires Python's venv module, a Qt-compatible
desktop runtime, and internet access. On Debian and Ubuntu, the venv package
is `python3-venv`. On Fedora and Nobara, the native dependencies are
`python3-pyside6` and `python3-psutil`.

The background service and journal features require a systemd user session
and access to the relevant journal entries. You can launch the dashboard
directly with `python3 monitor.py` if the dependencies are already installed.

Open **Linux System Monitor** from your application launcher. The installer uses your normal account and does not need sudo. If you tried the portable collector first, it may keep collecting until logout. The collector lock ensures only one instance writes to the same database. The installed service then takes over at the next login.

## What you can see

- **Overview** shows CPU utilization and temperature, RAM, system-wide Some and Full I/O pressure, recent faults, and up to seven days of activity charts. GPU charts below System Activity show utilization, temperature, VRAM used (solid) and total (dashed), and power draw. GPU history starts once this version of the collector runs.
- **Processes** lists visible PIDs, user, CPU, memory, disk writes where the OS permits per-process I/O access, and process state. CPU can exceed 100% when multiple cores are in use.
- **Services** shows loaded systemd system and user services, sockets and timers, including inactive and failed units. The collector logs observed transitions into and out of failed state.
- **Hardware** shows CPU cores, PCI and USB devices, motherboard and BIOS data, storage and filesystem utilization, disk throughput and latency, network interfaces, and exported temperatures and fan speeds. GPU readings come from Linux DRM, driver sensors and, for NVIDIA, `nvidia-smi` when available. A display adapter can appear even when its driver does not expose live counters or sensors. Device inventory is refreshed about every 45 seconds.
- **Events** includes journal warnings and errors, plus specific events such as GPU faults, USB issues, drive timeouts, PCIe hardware errors, sleep and wake, crashes, and resource pressure warnings. Search by message or source, filter by category or severity, select a row for details, or export all matching retained event groups to CSV.

The CPU temperature card prefers a package or die sensor. Its colors use thermal limits reported by that sensor, when available. Temperature history starts with version 1.3.5.

## CPU, I/O, and frame telemetry

Overview adds clock speeds, CPU package power, Vcore, load averages, runnable task count, and the process with the most CPU time over the last 60 seconds. Hardware shows per-core readings and recent plus lifetime CPU time by process. Clock speeds come from CPU frequency reporting. Power comes from readable package energy counters, and temperature headroom requires a reported thermal limit. Vcore requires an explicitly labeled core-voltage sensor.

I/O pressure charts show Full (solid) and Some (dashed). Hardware shows per-drive read and write throughput, completed operations per second, average latency, busy time, average outstanding I/O, and completed TRIM discard requests per second.

### Live FPS and dropped frames

Overview shows two compact cards when a supported source is reporting: **Live FPS** and **Dropped frames** over the last 60 seconds. The cards remain visible when the source keeps reporting after a window switch. MangoHud supplies FPS and does not report dropped frames to the monitor.

- **Games:** Install MangoHud if needed. Choose **Live FPS setup → Copy MangoHud launch option**, paste it into that game's Steam Launch Options, then restart the game. Outside Steam, use the same launcher path followed by the game command in place of `%command%`. The monitor reads the MangoHud log from that game session.
- **Twitch and YouTube:** Open **Overview → Live FPS setup → Browser video**. The guide shows the bundled add-on ZIP in the monitor's `add-ons` folder and walks through extraction, loading, and pairing in a Chromium browser. The add-on can report while its video tab or browser window is in the background. The pairing code works across app restarts until you select **Disable video FPS**. See the [browser add-on guide](../browser-extension/README.md) for more details.


## Drive health

Overview detects connected drives and lists one card per drive with its name, type, health check, temperature, and sector or media status. Select a card to see the last check, readings, and suggested steps for any warning. Cards turn yellow or red for warnings or critical conditions. **Check now** requests a background refresh. New health warnings are recorded in Events under Storage.

Linux reads SMART through UDisks when available and can fall back to smartmontools for directly accessible drives. Supported sleeping HDDs are left asleep. USB flash drives and some enclosures may expose no SMART data. Permissions and controller support affect available readings.

Each drive also shows its read and write rates, busy percentage, write latency, and a two-line read/write history graph. Used and available space come from readable mounted filesystems. Shared filesystems spanning drives are excluded to avoid double-counting. Per-drive history begins with version 1.3.0. The selected Overview time range applies to this graph.

Overview colors indicate sustained conditions. CPU turns yellow at 80% and red at 95%. RAM and VRAM use 85% and 95%. Full I/O pressure uses 5% and 50%. Drive busy turns yellow at 80%, or red at 95% when write latency is also elevated (20 ms for NVMe and SSD, 50 ms for other drives). GPU load turns yellow at 85% and red at 95% while thermal throttling is reported. Temperature uses the device's reported thresholds. Hover a colored card for its rule.

## Dashboard updates

The dashboard checks GitHub when it opens. Use **Check for updates** in the header to check again. Click **Update** when **Update Available** appears. The dashboard verifies the package against the GitHub manifest. It then updates the files, restarts the user service or portable collector as needed, and reopens. Existing history and network settings stay in the history directory. The update uses your current Python environment and runs without sudo.

## Network Traffic

- Interface charts show receive and send rates across non-loopback interfaces. The charts average observations over each history interval. VPNs and virtual interfaces can count traffic at more than one layer.
- Overview and Network Traffic graph Receive, Send, Timeouts, and Packet Drops. Timeouts count Linux TCP retransmission timer expirations, and Packet Drops count drops reported by local interfaces. Both show counts per history interval. Each tab summarizes drops and physical Ethernet/Wi-Fi carrier losses for its selected range. These passive events appear under Network in Events.
- Both tabs graph Latency and Probe Loss separately for the router and internet target. About every 15 seconds the collector sends three ICMP echo requests to each address with `ping`. Router readings use the IPv4 default gateway. The internet address defaults to `1.1.1.1`, follows normal system routing, and can be changed or cleared in Network Traffic. Latency is the mean round-trip time of replies. Probe Loss is the percentage of requests without a reply. Missing routes or probe errors show a gap. A target can filter ICMP even while other traffic works.
- **Live peers** shows remote sockets in the latest sample. **Recent peers** shows peers observed in the last seven days, with process, protocol, country estimate, and first/last seen times. Sort the byte columns to focus on larger sampled TCP transfers. A dash means no byte counter was available. Search by IP, port, process, country, or protocol.
- **Watchlist only** highlights country matches. The initial list is `KR, KP` (South Korea and North Korea). The list is editable and blank clears it. Matches are logged as informational Network events at most once per peer per day. **New in last 24h** finds peers first seen recently by this monitor. IP geolocation describes the visible peer address, and VPNs, CDNs, hosting, and stale country data can affect it.
- Country lookup uses `mmdblookup` and a local GeoLite2 Country database. If those are missing on Nobara, the relevant packages are `libmaxminddb` and `geolite2-country`. The tab shows the file date. To use a newer database, place `GeoLite2-Country.mmdb` in the folder opened by **Log folder**. No remote lookups are made.
- TCP totals are deltas between observed socket samples. A socket's earlier traffic, sockets that open and close between samples, unconnected UDP traffic, and processes outside the account's visibility can be missed. The per-peer view shows both inbound and outbound peers when visible. The interface totals include traffic that the socket view cannot attribute. This is socket monitoring and contains no packet payload capture.

## History and coverage

The collector imports up to seven days of readable journal history the first time it runs. It then follows the journal and resumes from its last recorded position when restarted. The app keeps seven days of sampled telemetry and up to 30 days or 25,000 event groups, whichever limit is reached first. Identical messages from the same source are grouped while they continue to arrive within two minutes of each other. That makes repeated warnings such as Razer command timeouts easier to count and inspect. Rates come from Linux counters. Missing or restricted readings are indicated in the GUI.

The collector records sustained I/O stalls, CPU utilization and memory pressure, sustained temperatures above the thresholds provided by accessible sensors, and filesystems at or above 95% usage. Very short spikes can fall between samples. Some applications write only to private logs or their own files. Journal visibility depends on your account. The Overview shows whether the kernel journal and GPU telemetry are accessible. The app does not change account permissions. SMART faults reported by `smartd` appear in Events if the service and journal are accessible. The collector does not run privileged SMART commands.

Telemetry remains local. The **Log folder** button opens the state folder, normally `~/.local/state/nobara-system-monitor`. History can contain remote IPs, process names, private paths and device information. Peer history retains up to seven days and 25,000 peer records.

To manage the installed background service:

```bash
systemctl --user status nobara-system-monitor.service
systemctl --user restart nobara-system-monitor.service
systemctl --user disable --now nobara-system-monitor.service
```

Use the same installer to update the app. History is kept in the state directory.
