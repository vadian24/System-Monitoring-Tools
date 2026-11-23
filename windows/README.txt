SYSTEM MONITOR FOR WINDOWS

Windows 10/11 desktop edition

Privacy
-------
System Monitor stores and processes your metrics, event logs, observed
network connections, and settings on your device. Your monitoring data stays
on your device unless you choose to share it, such as by sharing an exported
log.

The dashboard contacts GitHub to check for and download updates. Network
Traffic sends test pings to your router and to an internet probe target,
which defaults to 1.1.1.1. You can change or disable the internet probe in
Network Traffic. Setup may also download software dependencies.

Install
-------
1. Extract the entire ZIP to a folder.
2. Install 64-bit Python 3.12 or later if needed:
   https://www.python.org/downloads/windows/
3. Double-click InstallWindowsMonitor.cmd. It downloads Python dependencies
   from PyPI (internet required), installs the monitor for your account, and
   opens the dashboard.
4. To open it again, search Start for Windows System Monitor.

When upgrading, close the old dashboard before running setup. Setup restarts
the background collector and keeps existing history.

Administrator rights are not required. The installer adds a background
collector to your Windows Startup folder. It runs at sign-in and keeps
recording after you close the dashboard. To install without a sign-in
collector, run InstallWindowsMonitor.cmd --no-startup from Command Prompt.
To disable collector startup later, open shell:startup from Windows Run
and remove Windows System Monitor Collector.

Drive health
------------
Overview shows each connected drive with its name, type, health check,
temperature, and sector or media status. Select a card for its last check,
readings, and suggested steps for any warning.
Warning and critical cards turn yellow and red. Check now requests a
background refresh. Drive health warnings appear under Storage in Events.
Windows storage drivers, USB bridges and permissions determine which health
and reliability readings are available.

Each drive shows its read and write rates, busy percentage, write latency,
and a read/write history graph. Per-drive history begins with version 1.3.0.
The selected Overview time range applies to this graph. Used and available
space come from volumes that Windows can associate with one drive.

Overview colors flag sustained conditions. CPU uses 80% and 95%, while RAM
and VRAM use 85% and 95%. Drive busy turns yellow at 80%, or red at 95% when
write latency is also elevated (20 ms for NVMe and SSD, 50 ms for other
drives). GPU load turns yellow at 85% and red at 95% while thermal throttling
is reported. Temperature uses reported device thresholds. Hover a colored
card for its rule.

CPU, I/O, and frame telemetry
-----------------------------
Overview shows CPU clocks, package power, Vcore, processor queue length,
and recent process CPU time. Hardware adds per-core sensor readings,
logical CPU utilization, and recent plus cumulative CPU time by process.
CPU power, voltage, and core sensors depend on a Libre Hardware Monitor
or Open Hardware Monitor WMI provider and the sensors it exposes. The
system clock estimate is used when per-core clock readings are absent.

Hardware lists throughput, operations per second, average read and write
latency, busy time and average queue depth per drive where available.
Linux pressure and TRIM counts are unavailable from these Windows counters.

Live FPS and dropped frames
---------------------------
Under Overview > GPU Activity, Live FPS and Dropped frames stay visible.
A dash means no reading is available. Drops cover the last 60 seconds.
Install the Intel PresentMon console app separately, then choose
Live FPS setup > Select PresentMon console EXE and point to that console
executable. A game that emits supported presentation events will begin
reporting once it is presenting frames.

For Twitch and YouTube, use the Browser video tab in Live FPS setup.
It opens the folder containing the bundled add-on ZIP and walks through
loading and pairing the add-on in a Chromium browser. Only the local
monitor receives the site label and frame counters.


Dashboard updates
-----------------
The dashboard checks GitHub when it opens. Use Check for updates in the
header to check again. When Update Available appears, click Update.
The dashboard verifies the download, updates the per-user app files,
restarts the collector as needed, and reopens. This uses your existing
Python environment and preserves history, settings, and shortcuts.

Pin to the taskbar
------------------
Open Windows System Monitor, right-click its running taskbar icon, and
choose Pin to taskbar. If you pinned the app before version 1.1.5 and the
old pin opens Python, unpin that shortcut and pin the running dashboard again.

What the dashboard shows
------------------------
- Overview: live CPU utilization and temperature, RAM, per-drive activity,
  and GPU activity for
  detected display adapters, including virtual adapters. Utilization and
  dedicated memory depend on the driver. NVIDIA temperature and power are
  available when nvidia-smi exposes them. Network charts appear below GPU activity.
  CPU temperature requires CPU readings from an active Libre Hardware Monitor
  or Open Hardware Monitor WMI provider. Temperature history starts in 1.3.5.
- Processes: CPU, memory, and total process write I/O where permitted. On
  Windows this process counter can include non-disk I/O.
- Services: Windows Service Control Manager entries and running, stopped,
  and paused states. Service failures are available in Events when logged.
- Hardware: CPU/core activity, disks, filesystems, network interfaces,
  motherboard, BIOS, display adapters and USB controllers, where available.
  GPU utilization and memory are shown where the Windows graphics driver
  exposes the counters. Virtual adapters may expose only their identity.
- Events: recent critical, error and warning entries from the System and
  Application Event Logs, plus selected sleep and wake events and alerts from
  the monitor. Search/filter by message or source and export matching CSV.
- Network Traffic: the same network charts as Overview, observed TCP/connected
  UDP peers, process attribution where available, and a country watchlist.
  Very short connections may fall between samples. Windows per-peer byte
  counts are not collected. Network history includes up to seven days.

Network charts
--------------
Receive and Send are rates across non-loopback interfaces. TCP Retransmits
counts TCP segments retransmitted on Windows, including retransmissions
that occurred before a timer expired. Packet Drops counts packets discarded
by local interfaces. The charts show counts per history interval, and
interface packet drops and link-down transitions also appear under Network
in Events. VPN and virtual adapters can count traffic at multiple layers.

Latency and Probe Loss show separate router and internet readings. About
every 15 seconds the collector sends three ICMP requests to each target.
The router is the IPv4 default gateway. The internet target defaults to
1.1.1.1 and follows normal system routing. Set another IP address or clear
the field under Network Traffic to disable the internet probe. Latency is
the mean round-trip time of replies, and Probe Loss is the percentage of
probes that received no reply. Probe errors are shown as gaps. Some networks
filter ICMP even when other traffic works.

Country lookup
--------------
The KR/KP watchlist can be edited in the Network Traffic tab. Country
watchlist matches require a GeoLite2 Country database. Download it from
MaxMind and place the extracted GeoLite2-Country.mmdb file into the folder
opened by Log folder. The collector loads it automatically. Connections
remain visible before adding the database and country appears as Unknown.
The app performs lookups locally. VPNs, hosting and out-of-date databases
can affect country estimates.
https://support.maxmind.com/knowledge-base/articles/download-and-update-maxmind-databases

Data
----
Local history is stored under %LOCALAPPDATA%\SystemMonitorWindows\History.
The monitor keeps seven days of telemetry and up to 30 days or 25,000 event
groups, whichever limit is reached first. Access to processes, devices or
Event Logs depends on what Windows exposes to the signed-in account.
