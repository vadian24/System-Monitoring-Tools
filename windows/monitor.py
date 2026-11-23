#!/usr/bin/python3
"""Local system telemetry collector and Qt desktop launcher."""
import argparse
from collections import deque
import csv
import json
import os
from pathlib import Path
import re
import selectors
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

import psutil

import updater

from network import NetworkSampler

WINDOWS = sys.platform == "win32"
if WINDOWS:
    import windows_platform

if WINDOWS:
    STATE = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "SystemMonitorWindows" / "History"
else:
    STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "nobara-system-monitor"
INTERVAL = 2
STOP = threading.Event()
RULES = [
    ("Graphics", r"\bNVRM:.*\bXid\b|GPU HANG|GPU reset|GPU has fallen|flip_done timed out|page.?flip.*(?:timeout|fail)|atomic commit failed|Applying output configuration failed"),
    ("Storage", r"\bnvme\S*.*(?:timed? ?out|reset|abort|fail)|\b(?:Buffer |blk_update_request: )?I/O error\b|\bata\d+.*(?:slow to respond|hard resetting|exception|failed command)|\b(?:BTRFS|EXT4-fs|XFS).*\b(?:corrupt|error)\b|\bsmartd\b.*(?:failing|uncorrectable|unreadable|critical warning)"),
    ("Memory", r"out of memory|oom-kill|oomd.*killed|\bEDAC\b.*(?:error|uncorrectable)|Memory failure"),
    ("Kernel", r"machine check exception|\[Hardware Error\]|PCIe Bus Error|blocked for more than|(?:soft|hard) lockup|rcu.*stall|kernel panic"),
    ("Input / USB", r"razermouse.*timed out|device descriptor read.*error|unable to enumerate USB|couldn.t find an input interrupt endpoint|xhci.*(?:timeout|error|not responding)"),
    ("Thermals", r"thermal.*(?:critical|throttl)|temperature above threshold"),
    ("Network", r"NETDEV WATCHDOG|iwlwifi.*(?:Timed Out|firmware error)|igc .*exceed max|\b(?:igc|e1000e|r8169|r8152)\b.*link is down|Bluetooth: .*Failed"),
    ("Power / Firmware", r"ACPI (?:BIOS )?Error|\[Firmware Bug\]|PM: suspend (?:entry|exit)|PM: failed to resume|watchdog.*did not stop"),
    ("Services", r"Failed to (?:start|reload|stop) |Failed with result "),
    ("Application", r"dumped core|segfault at|systemd-coredump.*\bcrashed\b"),
]
RULES = [(category, re.compile(pattern, re.I)) for category, pattern in RULES]


def read(path):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return ""


def command(arguments, timeout=3):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, errors="replace",
                                timeout=timeout, env={**os.environ, "LC_ALL": "C"},
                                creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0)
        return result.stdout, result.returncode, result.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return "", -1, str(error)


