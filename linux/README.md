# Nobara System Monitor

A local desktop monitor for Nobara KDE. The dashboard refreshes roughly every two seconds. The collector records events independently of the window, so closing or pausing the display does not interrupt an installed collector.

## Start now

Extract the ZIP and open a KDE terminal in the extracted **Nobara-System-Monitor** folder. Run:

```bash
python3 monitor.py
```

If Python reports a missing dependency, install the Nobara packages:

```bash
sudo dnf install python3-pyside6 python3-psutil
```

The portable GUI starts a detached collector on demand. Its lifetime across logout depends on the desktop session. For automatic startup at login and recovery after collector failure, install the user service and app launcher:

```bash
bash install.sh
```

Open **Nobara System Monitor** from the KDE application launcher. The installer uses your normal account and does not need sudo. If you tried the portable collector first, it may keep collecting until logout. The collector lock ensures only one instance writes to the same database. The installed service then takes over at the next login.

## What you can see

- **Overview** shows CPU, RAM, storage writes, I/O pressure, recent faults, and up to seven days of activity charts. GPU charts below System Activity show utilization, temperature, VRAM used (solid) and total (dashed), and power draw. GPU history starts once this version of the collector runs.
- **Processes** lists visible PIDs, user, CPU, memory, disk writes where the OS permits per-process I/O access, and process state. CPU can exceed 100% when multiple cores are in use.
- **Services** shows loaded systemd system and user services, sockets and timers, including inactive and failed units. The collector logs observed transitions into and out of failed state.
- **Hardware** shows CPU cores, PCI and USB devices, motherboard and BIOS data, storage and filesystem utilization, disk throughput and latency, network interfaces, and exported temperatures and fan speeds. NVIDIA utilization, VRAM, temperature, power and driver details come from `nvidia-smi` when available. Unsupported or inaccessible sensors show missing values. Device inventory is refreshed about every 45 seconds.
- **Events** includes journal warnings and errors, plus specific events such as GPU faults, USB issues, drive timeouts, PCIe hardware errors, sleep and wake, crashes, and resource pressure warnings. Search by message or source, filter by category or severity, select a row for details, or export all matching retained event groups to CSV.

## Dashboard updates

The dashboard checks GitHub on launch and every 30 minutes while it is open. Click **Update** when **Update Available** appears. The dashboard verifies the package against the GitHub manifest. It then updates the files, restarts the user service or portable collector as needed, and reopens. Existing history and network settings stay in the history directory. The update uses your current Python environment and runs without sudo.

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

The collector records sustained I/O stalls, CPU utilization and memory pressure, sustained temperatures above the thresholds provided by accessible sensors, and filesystems at or above 95% usage. Very short spikes can fall between samples. Some applications write only to private logs or their own files. Journal visibility depends on your account. The Overview shows whether the kernel journal and NVIDIA telemetry are accessible. The app does not change account permissions. SMART faults reported by `smartd` appear in Events if the service and journal are accessible. The collector does not run privileged SMART commands.

Telemetry remains local. The **Log folder** button opens the state folder, normally `~/.local/state/nobara-system-monitor`. History can contain remote IPs, process names, private paths and device information. Peer history retains up to seven days and 25,000 peer records.

To manage the installed background service:

```bash
systemctl --user status nobara-system-monitor.service
systemctl --user restart nobara-system-monitor.service
systemctl --user disable --now nobara-system-monitor.service
```

Use the same installer to update the app. History is kept in the state directory.
