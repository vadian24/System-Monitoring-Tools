"""Windows inventory, Service Control Manager and Event Log adapters."""
from collections import Counter
import ctypes
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from uuid import UUID

import psutil

from network import probe_target


EVENT_LEVEL = {1: 2, 2: 3, 3: 4, 4: 6}
CHANNELS = ("System", "Application")
TASKBAR_APP_ID = "Vadian24.SystemMonitoringTools.WindowsMonitor"
EVENT_RULES = [
    ("Graphics", r"nvlddmkm|amdkmdag|igfx|display driver|^display\b|graphics"),
    ("Storage", r"stornvme|storahci|storport|\bdisk\b|ntfs|volmgr|bad block|\bI/O error"),
    ("Memory", r"memorydiagnostics|resource-exhaustion|out of memory"),
    ("Kernel", r"whea-logger|bugcheck|machine check|hardware error|kernel-general"),
    ("Power / Firmware", r"kernel-power|power-troubleshooter|firmware|\bacpi\b"),
    ("Input / USB", r"usb|driverframeworks|kernel-pnp"),
    ("Network", r"ndis|wlan|tcpip|network adapter|dhcp|dns-client|^dns"),
    ("Services", r"service control manager"),
    ("Application", r"application (?:error|hang)|windows error reporting|\.net runtime"),
]
EVENT_RULES = [(category, re.compile(pattern, re.I)) for category, pattern in EVENT_RULES]

INVENTORY_SCRIPT = r"""
$inventory = @{}
foreach ($class in @('Win32_ComputerSystem','Win32_BaseBoard','Win32_BIOS','Win32_Processor',
                      'Win32_VideoController','Win32_DiskDrive','Win32_USBController')) {
    try {
        $inventory[$class] = @(Get-CimInstance -ClassName $class -OperationTimeoutSec 4 -ErrorAction Stop |
            Select-Object Name,Caption,Manufacturer,Model,Product,SMBIOSBIOSVersion,DriverVersion,
                Index,Size,NumberOfCores,NumberOfLogicalProcessors,PNPDeviceID)
    } catch {
        $inventory[$class] = @()
    }
}
ConvertTo-Json -InputObject $inventory -Depth 4 -Compress
"""

GPU_COUNTERS_SCRIPT = r"""
$rows = @{}
foreach ($entry in @(
        @('engine', 'Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine'),
        @('memory', 'Win32_PerfFormattedData_GPUPerformanceCounters_GPUAdapterMemory'))) {
    try {
        $rows[$entry[0]] = @(Get-CimInstance -ClassName $entry[1] -OperationTimeoutSec 3 |
            Select-Object Name, UtilizationPercentage, DedicatedUsage)
    } catch {
        $rows[$entry[0]] = @()
    }
}
ConvertTo-Json -InputObject $rows -Depth 3 -Compress
"""

SHORTCUT_RENAME_SCRIPT = r"""
$shell = New-Object -ComObject WScript.Shell
$arguments = '"' + $env:MONITOR_MAIN + '"'
foreach ($location in @('Programs', 'Startup')) {
    $directory = $shell.SpecialFolders.Item($location)
    if (-not $directory) { continue }
    $suffix = if ($location -eq 'Startup') { ' Collector.lnk' } else { '.lnk' }
    $expected = if ($location -eq 'Startup') { $arguments + ' --collect' } else { $arguments }
    $old = Join-Path $directory ('System Monitor for Windows' + $suffix)
    if (-not (Test-Path -LiteralPath $old -PathType Leaf)) { continue }
    $shortcut = $shell.CreateShortcut($old)
    if ($shortcut.TargetPath -ne $env:MONITOR_PYTHONW -or $shortcut.Arguments -ne $expected) { continue }

    $new = Join-Path $directory ('Windows System Monitor' + $suffix)
    if (-not (Test-Path -LiteralPath $new)) {
        Move-Item -LiteralPath $old -Destination $new
    } else {
        $current = $shell.CreateShortcut($new)
        if ($current.TargetPath -eq $shortcut.TargetPath -and $current.Arguments -eq $expected) {
            Remove-Item -LiteralPath $old
        }
    }
}
'{}'
"""