def database(readonly=False):
    if readonly:
        connection = sqlite3.connect((STATE / "history.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=2)
    else:
        connection = sqlite3.connect(STATE / "history.sqlite3", timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    connection.row_factory = sqlite3.Row
    return connection


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS samples (
            ts INTEGER PRIMARY KEY, cpu REAL, memory REAL, io_full REAL,
            disk_write REAL, network REAL, disk_read REAL
        );
        CREATE TABLE IF NOT EXISTS io_pressure_history (ts INTEGER PRIMARY KEY, some REAL);
        CREATE TABLE IF NOT EXISTS drive_activity_history (
            ts INTEGER NOT NULL, device TEXT NOT NULL, read_rate REAL, write_rate REAL,
            PRIMARY KEY(ts, device)
        );
        CREATE INDEX IF NOT EXISTS drive_activity_device_time ON drive_activity_history(device, ts);
        CREATE TABLE IF NOT EXISTS cpu_temperature_history (
            ts INTEGER PRIMARY KEY, celsius REAL
        );
        CREATE TABLE IF NOT EXISTS gpu_history (
            ts INTEGER NOT NULL, slot INTEGER NOT NULL, name TEXT NOT NULL,
            utilization REAL, temperature REAL, vram_used REAL, vram_total REAL, power REAL,
            PRIMARY KEY(slot,ts)
        );
        CREATE INDEX IF NOT EXISTS gpu_history_time ON gpu_history(ts);
        CREATE TABLE IF NOT EXISTS traffic_history (
            ts INTEGER PRIMARY KEY, received REAL NOT NULL, sent REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS network_health_history (
            ts INTEGER PRIMARY KEY, retransmits INTEGER, drops INTEGER NOT NULL, link_losses INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS network_probe_history (
            ts INTEGER NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
            latency REAL, loss REAL, status TEXT NOT NULL, PRIMARY KEY(ts, kind)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS network_peers (
            protocol TEXT NOT NULL, host TEXT NOT NULL, port INTEGER NOT NULL, process TEXT NOT NULL,
            country TEXT NOT NULL, pids TEXT NOT NULL, states TEXT NOT NULL,
            first REAL NOT NULL, last REAL NOT NULL, sent INTEGER, received INTEGER, flagged REAL,
            PRIMARY KEY(protocol,host,port,process)
        );
        CREATE INDEX IF NOT EXISTS network_peers_last ON network_peers(last DESC);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, first REAL NOT NULL, last REAL NOT NULL,
            severity INTEGER NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL,
            message TEXT NOT NULL, repeats INTEGER NOT NULL,
            UNIQUE(first, category, source, message)
        );
        CREATE INDEX IF NOT EXISTS event_latest ON events(last DESC);
        CREATE INDEX IF NOT EXISTS event_signature ON events(category, source, message, last);
    """)
    if "disk_read" not in {column[1] for column in db.execute("PRAGMA table_info(samples)")}:
        db.execute("ALTER TABLE samples ADD COLUMN disk_read REAL")


def is_loopback(name):
    return name == "lo" or (WINDOWS and "loopback" in name.casefold())


class NetworkHealth:
    def __init__(self):
        self.tcp = {}
        self.links = {}

    def sample(self, interfaces, previous, links, gap):
        tcp = windows_platform.tcp_retransmit_counts() if WINDOWS else {}
        deltas = [value - self.tcp[family] for family, value in tcp.items()
                  if family in self.tcp and value >= self.tcp[family]]
        retransmits = sum(deltas) if deltas and not gap else None
        self.tcp = tcp
        events = []
        drops = link_losses = 0
        if not gap:
            for name, current in interfaces.items():
                if is_loopback(name) or name not in previous:
                    continue
                old = previous[name]
                count = max(0, current.dropin - old.dropin) + max(0, current.dropout - old.dropout)
                drops += count
                if count:
                    events.append((4, "Network", name, "Interface reported packet drops."))
            for name, link in links.items():
                if not is_loopback(name) and not link.isup and self.links.get(name):
                    link_losses += 1
                    events.append((4, "Network", name, "Network adapter link went down."))
        self.links = {name: link.isup for name, link in links.items()}
        return (retransmits, drops, link_losses), events


def record(db, when, severity, category, source, message):
    previous = db.execute(
        "SELECT id,last FROM events WHERE category=? AND source=? AND message=? ORDER BY last DESC LIMIT 1",
        (category, source, message)).fetchone()
    if previous and 0 <= when - previous["last"] <= 120:
        db.execute("UPDATE events SET last=?, severity=min(severity,?), repeats=repeats+1 WHERE id=?",
                   (when, severity, previous["id"]))
    else:
        db.execute("INSERT OR IGNORE INTO events(first,last,severity,category,source,message,repeats) VALUES(?,?,?,?,?,?,1)",
                   (when, when, severity, category, source, message))


def setting(db, key, value):
    db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def write_json(name, value):
    path = STATE / name
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
    temp.replace(path)


def classify(row):
    message = row.get("MESSAGE", "")
    if isinstance(message, list) and all(isinstance(item, int) and 0 <= item <= 255 for item in message):
        message = bytes(message).decode(errors="replace")
    if not isinstance(message, str) or not message.strip():
        return None
    message = message.strip()[:4000]
    try:
        priority = int(row.get("PRIORITY", 6))
    except (ValueError, TypeError):
        priority = 6
    source = row.get("_SYSTEMD_USER_UNIT") or row.get("SYSLOG_IDENTIFIER") or row.get("_SYSTEMD_UNIT") or row.get("_COMM") or "journal"
    searchable = f"{source}: {message}"
    category = next((label for label, pattern in RULES if pattern.search(searchable)), None)
    if not 0 <= priority <= 7:
        priority = 6
    if priority > 4 and not category:
        return None
    if category and "PM: suspend " not in message:
        priority = min(priority, 4)
    # Keep the complete signature, but remove variable transaction parameters from this recurring driver warning.
    if "razermouse: Command timed out." in message:
        message = message.split(" transaction_id.", 1)[0]
    return priority, category or ("Kernel" if row.get("_TRANSPORT") == "kernel" else "System / Apps"), str(source)[:160], message


def commit_journal(db, batch):
    if not batch:
        return
    with db:
        last_time = None
        cursor = None
        for row in batch:
            try:
                when = int(row["__REALTIME_TIMESTAMP"]) / 1_000_000
            except (KeyError, ValueError, TypeError):
                continue
            last_time = when
            cursor = row.get("__CURSOR")
            event = classify(row)
            if event:
                record(db, when, *event)
        if cursor:
            setting(db, "cursor", cursor)
            setting(db, "journal_time", last_time)


def journal_once(db, cursor, since, follow=False):
    arguments = ["journalctl", "--lines=all", "--no-pager", "-o", "json",
                 "--output-fields=MESSAGE,PRIORITY,_TRANSPORT,SYSLOG_IDENTIFIER,_SYSTEMD_UNIT,_SYSTEMD_USER_UNIT,_COMM"]
    if follow:
        arguments.append("--follow")
    arguments.extend(["--after-cursor", cursor] if cursor else ["--since", since])
    batch = []
    buffer = b""
    with subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while not STOP.is_set():
                    for ready, _ in selector.select(timeout=0.5):
                        data = os.read(ready.fd, 65536)
                        if not data:
                            commit_journal(db, batch)
                            child.wait(timeout=2)
                            if not follow and child.returncode == 0:
                                return ""
                            return child.stderr.read(4000).decode(errors="replace").strip() or f"journalctl exited ({child.returncode})"
                        lines = (buffer + data).split(b"\n")
                        buffer = lines.pop()
                        for line in lines:
                            try:
                                batch.append(json.loads(line))
                            except (ValueError, UnicodeError):
                                continue
                    commit_journal(db, batch)
                    batch.clear()
                return ""
        finally:
            if child.poll() is None:
                child.terminate()


def journal_worker():
    with database() as db:
        def get(key, default=""):
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

        while not STOP.is_set():
            out, _, _ = command(["journalctl", "-n", "1", "-o", "json", "--no-pager", "_TRANSPORT=kernel"])
            with db:
                setting(db, "kernel_journal", "Readable" if out.startswith("{") else "Unavailable to this account")
                setting(db, "journal_status", "Reading journal history")
            cursor = get("cursor")
            fallback = float(get("journal_time", str(time.time() - 7 * 86400)))
            try:
                since = f"@{fallback + 0.000001:.6f}"
                # Follow mode defaults to the current boot, so catch up across retained boots first.
                error = journal_once(db, cursor, since)
                if not error and not STOP.is_set():
                    cursor = get("cursor")
                    with db:
                        setting(db, "journal_status", "Live")
                    error = journal_once(db, cursor, since, follow=True)
            except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
                error = str(exc)
            if STOP.is_set():
                break
            with db:
                setting(db, "journal_status", f"Retrying · {error[:380]}")
                if cursor and re.search(r"cursor|seek", error, re.I):
                    db.execute("DELETE FROM settings WHERE key='cursor'")
            STOP.wait(5)


def pressure(previous, elapsed):
    values = {}
    totals = {}
    for name in ("cpu", "memory", "io"):
        for line in read(f"/proc/pressure/{name}").splitlines():
            mode, *fields = line.split()
            info = dict(field.split("=", 1) for field in fields)
            key = f"{name}.{mode}"
            total = int(info["total"])
            totals[key] = total
            values[key] = min(100, max(0, (total - previous[key]) / (elapsed * 10000))) if key in previous else float(info["avg10"])
    return values, totals


def mount_usage():
    rows = []
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        rows.append([part.mountpoint, part.device, part.fstype, usage.percent, usage.free])
    return rows


def inventory():
    dmi = "/sys/class/dmi/id/"
    rows = [
        ("System", "Host", os.uname().nodename),
        ("System", "Kernel", os.uname().release),
        ("System", "Motherboard", " ".join(filter(None, [read(dmi+"board_vendor"), read(dmi+"board_name")]))),
        ("System", "BIOS", " • ".join(filter(None, [read(dmi+"bios_version"), read(dmi+"bios_date")]))),
        ("CPU", "Model", next((line.split(":", 1)[1].strip() for line in read("/proc/cpuinfo").splitlines() if line.startswith("model name")), "")),
        ("CPU", "Cores / threads", f"{psutil.cpu_count(logical=False) or '—'} / {psutil.cpu_count() or '—'}"),
        ("Memory", "Physical RAM", size(psutil.virtual_memory().total)),
    ]
    for path in sorted(Path("/sys/bus/pci/devices").glob("*")):
        driver = (path / "driver").resolve().name if (path / "driver").is_symlink() else "unbound"
        rows.append(("PCI", path.name, f"{read(path/'vendor')}:{read(path/'device')} • {driver}"))
    pci, _, _ = command(["lspci", "-Dnn"])
    names = dict(line.split(" ", 1) for line in pci.splitlines() if " " in line)
    rows = [(group, name, f"{names[name]} • {detail.split(' • ')[-1]}" if group == "PCI" and name in names else detail)
            for group, name, detail in rows]
    for path in sorted(Path("/sys/bus/usb/devices").glob("*")):
        vendor, product_id = read(path/"idVendor"), read(path/"idProduct")
        if vendor and product_id:
            product = read(path/"product") or read(path/"manufacturer") or "USB device"
            rows.append(("USB", f"{path.name}  {product}", f"{vendor}:{product_id}"))
    for path in sorted(Path("/sys/block").glob("*")):
        if path.name.startswith(("loop", "ram", "zram", "dm-")):
            continue
        model = read(path/"device/model") or path.name
        rows.append(("Storage", path.name, f"{model} • {size(int(read(path/'size') or 0) * 512)}"))
    return rows


def size(value):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024


def services(user=False):
    args = ["systemctl"] + (["--user"] if user else []) + [
        "list-units", "--all", "--type=service,socket,timer", "--plain", "--no-legend", "--no-pager"]
    output, status, error = command(args)
    rows = []
    for line in output.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) >= 4:
            rows.append(["User" if user else "System", *parts, *([] if len(parts) == 5 else [""])])
    return rows, "Readable" if status == 0 else f"Unavailable · {error[:150] or status}"


def drm_metrics():
    rows = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        pci = card / "device"
        if not re.fullmatch(r"card[0-9]+", card.name) or not (pci / "vendor").exists():
            continue
        vendor_id = read(pci / "vendor")
        vendor = {"0x8086": "Intel", "0x1002": "AMD", "0x10de": "NVIDIA"}.get(vendor_id, vendor_id)
        mhz = read(card / "gt_act_freq_mhz") or read(pci / "gt_act_freq_mhz")
        busy = read(pci / "gpu_busy_percent")
        rows.append([card.name, vendor, pci.resolve().name,
                     int(busy) if busy.isdecimal() else None, int(mhz) if mhz.isdecimal() else None])
    return rows


def gpu():
    if WINDOWS:
        executable = windows_platform.nvidia_tool()
    else:
        executable = "nvidia-smi" if Path("/proc/driver/nvidia/version").exists() else None
    if not executable:
        return [], "NVIDIA telemetry unavailable"
    result, status, error = command([executable, "--query-gpu=name,driver_version,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw",
                                     "--format=csv,noheader,nounits"])
    rows = []
    for row in csv.reader(result.splitlines(), skipinitialspace=True):
        if status == 0 and len(row) == 7:
            values = []
            for field in row[2:]:
                try:
                    values.append(float(field))
                except ValueError:
                    values.append(None)
            rows.append([*row[:2], *values])
    return rows, "Live" if rows else (error or result.strip() or "NVIDIA telemetry unavailable")[:180]


def windows_inventory_worker(sampler):
    while not STOP.is_set():
        data, error = windows_platform.powershell_json(windows_platform.INVENTORY_SCRIPT, timeout=22)
        rows, displays, disks = windows_platform.inventory_rows(data)
        if rows:
            sampler.windows_video_controllers = data.get("Win32_VideoController", [])
            sampler.windows_inventory = (
                [("System", "Windows build", ".".join(map(str, sys.getwindowsversion().platform_version))),
                 ("Memory", "Physical RAM", size(psutil.virtual_memory().total))] + rows, displays, disks
            )
        elif error:
            print(f"Windows hardware inventory: {error}", flush=True)
        STOP.wait(90)


def nvidia_thermal(executable):
    raw, status, _ = command([executable, "-q", "-x"])
    if status:
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    results = {}
    for gpu in root.findall("gpu"):
        name = gpu.findtext("product_name")
        pci = gpu.findtext("pci/pci_bus_id") or gpu.get("id")
        if not name:
            continue
        def threshold(key):
            value = gpu.findtext(f"temperature/{key}", "").split()
            return int(value[0]) if value and value[0].isdigit() and int(value[0]) <= 150 else None
        reasons = gpu.find("clocks_event_reasons")
        if reasons is None:
            reasons = gpu.find("clocks_throttle_reasons")
        throttling = None
        if reasons is not None:
            thermal = [e.text for e in reasons if e.tag.endswith("thermal_slowdown") and e.text in ("Active", "Not Active")]
            throttling = "Active" in thermal if thermal else None
        data = (threshold("gpu_temp_max_gpu_threshold"), threshold("gpu_temp_slow_threshold"), throttling)
        results[pci[-12:].lower() if pci else name] = (name, data)
    return results


def windows_cpu_temperature_worker(sampler):
    while not STOP.is_set():
        readings, _ = windows_platform.powershell_json(windows_platform.CPU_TEMPERATURE_SCRIPT, timeout=8)
        sampler.windows_cpu_temperature, sampler.windows_cpu_details = windows_platform.cpu_sensor_details(readings or {})
        STOP.wait(5)


def windows_disk_worker(sampler):
    while not STOP.is_set():
        rows, _ = windows_platform.powershell_json(windows_platform.DISK_ACTIVITY_SCRIPT, timeout=8)
        sampler.windows_disk_details = {f"PhysicalDrive{row['Number']}": row for row in rows or []}
        STOP.wait(4)


def windows_gpu_worker(sampler):
    adapters = []
    next_inventory = 0
    while not STOP.is_set():
        tick = time.monotonic()
        if tick >= next_inventory:
            adapters = windows_platform.dxgi_adapters()
            next_inventory = tick + 45
        controllers = sampler.windows_video_controllers
        counters, _ = windows_platform.powershell_json(windows_platform.GPU_COUNTERS_SCRIPT, timeout=8)
        extra, _ = gpu()
        rows = windows_platform.gpu_rows(adapters, controllers, counters, extra)
        status = "Display adapters detected. Readings depend on the Windows graphics driver." if rows else "No display adapters detected"
        executable = windows_platform.nvidia_tool() if extra else None
        sensors = list(nvidia_thermal(executable).values()) if executable else []
        thermal = {}
        for row in rows:
            name = row[0].casefold().removeprefix("nvidia ")
            matches = [data for adapter, data in sensors if adapter.casefold().removeprefix("nvidia ") == name]
            if len(matches) == 1:
                thermal[row[0]] = matches[0]
        sampler.windows_gpu = rows, thermal, status
        STOP.wait(4)


def cpu_temperature(sensors):
    readings = [(driver, name, value, high, critical) for driver, name, value, high, critical in sensors
                if driver in {"coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "cpu_therm", "via-cputemp"}
                and value is not None]
    packages = [row for row in readings if "package" in row[1].lower() or row[1].lower() == "tdie"]
    controls = [row for row in readings if row[1].lower() == "tctl"]
    if not readings:
        return None
    driver, name, value, high, critical = max(packages or controls or readings, key=lambda row: row[2])
    high = high if high and high > 0 else None
    critical = critical if critical and critical > 0 else None
    return [value, high, critical, f"{driver} · {name}"]


class Sampler:
    def __init__(self):
        self.last = time.monotonic()
        self.last_wall = time.time()
        self.disk = psutil.disk_io_counters(perdisk=True, nowrap=False) or {}
        self.net = psutil.net_io_counters(pernic=True, nowrap=False) or {}
        self.network_health = NetworkHealth()
        self.psi = {}
        self.proc_io = {}
        self.proc_times = deque()
        self.inventory = []
        self.mounts = []
        self.services = []
        self.system_status = self.user_status = "Checking"
        self.known_services = {}
        self.next_inventory = self.next_services = 0
        self.windows_inventory = ([("System", "Hardware inventory", "Waiting for Windows hardware information")], [], {})
        self.windows_video_controllers = []
        self.windows_cpu_temperature = None
        self.windows_disk_details = {}
        self.windows_cpu_details = {}
        self.windows_gpu = [], {}, "Checking display adapters"
        if WINDOWS:
            threading.Thread(target=windows_inventory_worker, args=(self,), name="inventory", daemon=True).start()
            threading.Thread(target=windows_cpu_temperature_worker, args=(self,), name="cpu-temperature", daemon=True).start()
            threading.Thread(target=windows_disk_worker, args=(self,), name="disk-activity", daemon=True).start()
            threading.Thread(target=windows_gpu_worker, args=(self,), name="graphics", daemon=True).start()
        psutil.cpu_percent()
        psutil.cpu_percent(percpu=True)

    def sample(self):
        tick = time.monotonic()
        elapsed = max(0.01, tick - self.last)
        gap = elapsed > 10
        self.last = tick
        wall = time.time()
        network_gap = gap or abs(wall - self.last_wall) > 10
        self.last_wall = wall
        cpu = psutil.cpu_percent()
        cores = psutil.cpu_percent(percpu=True)
        memory, swap = psutil.virtual_memory(), psutil.swap_memory()
        psi, self.psi = ({}, {}) if WINDOWS else pressure({} if gap else self.psi, elapsed)
        disk = psutil.disk_io_counters(perdisk=True, nowrap=False) or {}
        drives = []
        hardware, displays, labels = self.windows_inventory
        if WINDOWS:
            models = {name: labels.get(name.removeprefix("PhysicalDrive"), name) for name in disk}
        else:
            models = {node.name: read(node/"device/model") or node.name for node in Path("/sys/block").glob("*")
                      if not node.name.startswith(("loop", "ram", "zram", "dm-"))}
        for name, model in sorted(models.items()):
            if name not in disk:
                continue
            current, old = disk[name], self.disk.get(name, disk[name])
            writes = max(0, current.write_count - old.write_count)
            busy = min(100, max(0, current.busy_time-old.busy_time)/elapsed/10) if hasattr(current, "busy_time") else None
            reads = current.read_count - old.read_count
            valid = name in self.disk and not gap and reads >= 0 and current.write_count >= old.write_count
            detail = self.windows_disk_details.get(name, {})
            if busy is None and detail.get("Idle") is not None:
                busy = max(0, min(100, 100 - detail["Idle"]))
            drives.append([name, model,
                           max(0, current.read_bytes-old.read_bytes)/elapsed,
                           max(0, current.write_bytes-old.write_bytes)/elapsed,
                           busy, max(0, current.write_time-old.write_time)/writes if writes else None,
                           reads/elapsed if valid else None, writes/elapsed if valid else None,
                           (current.read_time-old.read_time)/reads if valid and reads and current.read_time >= old.read_time else None,
                           detail.get("Queue"), None])
        self.disk = disk
        net = psutil.net_io_counters(pernic=True, nowrap=False) or {}
        interfaces = []
        try:
            links = psutil.net_if_stats() if WINDOWS else {}
        except OSError:
            links = {}
        network_health, network_events = self.network_health.sample(net, self.net, links, network_gap)
        for name, current in sorted(net.items()):
            old = self.net.get(name, current)
            if WINDOWS:
                link = ("up" if links[name].isup else "down") if name in links else ""
            else:
                link = read(Path("/sys/class/net") / name / "operstate")
            interfaces.append([name, link.capitalize() or "Unknown",
                               max(0, current.bytes_recv-old.bytes_recv)/elapsed,
                               max(0, current.bytes_sent-old.bytes_sent)/elapsed,
                               max(0, current.errin+current.errout-old.errin-old.errout)])
        self.net = net
        processes = []
        io_cache = {}
        cpu_times = {}
        process_times = []
        cutoff = tick - 60
        while len(self.proc_times) > 1 and self.proc_times[1][0] <= cutoff:
            self.proc_times.popleft()
        baseline = self.proc_times[0][1] if self.proc_times and self.proc_times[0][0] <= cutoff else None
        minimum_start = time.time() - 60
        for proc in psutil.process_iter(["pid", "name", "username", "memory_info", "status", "create_time"], ad_value=None):
            try:
                info = proc.info
                key = (proc.pid, info["create_time"])
                try:
                    io = proc.io_counters()
                    old = self.proc_io.get(key, io.write_bytes)
                    io_cache[key] = io.write_bytes
                    write = max(0, io.write_bytes-old)/elapsed
                except (psutil.AccessDenied, PermissionError):
                    write = None
                try:
                    times = proc.cpu_times()
                    total = times.user + times.system
                    cpu_times[key] = total
                    old_total = baseline.get(key) if baseline is not None else None
                    if old_total is None and key[1] is not None and key[1] >= minimum_start:
                        old_total = 0
                    recent = max(0, total - old_total) if old_total is not None else None
                    process_times.append([proc.pid, info["name"] or "—", recent, total])
                except (psutil.AccessDenied, PermissionError):
                    pass
                processes.append([proc.pid, info["name"] or "—", info["username"] or "—",
                                  proc.cpu_percent(), info["memory_info"].rss if info["memory_info"] else None,
                                  write, info["status"] or "—"])
            except (psutil.Error, OSError):
                continue
        self.proc_io = io_cache
        self.proc_times.append((tick, cpu_times))
        temperature_groups = {} if WINDOWS else psutil.sensors_temperatures()
        fan_groups = {} if WINDOWS else psutil.sensors_fans()
        temperatures = [[name, item.label or f"Sensor {i+1}", item.current, item.high, item.critical]
                        for name, items in sorted(temperature_groups.items())
                        for i, item in enumerate(items)]
        fans = [[name, item.label or f"Fan {i+1}", item.current]
                for name, items in sorted(fan_groups.items())
                for i, item in enumerate(items)]
        if tick >= self.next_inventory:
            self.inventory = inventory() if not WINDOWS else hardware
            self.mounts = mount_usage()
            self.next_inventory = tick + 45
        changed = []
        if tick >= self.next_services:
            if WINDOWS:
                system, self.system_status = windows_platform.services()
                user, self.user_status = [], ""
            else:
                system, self.system_status = services()
                user, self.user_status = services(True)
            self.services = system + user
            current_services = {(row[0], row[1]): row[3] for row in self.services}
            for key, active in current_services.items():
                if key in self.known_services and self.known_services[key] != active:
                    old = self.known_services[key]
                    if active == "failed" or old == "failed":
                        changed.append((3 if active == "failed" else 6, "Services", key[1],
                                        f"{key[0]} unit changed state from {old} to {active}."))
            self.known_services.update(current_services)
            self.next_services = tick + 12
        cpu_details = dict(self.windows_cpu_details)
        if not cpu_details.get("cores"):
            try:
                frequency = psutil.cpu_freq()
            except (OSError, psutil.Error, NotImplementedError):
                frequency = None
            cpu_details["clock"] = frequency.current if frequency and frequency.current > 0 else None
            cpu_details["cores"] = [(f"CPU {index}", "Logical", None, percent, None, None)
                                    for index, percent in enumerate(cores)]
        if WINDOWS:
            gpus, gpu_thermal, gpu_status = self.windows_gpu
        else:
            gpus, gpu_status = gpu()
            gpu_thermal = {}
        return dict(ts=time.time(), cpu_details=cpu_details, process_times=process_times, cpu_temperature=self.windows_cpu_temperature if WINDOWS else cpu_temperature(temperatures), cpu=cpu, cores=cores, memory=memory.percent, ram_used=memory.total-memory.available,
                    ram_total=memory.total, swap=swap.percent, pressure=psi, disk=drives, net=interfaces, gpu=gpus,
                    gpu_status=gpu_status, gpu_thermal=gpu_thermal, drm=displays if WINDOWS else drm_metrics(), processes=processes,
                    inventory=hardware if WINDOWS else self.inventory,
                    mounts=self.mounts, fans=fans,
                    temperatures=temperatures, services=self.services, system_status=self.system_status,
                    user_status=self.user_status, uptime=max(0, time.time()-psutil.boot_time()),
                    network_health=network_health), changed + network_events, gap


def collector():
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    with updater.exclusive_lock(STATE / "collector.lock") as locked:
        if not locked:
            return
        with updater.collector_registration(STATE, Path(__file__).resolve().parent) as token, database() as db:
            initialize(db)
            with db:
                setting(db, "session_started", time.time())
                setting(db, "journal_status", "Starting")
            if WINDOWS:
                journal = threading.Thread(target=windows_platform.event_worker, args=(database, record, setting, STOP),
                                           name="event-log", daemon=True)
            else:
                journal = threading.Thread(target=journal_worker, name="journal", daemon=True)
            journal.start()
            probes = None
            if WINDOWS:
                probes = threading.Thread(target=probe_worker, name="network-probes", daemon=True)
                probes.start()
            try:
                run_samples(db, token)
            finally:
                STOP.set()
                journal.join(timeout=7)
                if probes is not None:
                    probes.join(timeout=7)


def probe_worker():
    with database() as db:
        while not STOP.is_set():
            start = time.monotonic()
            wall = time.time()
            rows = list(windows_platform.connectivity_samples(STATE))
            elapsed = time.monotonic() - start
            if abs(time.time() - wall) < 15 and not STOP.is_set():
                stamp = int(time.time())
                try:
                    with db:
                        db.executemany("INSERT OR REPLACE INTO network_probe_history VALUES(?,?,?,?,?,?)",
                                       [(stamp, *row) for row in rows])
                except sqlite3.Error as error:
                    if sys.stderr:
                        print(f"Network probe history write failed: {error}", file=sys.stderr, flush=True)
            STOP.wait(max(0, 15 - elapsed))


def store_peers(db, now, rows):
    for protocol, host, port, process, country, pids, states, sent, received, watched in rows:
        peer = (protocol, host, port, process)
        previous = db.execute("SELECT flagged,country FROM network_peers WHERE protocol=? AND host=? AND port=? AND process=?", peer).fetchone()
        flagged = None
        if watched and (not previous or previous["flagged"] is None or previous["country"] != country
                        or now - previous["flagged"] >= 86400):
            flagged = now
            address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            record(db, now, 6, "Network", process or "Socket monitor",
                   f"Country watchlist match: {country} · {protocol.upper()} peer {address}.")
        db.execute("""
            INSERT INTO network_peers VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(protocol,host,port,process) DO UPDATE SET
                country=CASE WHEN excluded.country!='' THEN excluded.country ELSE network_peers.country END,
                pids=excluded.pids, states=excluded.states, last=excluded.last,
                sent=CASE WHEN excluded.sent IS NULL THEN network_peers.sent ELSE coalesce(network_peers.sent,0)+excluded.sent END,
                received=CASE WHEN excluded.received IS NULL THEN network_peers.received ELSE coalesce(network_peers.received,0)+excluded.received END,
                flagged=coalesce(excluded.flagged,network_peers.flagged)
        """, (*peer, country, pids, states, now, now, sent, received, flagged))


def drive_result(drive, readings=(), issues=(), *, status="Unavailable", checked=None, note=""):
    health_check = status
    if issues:
        status = "Critical" if any(issue[0] == "Critical" for issue in issues) else "Warning"
    return {**drive, "readings": list(readings), "issues": list(issues),
            "status": status, "health_check": health_check,
            "checked": time.time() if checked is None else checked, "note": note}


BACK_UP = "Back up readable files to another drive now. If the errors persist or increase, plan to replace this drive."
REPLACE_DRIVE = "Back up readable files to another drive immediately and plan to replace this drive."
COOL_DRIVE = "Reduce disk activity, check airflow or enclosure cooling, and recheck the temperature."
WEAR_DRIVE = "Confirm your backups are current and plan a replacement if the drive has reached its rated endurance."


def drive_readings(values):
    return [(label, value) for label, value in values if value is not None]


def drive_health_worker(sampler):
    previous = {}
    observed_request = 0
    deadline = 0
    while not STOP.is_set():
        try:
            request = (STATE / "drive-health-check").stat().st_mtime_ns
        except OSError:
            request = 0
        tick = time.monotonic()
        try:
            inventory = drive_inventory()
            if inventory is not None:
                refresh = tick >= deadline or request != observed_request
                results = {}
                sampler.drive_space = {drive["id"]: drive["space"] for drive in inventory if drive.get("space") is not None}
                for drive in inventory:
                    drive = {key: value for key, value in drive.items() if key != "space"}
                    if STOP.is_set():
                        return
                    old = previous.get(drive["id"])
                    results[drive["id"]] = read_drive_health(drive, old) if refresh or old is None else {**old, **drive}
                sampler.drive_health = list(results.values())
                previous = results
                if refresh:
                    observed_request = request
                    deadline = tick + 300
        except (OSError, ValueError, TypeError) as error:
            print(f"Drive health: {error}", flush=True)
        STOP.wait(5)


def drive_health_events(rows, last):
    events = []
    attached = {drive["id"] for drive in rows}
    for identity in last.keys() - attached:
        del last[identity]
    for drive in rows:
        issues = drive["issues"]
        if issues:
            signature = tuple((level, reason) for level, reason, _ in issues)
            if last.get(drive["id"]) != signature:
                last[drive["id"]] = signature
                severity = 3 if drive["status"] == "Critical" else 4
                source = f'Drive health: {drive["name"]} ({drive["device"]})'
                events.append((severity, "Storage", source, "; ".join(reason for _, reason, _ in issues)))
        elif drive["status"] in ("SMART passed", "Healthy"):
            last.pop(drive["id"], None)
    return events


def drive_inventory():
    if not WINDOWS:
        return None
    data, _ = windows_platform.powershell_json(windows_platform.DRIVE_INVENTORY_SCRIPT, timeout=8)
    if not isinstance(data, list):
        return None
    data.sort(key=lambda row: not row.get("System"))
    return [{"id": f"{row['Number']}:{row.get('Identity') or row.get('Name') or ''}",
             "number": row["Number"], "device": f"Disk {row['Number']}",
             "name": row.get("Name") or f"Disk {row['Number']}",
             "kind": row.get("Bus") or "Storage", "storage": row.get("Storage"),
             "windows_health": row.get("Health"), "space": row.get("Space"),
             "volume_names": row.get("VolumeNames") or []} for row in data]


def read_drive_health(drive, previous):
    counters = None
    health = drive["windows_health"]
    if drive["storage"]:
        data, _ = windows_platform.powershell_json(
            windows_platform.DRIVE_HEALTH_SCRIPT, timeout=8,
            variables={"MONITOR_DISK_NUMBER": str(drive["number"])})
        if isinstance(data, dict):
            counters = data.get("Counters")
            health = data.get("Health")
    counters = counters or {}
    temp = counters.get("Temperature") or None
    wear = counters.get("Wear")
    read_errors = counters.get("ReadErrorsUncorrected")
    write_errors = counters.get("WriteErrorsUncorrected")
    readings = drive_readings([
        ("Temperature", f"{temp} °C" if temp is not None else None),
        ("Endurance used", f"{wear}%" if wear is not None else None),
        ("Uncorrected read errors", read_errors), ("Uncorrected write errors", write_errors),
        ("Power-on hours", counters.get("PowerOnHours"))])
    issues = []
    if health == 2:
        issues.append(("Critical", "Windows reports this drive as unhealthy", REPLACE_DRIVE))
    elif health == 1:
        issues.append(("Warning", "Windows reports a drive health warning", BACK_UP))
    if read_errors or write_errors:
        issues.append(("Warning", f"Uncorrected errors: {read_errors or 0} read, {write_errors or 0} write", BACK_UP))
    if wear is not None and wear >= 100:
        issues.append(("Warning", f"Rated endurance used: {wear}%", WEAR_DRIVE))
    status = {1: "Drive warning", 2: "Drive unhealthy"}.get(
        health, "Healthy" if health == 0 and readings else "Unavailable")
    note = ("Source: Windows storage reliability counters." if readings else
            "Windows did not expose reliability counters for this drive. USB bridges, device drivers, and access permissions can limit what is available.")
    return drive_result(drive, readings, issues, status=status, note=note)


def drive_activity_rows(health, disks):
    counters = {row[0]: row for row in disks}
    activity = {}
    for drive in health:
        names = [Path(drive["device"]).name]
        if drive.get("number") is not None:
            names = [f'PhysicalDrive{drive["number"]}']
        elif drive["kind"] == "NVMe":
            controller = re.fullmatch(r"(nvme\d+)n\d+", names[0])
            if controller:
                names = [name for name in counters if re.fullmatch(controller[1] + r"n\d+", name)]
        rows = [counters[name] for name in names if name in counters]
        if rows:
            activity[drive["id"]] = (
                sum(row[2] for row in rows), sum(row[3] for row in rows),
                max((row[4] for row in rows if row[4] is not None), default=None),
                max((row[5] for row in rows if row[5] is not None), default=None))
    return activity


def run_samples(db, token=None):
    sampler = Sampler()
    sampler.drive_health = None
    sampler.drive_space = {}
    threading.Thread(target=drive_health_worker, args=(sampler,), name="drive-health", daemon=True).start()
    last_drive_alerts = {}
    network = NetworkSampler(STATE, command)
    conditions = {}
    next_sample = next_cleanup = 0
    last_error = None
    traffic_totals = [0.0, 0.0]
    traffic_seconds = 0.0
    retransmit_total = None
    drop_total = link_total = 0
    previous_tick = time.monotonic()
    while not STOP.is_set() and not updater.stop_requested(STATE, token):
        tick = time.monotonic()
        try:
            snapshot, events, gap = sampler.sample()
            snapshot["drive_health"] = sampler.drive_health
            snapshot["drive_space"] = sampler.drive_space
            snapshot["drive_activity"] = drive_activity_rows(sampler.drive_health or [], snapshot["disk"])
            events.extend(drive_health_events(sampler.drive_health or [], last_drive_alerts))
            traffic = network.sample()
            now = snapshot["ts"]
            span = tick - previous_tick
            if 0 < span <= 10:
                for index in range(2):
                    traffic_totals[index] += sum(row[2+index] for row in snapshot["net"] if not is_loopback(row[0])) * span
                traffic_seconds += span
            previous_tick = tick
            snapshot["traffic"] = {key: value for key, value in traffic.items() if key != "peers"}
            retransmits, drops, link_losses = snapshot["network_health"]
            if retransmits is not None:
                retransmit_total = (retransmit_total or 0) + retransmits
            drop_total += drops
            link_total += link_losses
            tests = [
                ("I/O stall", snapshot["pressure"].get("io.full", 0) >= 5, 3, "Storage",
                 "All non-idle tasks were stalled on I/O for at least 5% of three consecutive samples."),
                ("CPU load", snapshot["cpu"] >= 95, 10, "CPU",
                 "CPU utilization stayed above 95% for ten consecutive samples."),
                ("Memory pressure", snapshot["memory"] >= 95 or snapshot["pressure"].get("memory.full", 0) >= 5,
                 5, "Memory", "RAM use reached 95% or full memory pressure reached 5% for five consecutive samples."),
            ]
            for driver, name, value, high, critical in snapshot["temperatures"]:
                limit = high if high and high > 0 else critical
                tests.append((f"{driver} / {name}", bool(limit and value >= limit), 3, "Thermals",
                              f"Temperature stayed at or above its {limit} °C sensor threshold for three samples."))
            for mount, device, filesystem, used, _ in snapshot["mounts"]:
                tests.append((mount, used >= 95, 3, "Storage",
                              f"Filesystem {device} ({filesystem}) at {mount} is {used:.1f}% full."))
            if gap:
                conditions.clear()
            active_tests = set()
            for name, active, threshold, category, message in tests:
                active_tests.add(name)
                previous = conditions.get(name, 0)
                count = min(previous + 1, threshold) if active else 0
                if count == threshold and previous < threshold:
                    events.append((4, category, name, message))
                elif previous == threshold and count == 0:
                    events.append((6, category, name, "Returned below the monitor's alert threshold."))
                conditions[name] = count
            conditions = {name: value for name, value in conditions.items() if name in active_tests}
            snapshot["alerts"] = [name for name, active, threshold, _, _ in tests if conditions.get(name) == threshold]
            if WINDOWS:
                snapshot["scope"] = "Windows · current account"
            else:
                snapshot["scope"] = "Restricted PID view" if read("/proc/1/comm") not in ("systemd", "init") else "Host PID view"
            with db:
                store_peers(db, now, traffic["peers"])
                if last_error:
                    record(db, now, 6, "Monitor", "Collector", "System sampling recovered.")
                    last_error = None
                for event in events:
                    record(db, now, *event)
                if tick >= next_sample:
                    if "io.some" in snapshot["pressure"]:
                        db.execute("INSERT OR REPLACE INTO io_pressure_history VALUES(?,?)",
                                   (int(now), snapshot["pressure"]["io.some"]))
                    cpu_thermal = snapshot.get("cpu_temperature")
                    db.execute("INSERT OR REPLACE INTO cpu_temperature_history VALUES(?,?)",
                               (int(now), cpu_thermal[0] if cpu_thermal else None))
                    db.executemany("INSERT OR REPLACE INTO drive_activity_history VALUES(?,?,?,?)",
                                   [(int(now), key, *data[:2]) for key, data in snapshot["drive_activity"].items()])
                    db.execute("INSERT OR REPLACE INTO samples(ts,cpu,memory,io_full,disk_write,network,disk_read) VALUES(?,?,?,?,?,?,?)",
                               (int(now), snapshot["cpu"], snapshot["memory"], snapshot["pressure"].get("io.full"),
                                sum(row[3] for row in snapshot["disk"]),
                                sum(row[2]+row[3] for row in snapshot["net"] if not is_loopback(row[0])),
                                sum(row[2] for row in snapshot["disk"])))
                    db.executemany("INSERT OR REPLACE INTO gpu_history VALUES(?,?,?,?,?,?,?,?)",
                                   [(int(now), slot, row[0], *row[2:]) for slot, row in enumerate(snapshot["gpu"])])
                    db.execute("INSERT OR REPLACE INTO traffic_history VALUES(?,?,?)",
                               (int(now), *(total / traffic_seconds if traffic_seconds else 0 for total in traffic_totals)))
                    db.execute("INSERT OR REPLACE INTO network_health_history VALUES(?,?,?,?)",
                               (int(now), retransmit_total, drop_total, link_total))
                    traffic_totals = [0.0, 0.0]
                    traffic_seconds = 0.0
                    retransmit_total = None
                    drop_total = link_total = 0
                    next_sample = tick + 15
                if tick >= next_cleanup:
                    db.execute("DELETE FROM io_pressure_history WHERE ts < ?", (now-7*86400,))
                    db.execute("DELETE FROM cpu_temperature_history WHERE ts < ?", (now-7*86400,))
                    db.execute("DELETE FROM drive_activity_history WHERE ts < ?", (now-7*86400,))
                    db.execute("DELETE FROM samples WHERE ts < ?", (now-7*86400,))
                    db.execute("DELETE FROM gpu_history WHERE ts < ?", (now-7*86400,))
                    db.execute("DELETE FROM traffic_history WHERE ts < ?", (now-7*86400,))
                    db.execute("DELETE FROM network_health_history WHERE ts < ?", (now-7*86400,))
                    db.execute("DELETE FROM network_probe_history WHERE ts < ?", (now-7*86400,))
                    db.execute("DELETE FROM network_peers WHERE last < ?", (now-7*86400,))
                    db.execute("DELETE FROM network_peers WHERE rowid IN (SELECT rowid FROM network_peers ORDER BY last DESC LIMIT -1 OFFSET 25000)")
                    db.execute("DELETE FROM events WHERE last < ?", (now-30*86400,))
                    db.execute("DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY last DESC LIMIT -1 OFFSET 25000)")
                    next_cleanup = tick + 300
            write_json("live.json", snapshot)
        except (OSError, ValueError, KeyError, TypeError, psutil.Error, sqlite3.Error) as error:
            message = f"{type(error).__name__}: {error}"
            if message != last_error:
                with db:
                    record(db, time.time(), 3, "Monitor", "Collector", message)
                last_error = message
            if sys.stderr:
                print(f"Sample failed: {message}", file=sys.stderr, flush=True)
        STOP.wait(max(0.15, INTERVAL - (time.monotonic() - tick)))


def main():
    global STATE
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--collect", action="store_true", help="Run the background collector without a window.")
    cli.add_argument("--state-dir", type=Path, default=STATE, help="Override the local history directory.")
    cli.add_argument("--no-start", action="store_true", help="Open the window without starting a collector.")
    args = cli.parse_args()
    STATE = args.state_dir.expanduser().resolve()
    if args.collect:
        signal.signal(signal.SIGTERM, lambda *_: STOP.set())
        signal.signal(signal.SIGINT, lambda *_: STOP.set())
        collector()
        return
    from dashboard import run
    run(STATE, not args.no_start, Path(__file__).resolve())


if __name__ == "__main__":
    # Share the state-dir override with the UI without re-importing a second copy.
    sys.modules["monitor"] = sys.modules[__name__]
    main()