EVENT_SCRIPT = r"""
$channel = $env:MONITOR_EVENT_CHANNEL
$cursor = [long]$env:MONITOR_EVENT_CURSOR
$lookback = 604800000
function Read-Events($params) {
    try {
        Get-WinEvent @params -ErrorAction Stop
    } catch {
        if ($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') { throw }
    }
}
$head = Read-Events @{LogName=$channel; MaxEvents=1}
$reset = ($head -and $head.RecordId -lt $cursor)
if ($reset) { $cursor = 0 }
$selected = '(Level=1 or Level=2 or Level=3 or ((EventID=1 or EventID=42 or EventID=107 or EventID=506 or EventID=507) and (Provider[@Name="Microsoft-Windows-Kernel-Power"] or Provider[@Name="Microsoft-Windows-Power-Troubleshooter"])))'
$xpath = "*[System[$selected and EventRecordID > $cursor and TimeCreated[timediff(@SystemTime) <= $lookback]]]"
$events = @(Read-Events @{LogName=$channel; FilterXPath=$xpath; Oldest=$true; MaxEvents=250})
$rows = @(
    foreach ($event in $events) {
        if (-not $event.TimeCreated) { continue }
        $message = ''
        try { $message = [string]$event.Message } catch {}
        if ($message.Length -gt 3800) { $message = $message.Substring(0,3800) }
        [pscustomobject]@{
            record = $event.RecordId
            timestamp = ([DateTimeOffset]$event.TimeCreated).ToUnixTimeMilliseconds()
            level = $event.Level
            id = $event.Id
            provider = [string]$event.ProviderName
            message = $message
        }
    }
)
ConvertTo-Json -InputObject @{ rows=$rows; reset=[bool]$reset } -Depth 5 -Compress
"""


PROBE_SCRIPT = r"""
function Measure-Target($kind, $target, $unavailable) {
    $row = @{kind=$kind; target=$target; latency=$null; loss=$null; status=$unavailable}
    if ($target) {
        $ping = [System.Net.NetworkInformation.Ping]::new()
        try {
            $address = [System.Net.IPAddress]::Parse($target)
            $times = @(for ($i=0; $i -lt 3; $i++) {
                $reply = $ping.Send($address, 900)
                if ($reply.Status -eq [System.Net.NetworkInformation.IPStatus]::Success) {
                    $reply.RoundtripTime
                }
                if ($i -lt 2) { Start-Sleep -Milliseconds 300 }
            })
            $row.loss = (3 - $times.Count) * 100.0 / 3
            if ($times.Count) {
                $row.latency = ($times | Measure-Object -Average).Average
                $row.status = ''
            } else {
                $row.status = 'No replies'
            }
        } catch {
            $row.status = 'Ping unavailable'
        } finally {
            $ping.Dispose()
        }
    }
    $row
}
$gateway = ''
$routerStatus = 'No IPv4 default gateway'
try {
    $metrics = @{}
    Get-NetIPInterface -AddressFamily IPv4 | ForEach-Object { $metrics[$_.InterfaceIndex] = $_.InterfaceMetric }
    $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -PolicyStore ActiveStore |
        Where-Object { $_.NextHop -ne '0.0.0.0' -and $metrics.ContainsKey($_.InterfaceIndex) } |
        Sort-Object -Property @{Expression={$_.RouteMetric + $metrics[$_.InterfaceIndex]}} | Select-Object -First 1
    if ($route) { $gateway = [string]$route.NextHop }
} catch {
    $routerStatus = 'Gateway lookup unavailable'
}
$rows = @((Measure-Target 'router' $gateway $routerStatus),
          (Measure-Target 'internet' ([string]$env:MONITOR_PROBE_TARGET) 'Disabled'))
ConvertTo-Json -InputObject $rows -Compress
"""


@lru_cache(maxsize=1)
def tcp_statistics_api():
    try:
        getter = ctypes.WinDLL("iphlpapi").GetTcpStatisticsEx
        getter.argtypes = (ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32)
        getter.restype = ctypes.c_uint32
        return getter
    except (AttributeError, OSError):
        return None


def tcp_retransmit_counts():
    getter = tcp_statistics_api()
    counts = {}
    if getter is not None:
        for family in (2, 23):  # Windows AF_INET and AF_INET6
            statistics = (ctypes.c_uint32 * 15)()  # MIB_TCPSTATS consists of 15 32-bit fields.
            if getter(statistics, family) == 0:
                counts[family] = statistics[11]  # dwRetransSegs
    return counts


def connectivity_samples(state):
    target = probe_target(state)
    rows, error = powershell_json(PROBE_SCRIPT, variables={"MONITOR_PROBE_TARGET": target}, timeout=13)
    if isinstance(rows, list):
        for row in rows:
            yield row["kind"], row["target"], row["latency"], row["loss"], row["status"]
    else:
        yield "router", "", None, None, "Probe unavailable"
        yield "internet", target, None, None, "Probe unavailable" if target else "Disabled"


def powershell_json(script, *, variables=None, timeout=20):
    env = {**os.environ, **(variables or {})}
    prefix = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $ErrorActionPreference='Stop'; "
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", prefix + script],
            stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8-sig", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=timeout, env=env,
        )
        if result.returncode:
            return None, " ".join(result.stderr.split())[:200] or f"PowerShell exit {result.returncode}"
        return json.loads(result.stdout), ""
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return None, str(error)[:200]


def nvidia_tool():
    installed = shutil.which("nvidia-smi.exe")
    if installed:
        return installed
    root = os.environ.get("SystemRoot", r"C:\Windows")
    program_files = os.environ.get("ProgramW6432", os.environ.get("ProgramFiles", r"C:\Program Files"))
    for candidate in (Path(root) / "System32" / "nvidia-smi.exe",
                      Path(program_files) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def refresh_shortcut_names(launcher):
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        return
    root = (Path(root) / "SystemMonitorWindows").resolve()
    if launcher.resolve() != root / "App" / "monitor.py":
        return
    powershell_json(SHORTCUT_RENAME_SCRIPT, timeout=5,
                    variables={"MONITOR_MAIN": str(launcher.resolve()),
                               "MONITOR_PYTHONW": str(root / "Python" / "Scripts" / "pythonw.exe")})


class PropertyKey(ctypes.Structure):
    _fields_ = [("format_id", ctypes.c_uint32 * 4), ("property_id", ctypes.c_uint32)]


class PropertyValue(ctypes.Union):
    _fields_ = [("text", ctypes.c_wchar_p), ("storage", ctypes.c_byte * 16)]


class PropertyVariant(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint16), ("reserved", ctypes.c_uint16 * 3), ("value", PropertyValue)]


def set_taskbar_app_id():
    set_app_id = ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID
    set_app_id.argtypes = (ctypes.c_wchar_p,)
    set_app_id.restype = ctypes.c_int32
    set_app_id(TASKBAR_APP_ID)


def taskbar_relaunch_properties(launcher, state, autostart):
    python = Path(sys.executable)
    windowed = python.with_name("pythonw.exe")
    command = [str(windowed if windowed.is_file() else python), str(launcher.resolve()), "--state-dir", str(state)]
    if not autostart:
        command.append("--no-start")
    # Set the relaunch command, icon and display name before the window's AppUserModelID.
    return (
        (2, subprocess.list2cmdline(command)),
        (3, f"{launcher.resolve().with_name('system-monitor.ico')},0"),
        (4, "Windows System Monitor"),
        (5, TASKBAR_APP_ID),
    )


def set_taskbar_window_properties(hwnd, properties):
    get_store = ctypes.WinDLL("shell32").SHGetPropertyStoreForWindow
    guid = ctypes.c_uint32 * 4
    get_store.argtypes = (ctypes.c_void_p, ctypes.POINTER(guid), ctypes.POINTER(ctypes.c_void_p))
    get_store.restype = ctypes.c_int32
    store = ctypes.c_void_p()
    iid = guid.from_buffer_copy(UUID("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99").bytes_le)
    if get_store(hwnd, ctypes.byref(iid), ctypes.byref(store)) < 0:
        return

    try:
        set_value = com_method(store, 6, ctypes.c_int32, ctypes.POINTER(PropertyKey), ctypes.POINTER(PropertyVariant))
        format_id = guid.from_buffer_copy(UUID("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3").bytes_le)
        for pid, text in properties:
            key = PropertyKey(format_id, pid)
            value = PropertyVariant()  # VT_EMPTY removes a window property.
            if text is not None:
                value.type = 31  # VT_LPWSTR
                value.value.text = text
            if set_value(store, ctypes.byref(key), ctypes.byref(value)) < 0:
                break
    finally:
        com_method(store, 2, ctypes.c_uint32)(store)


def com_method(pointer, index, result, *arguments):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(table[index])


class DxgiAdapterDescription(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_wchar * 128),
        ("vendor", ctypes.c_uint32), ("device", ctypes.c_uint32),
        ("subsystem", ctypes.c_uint32), ("revision", ctypes.c_uint32),
        ("dedicated_video", ctypes.c_size_t), ("dedicated_system", ctypes.c_size_t),
        ("shared_system", ctypes.c_size_t),
        ("luid_low", ctypes.c_uint32), ("luid_high", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
    ]


def dxgi_adapters():
    try:
        create = ctypes.WinDLL("dxgi").CreateDXGIFactory1
    except (AttributeError, OSError):
        return []
    guid = (ctypes.c_uint32 * 4).from_buffer_copy(UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le)
    factory = ctypes.c_void_p()
    create.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    create.restype = ctypes.c_int32
    if create(ctypes.byref(guid), ctypes.byref(factory)) < 0:
        return []
    rows = []
    try:
        enumerate_adapter = com_method(factory, 12, ctypes.c_int32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))
        for index in range(64):
            adapter = ctypes.c_void_p()
            if enumerate_adapter(factory, index, ctypes.byref(adapter)) < 0:
                break
            try:
                description = DxgiAdapterDescription()
                describe = com_method(adapter, 10, ctypes.c_int32, ctypes.POINTER(DxgiAdapterDescription))
                if describe(adapter, ctypes.byref(description)) >= 0 and not description.flags & 2:
                    rows.append((description.name, description.vendor, description.device,
                                 (description.luid_high, description.luid_low), description.dedicated_video))
            finally:
                com_method(adapter, 2, ctypes.c_uint32)(adapter)
    finally:
        com_method(factory, 2, ctypes.c_uint32)(factory)
    return rows


def gpu_counter_values(counters):
    engines, busy, dedicated = {}, {}, {}
    for entries, field in (("engine", "UtilizationPercentage"), ("memory", "DedicatedUsage")):
        for row in counters.get(entries, []):
            name = row.get("Name") or ""
            match = re.search(r"luid_0x([0-9a-f]+)_0x([0-9a-f]+)_phys_(\d+)", name, re.I)
            if not match or row.get(field) is None:
                continue
            luid = (int(match[1], 16), int(match[2], 16))
            value = float(row[field])
            if entries == "engine":
                engine = (luid, name[match.start(3):].casefold())
                engines[engine] = engines.get(engine, 0) + value
            else:
                dedicated[luid] = dedicated.get(luid, 0) + value
    for (luid, _), value in engines.items():
        busy[luid] = min(100, max(busy.get(luid, 0), value))
    return busy, dedicated


def gpu_rows(adapters, controllers, counters, extra):
    busy, dedicated = gpu_counter_values(counters or {})
    rows = []
    matched = set()
    for name, vendor, device, luid, capacity in adapters:
        identifier = f"VEN_{vendor:04X}&DEV_{device:04X}"
        candidates = [(index, row) for index, row in enumerate(controllers) if index not in matched and (
            name.casefold() == str(row.get("Name")).casefold() or identifier in (row.get("PNPDeviceID") or "").upper())]
        driver = "—"
        if candidates:
            index, controller = candidates[0]
            matched.add(index)
            driver = controller.get("DriverVersion") or driver
        usage = dedicated.get(luid)
        rows.append([name, driver, busy.get(luid), None,
                     usage / 2**20 if usage is not None else None, capacity / 2**20 if capacity else None, None])
    for index, controller in enumerate(controllers):
        if index not in matched:
            rows.append([controller.get("Name") or "Display adapter", controller.get("DriverVersion") or "—",
                         None, None, None, None, None])
    enriched = set()
    for telemetry in extra:
        key = telemetry[0].casefold().removeprefix("nvidia ")
        slot = next((index for index, row in enumerate(rows) if index not in enriched and
                     row[0].casefold().removeprefix("nvidia ") == key), None)
        if slot is None:
            enriched.add(len(rows))
            rows.append(list(telemetry))
        else:
            enriched.add(slot)
            match = rows[slot]
            match[1] = telemetry[1]
            for index, value in enumerate(telemetry[2:], 2):
                if match[index] is None:
                    match[index] = value
    totals = Counter(row[0] for row in rows)
    indexes = Counter()
    for row in rows:
        name = row[0]
        if totals[name] > 1:
            indexes[name] += 1
            row[0] = f"{name} ({indexes[name]})"
    return rows


def inventory_rows(data):
    if not data:
        return [], [], {}
    rows, displays, disks = [], [], {}
    for item in data.get("Win32_ComputerSystem", []):
        rows.append(("System", "Computer", " ".join(str(item[key]) for key in ("Manufacturer", "Model") if item.get(key))))
    for item in data.get("Win32_BaseBoard", []):
        rows.append(("System", "Motherboard", " ".join(str(item[key]) for key in ("Manufacturer", "Product") if item.get(key))))
    for item in data.get("Win32_BIOS", []):
        rows.append(("System", "BIOS", item.get("SMBIOSBIOSVersion") or item.get("Name") or "Unknown"))
    for item in data.get("Win32_Processor", []):
        rows.append(("CPU", item.get("Name") or "Processor",
                     f"{item.get('NumberOfCores') or '?'} cores / {item.get('NumberOfLogicalProcessors') or '?'} threads"))
    for item in data.get("Win32_VideoController", []):
        name = item.get("Name") or "Display adapter"
        vendor = item.get("Manufacturer") or ("NVIDIA" if "nvidia" in name.lower() else "—")
        displays.append((name, vendor, item.get("PNPDeviceID") or "—", None, None))
        rows.append(("Graphics", name, f"Driver {item.get('DriverVersion') or 'unknown'}"))
    for item in data.get("Win32_DiskDrive", []):
        index = item.get("Index")
        label = item.get("Model") or item.get("Name") or "Drive"
        capacity = item.get("Size")
        readable = f"{int(capacity) / 10**9:,.0f} GB" if str(capacity).isdecimal() else "Capacity unavailable"
        if index is not None:
            disks[str(index)] = label
        rows.append(("Storage", label, readable))
    for item in data.get("Win32_USBController", []):
        name = item.get("Name") or "USB controller"
        rows.append(("USB", name, item.get("Manufacturer") or "—"))
    return rows, displays, disks


def services():
    rows = []
    try:
        for service in psutil.win_service_iter():
            try:
                details = service.as_dict()
            except psutil.Error:
                continue
            rows.append(["Windows", details["name"], details.get("start_type") or "unknown",
                         details.get("status") or "unknown", "—", details.get("display_name") or details["name"]])
    except (OSError, psutil.Error) as error:
        return rows, f"Limited · {error}"
    return rows, "Readable"


def format_event(channel, event):
    provider = event.get("provider") or channel
    event_id = event.get("id")
    message = (event.get("message") or "").strip() or "Windows supplied no event description."
    context = f"{provider}: {message}"
    category = next((label for label, pattern in EVENT_RULES if pattern.search(context)),
                    "Application" if channel == "Application" else "System / Apps")
    return (event["timestamp"] / 1000, EVENT_LEVEL.get(event.get("level"), 6), category,
            f"{provider} · {channel}"[:160], f"Event {event_id} · {message}")


def ingest_events(db, channel, page, record, setting):
    key = f"windows_cursor_{channel}"
    cursor_row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    cursor = int(cursor_row[0]) if cursor_row and not page.get("reset") else 0
    rows = sorted(page.get("rows") or [], key=lambda event: event["record"])
    with db:
        for event in rows:
            if int(event["record"]) > cursor:
                record(db, *format_event(channel, event))
                cursor = int(event["record"])
        setting(db, key, cursor)


def event_worker(database, record, setting, stop):
    with database() as db:
        while not stop.is_set():
            coverage = []
            catch_up = False
            for channel in CHANNELS:
                if stop.is_set():
                    return
                row = db.execute("SELECT value FROM settings WHERE key=?", (f"windows_cursor_{channel}",)).fetchone()
                page, error = powershell_json(EVENT_SCRIPT, variables={
                    "MONITOR_EVENT_CHANNEL": channel, "MONITOR_EVENT_CURSOR": str(int(row[0]) if row else 0),
                }, timeout=15)
                if isinstance(page, dict):
                    ingest_events(db, channel, page, record, setting)
                    catch_up |= len(page.get("rows") or []) >= 250
                    coverage.append(f"{channel} readable")
                else:
                    coverage.append(f"{channel} unavailable")
                    if error:
                        print(f"{channel} Event Log: {error}", flush=True)
            with db:
                setting(db, "journal_status", " · ".join(coverage))
                setting(db, "kernel_journal", "Readable" if "System readable" in coverage else "Unavailable to this account")
            stop.wait(1 if catch_up else 10)
