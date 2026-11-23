#!/usr/bin/python3
"""Local Linux telemetry collector and Qt desktop launcher."""
import argparse
from collections import Counter
import csv
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

import psutil

import updater

from network import NetworkHealthSampler, NetworkSampler, connectivity_samples

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
                                timeout=timeout, env={**os.environ, "LC_ALL": "C"})
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
            disk_write REAL, network REAL
        );
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
            ts INTEGER PRIMARY KEY, timeouts INTEGER, drops INTEGER NOT NULL,
            carrier_losses INTEGER NOT NULL
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


def nvidia_gpu():
    if not Path("/proc/driver/nvidia/version").exists():
        return {}
    result, status, _ = command(["nvidia-smi",
                                 "--query-gpu=pci.bus_id,name,driver_version,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw",
                                 "--format=csv,noheader,nounits"])
    rows = {}
    for row in csv.reader(result.splitlines(), skipinitialspace=True):
        if status == 0 and len(row) == 8:
            values = []
            for field in row[3:]:
                try:
                    values.append(float(field))
                except ValueError:
                    values.append(None)
            rows[row[0][-12:].lower()] = [*row[1:3], *values]
    return rows


@lru_cache(maxsize=32)
def gpu_name(device_id, fallback):
    if re.fullmatch(r"[\da-f]{4}:[\da-f]{2}:[\da-f]{2}\.\d", device_id, re.I):
        result, status, _ = command(["lspci", "-mm", "-s", device_id])
        if status == 0 and result:
            try:
                fields = shlex.split(result.splitlines()[0])
            except ValueError:
                fields = []
            if len(fields) >= 4:
                return " ".join(fields[2:4])
    return fallback


def read_number(path, divisor=1):
    value = read(path)
    try:
        return int(value) / divisor
    except ValueError:
        return None


def drm_clients(devices):
    clients = {}
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            with os.scandir(proc / "fd") as entries:
                for fd in entries:
                    try:
                        target = os.readlink(fd.path)
                    except OSError:
                        continue
                    device = devices.get(target)
                    if device is None:
                        continue
                    fields = dict(line.split(":\t", 1) for line in read(proc / "fdinfo" / fd.name).splitlines()
                                  if ":\t" in line and line.startswith("drm-"))
                    client = fields.get("drm-client-id")
                    if client is not None:
                        clients.setdefault((device, client), fields)
        except OSError:
            continue
    return clients


def engine_activity(clients, previous, elapsed):
    def count(fields, key):
        value = fields.get(key, "").split()
        return int(value[0]) if value and value[0].isdecimal() else None

    loads = {}
    if elapsed <= 0:
        return {}
    for (device, client), fields in clients.items():
        old = previous.get((device, client), {})
        for key in fields:
            if key.startswith("drm-engine-") and not key.startswith("drm-engine-capacity-"):
                engine = key.removeprefix("drm-engine-")
                capacity = count(fields, "drm-engine-capacity-" + engine) or 1
                interval = elapsed * 1e9 * capacity
            elif key.startswith("drm-cycles-"):
                engine = key.removeprefix("drm-cycles-")
                if "drm-engine-" + engine in fields:
                    continue
                total_key = "drm-total-cycles-" + engine
                current, baseline = count(fields, total_key), count(old, total_key)
                if current is None or baseline is None:
                    continue
                interval = current - baseline
            else:
                continue
            current, baseline = count(fields, key), count(old, key)
            if current is not None and baseline is not None and current >= baseline and interval > 0:
                slot = (device, engine)
                loads[slot] = loads.get(slot, 0) + 100 * (current - baseline) / interval
    busy = {}
    for (device, _), value in loads.items():
        busy[device] = min(100, max(busy.get(device, 0), value))
    return busy


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


def linux_gpu_worker(sampler):
    previous = {}
    last = time.monotonic()
    while not STOP.is_set():
        rows_by_pci = nvidia_gpu()
        thermal = {pci: data for pci, (_, data) in nvidia_thermal("nvidia-smi").items()} if rows_by_pci else {}
        devices = {}
        pending = {}
        for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
            device = card / "device"
            if not re.fullmatch(r"card\d+", card.name) or not device.exists():
                continue
            pci = device.resolve().name
            if pci in rows_by_pci:
                continue
            driver_link = device / "driver"
            driver = driver_link.resolve().name if driver_link.is_symlink() else "—"
            name = gpu_name(pci, f"{driver} ({card.name})" if driver != "—" else card.name)
            util = read_number(device / "gpu_busy_percent")
            used = read_number(device / "mem_info_vram_used", 2**20)
            total = read_number(device / "mem_info_vram_total", 2**20)
            hwmon = next(iter(sorted(device.glob("hwmon/hwmon*"))), None)
            temp = read_number(hwmon / "temp1_input", 1000) if hwmon else None
            power = read_number(hwmon / "power1_average", 10**6) if hwmon else None
            if hwmon:
                thermal[pci] = (read_number(hwmon / "temp1_max", 1000), read_number(hwmon / "temp1_crit", 1000), None)
            if hwmon and power is None:
                power = read_number(hwmon / "power1_input", 10**6)
            row = [name, driver, util, temp, used, total, power]
            if util is None:
                pending[pci] = row
                devices[f"/dev/dri/{card.name}"] = pci
                for render in card.glob("device/drm/renderD*"):
                    devices[f"/dev/dri/{render.name}"] = pci
            rows_by_pci[pci] = row
        tick = time.monotonic()
        clients = drm_clients(devices) if devices else {}
        for pci, utilization in engine_activity(clients, previous, tick - last).items():
            pending[pci][2] = utilization
        previous, last = clients, tick
        names = Counter(row[0] for row in rows_by_pci.values())
        for pci, row in rows_by_pci.items():
            if names[row[0]] > 1:
                row[0] = f"{row[0]} ({pci})"
        rows = list(rows_by_pci.values())
        status_by_name = {row[0]: thermal[pci] for pci, row in rows_by_pci.items() if pci in thermal}
        sampler.gpu_metrics = rows, status_by_name, ("Display adapters detected. Readings depend on the graphics driver."
                                     if rows else "No display adapters detected")
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
        self.gpu_metrics = [], {}, "Checking display adapters"
        threading.Thread(target=linux_gpu_worker, args=(self,), name="graphics", daemon=True).start()
        self.last = time.monotonic()
        self.disk = psutil.disk_io_counters(perdisk=True, nowrap=False) or {}
        self.net = psutil.net_io_counters(pernic=True, nowrap=False) or {}
        self.network_health = NetworkHealthSampler()
        self.psi = {}
        self.proc_io = {}
        self.inventory = []
        self.mounts = []
        self.services = []
        self.system_status = self.user_status = "Checking"
        self.known_services = {}
        self.next_inventory = self.next_services = 0
        psutil.cpu_percent()
        psutil.cpu_percent(percpu=True)

    def sample(self):
        tick = time.monotonic()
        elapsed = max(0.01, tick - self.last)
        gap = elapsed > 10
        self.last = tick
        cpu = psutil.cpu_percent()
        cores = psutil.cpu_percent(percpu=True)
        memory, swap = psutil.virtual_memory(), psutil.swap_memory()
        psi, self.psi = pressure({} if gap else self.psi, elapsed)
        disk = psutil.disk_io_counters(perdisk=True, nowrap=False) or {}
        drives = []
        for node in sorted(Path("/sys/block").glob("*")):
            name = node.name
            if name.startswith(("loop", "ram", "zram", "dm-")) or name not in disk:
                continue
            current, old = disk[name], self.disk.get(name, disk[name])
            writes = max(0, current.write_count - old.write_count)
            drives.append([name, read(node/"device/model") or name,
                           max(0, current.read_bytes-old.read_bytes)/elapsed,
                           max(0, current.write_bytes-old.write_bytes)/elapsed,
                           min(100, max(0, current.busy_time-old.busy_time)/elapsed/10),
                           max(0, current.write_time-old.write_time)/writes if writes else None])
        self.disk = disk
        net = psutil.net_io_counters(pernic=True, nowrap=False) or {}
        timeouts, drops, carrier_losses, network_events = self.network_health.sample(net, self.net)
        interfaces = []
        for name, current in sorted(net.items()):
            old = self.net.get(name, current)
            link = read(Path("/sys/class/net") / name / "operstate")
            interfaces.append([name, link.capitalize() or "Unknown",
                               max(0, current.bytes_recv-old.bytes_recv)/elapsed,
                               max(0, current.bytes_sent-old.bytes_sent)/elapsed,
                               max(0, current.errin+current.errout-old.errin-old.errout)])
        self.net = net
        processes = []
        io_cache = {}
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
                processes.append([proc.pid, info["name"] or "—", info["username"] or "—",
                                  proc.cpu_percent(), info["memory_info"].rss if info["memory_info"] else None,
                                  write, info["status"] or "—"])
            except (psutil.Error, OSError):
                continue
        self.proc_io = io_cache
        temperatures = [[name, item.label or f"Sensor {i+1}", item.current, item.high, item.critical]
                        for name, items in sorted(psutil.sensors_temperatures().items())
                        for i, item in enumerate(items)]
        fans = [[name, item.label or f"Fan {i+1}", item.current]
                for name, items in sorted(psutil.sensors_fans().items())
                for i, item in enumerate(items)]
        if tick >= self.next_inventory:
            self.inventory = inventory()
            self.mounts = mount_usage()
            self.next_inventory = tick + 45
        changed = []
        if tick >= self.next_services:
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
        gpus, gpu_thermal, gpu_status = self.gpu_metrics
        return dict(ts=time.time(), cpu_temperature=cpu_temperature(temperatures), cpu=cpu, cores=cores, memory=memory.percent, ram_used=memory.total-memory.available,
                    ram_total=memory.total, swap=swap.percent, pressure=psi, disk=drives, net=interfaces, gpu=gpus,
                    gpu_status=gpu_status, gpu_thermal=gpu_thermal, drm=drm_metrics(), processes=processes, inventory=self.inventory,
                    mounts=self.mounts, fans=fans,
                    temperatures=temperatures, services=self.services, system_status=self.system_status,
                    user_status=self.user_status, uptime=max(0, time.time()-psutil.boot_time()),
                    network_health=(timeouts, drops, carrier_losses)), changed + network_events, gap


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
            journal = threading.Thread(target=journal_worker, name="journal", daemon=True)
            probes = threading.Thread(target=probe_worker, name="network-probes", daemon=True)
            journal.start()
            probes.start()
            try:
                run_samples(db, token)
            finally:
                STOP.set()
                journal.join(timeout=7)
                probes.join(timeout=7)


def probe_worker():
    with database() as db:
        while not STOP.is_set():
            start = time.clock_gettime(time.CLOCK_BOOTTIME)
            rows = list(connectivity_samples(STATE, command))
            elapsed = time.clock_gettime(time.CLOCK_BOOTTIME) - start
            if elapsed < 8 and not STOP.is_set():
                stamp = int(time.time())
                try:
                    with db:
                        db.executemany("INSERT OR REPLACE INTO network_probe_history VALUES(?,?,?,?,?,?)",
                                       [(stamp, *row) for row in rows])
                except sqlite3.Error as error:
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


def mounted_volume_space(nodes, group, owners, volumes, labels):
    hosts_root = False
    for node in nodes:
        identity = node.get("uuid") or node["name"]
        owners.setdefault(identity, set()).add(group)
        used, available = node.get("fsused"), node.get("fsavail")
        if used is not None and available is not None:
            volumes[identity] = (used, available)
        mountpoints = node.get("mountpoints") or []
        name = (node.get("label") or "").strip()
        system_partition = node.get("parttype") in (
            "0xef", "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "de94bba4-06d1-4d40-a16a-bfd50179d6ac")
        if name and not system_partition and node.get("fstype") != "swap" and "[SWAP]" not in mountpoints:
            priority = 0 if any(path and path.startswith("/") and not path.startswith("/boot")
                                for path in mountpoints) else 1
            labels[name] = min(priority, labels.get(name, priority))
        child_hosts_root = mounted_volume_space(node.get("children") or [], group, owners, volumes, labels)
        hosts_root = hosts_root or child_hosts_root or "/" in mountpoints
    return hosts_root


def drive_inventory():
    fields = "NAME,TYPE,MODEL,TRAN,ROTA,SERIAL,UUID,FSUSED,FSAVAIL,MOUNTPOINTS,LABEL,FSTYPE,PARTTYPE"
    result, code, _ = command(["lsblk", "--json", "--bytes", "--output", fields])
    if code:
        return None
    drives, owners, volumes, labels_by_drive = {}, {}, {}, {}
    system_groups = set()
    for disk in json.loads(result).get("blockdevices", []):
        name = disk["name"]
        if disk["type"] != "disk" or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name.startswith(("ram", "zram")):
            continue
        controller = re.fullmatch(r"(nvme\d+)n\d+", name)
        device = f"/dev/{name}"
        group = controller[1] if controller else name
        sequence = read(Path("/sys/class/block") / name / "diskseq")
        identity = f"{group}:{disk.get('serial') or sequence or name}"
        kind = "NVMe" if controller else "USB" if disk.get("tran") == "usb" else "HDD" if disk.get("rota") else "SSD / flash"
        drives.setdefault(group, {"id": identity, "name": (disk.get("model") or name).strip(),
                                  "kind": kind, "device": device})
        if mounted_volume_space([disk], group, owners, volumes, labels_by_drive.setdefault(group, {})):
            system_groups.add(group)
    for group, drive in drives.items():
        labels = labels_by_drive[group]
        drive["volume_names"] = sorted(labels, key=labels.get)
        filesystems = [space for key, space in volumes.items() if owners[key] == {group}]
        if filesystems:
            drive["space"] = tuple(map(sum, zip(*filesystems)))
    return [drives[group] for group in sorted(drives, key=lambda group: group not in system_groups)]


def udisks_drive(drive):
    output, code, _ = command(["busctl", "--system", "--json=short", "call", "org.freedesktop.UDisks2",
                               "/org/freedesktop/UDisks2", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"])
    if code:
        return None
    objects, = json.loads(output)["data"]
    block = objects.get("/org/freedesktop/UDisks2/block_devices/" + Path(drive["device"]).name, {})
    properties = block.get("org.freedesktop.UDisks2.Block", {})
    path = properties.get("Drive", {}).get("data")
    if not path or path == "/":
        return None
    for protocol in ("Drive.Ata", "NVMe.Controller"):
        interface = "org.freedesktop.UDisks2." + protocol
        if interface in objects.get(path, {}):
            return path, interface
    return None


def udisks_call(path, interface, method, *options):
    return command(["busctl", "--system", "--allow-interactive-authorization=no", "--json=short", "call",
                    "org.freedesktop.UDisks2", path, interface, method, "a{sv}", *options], timeout=5)


def udisks_read_health(drive, previous):
    endpoint = udisks_drive(drive)
    if not endpoint:
        return None
    path, interface = endpoint
    is_ata = interface.endswith(".Ata")
    _, update_code, error = udisks_call(path, interface, "SmartUpdate", *(
        ("1", "nowakeup", "b", "true") if is_ata else ("0",)))
    sleeping = is_ata and update_code and ("sleep" in error.lower() or "wake" in error.lower())
    raw, code, _ = command(["busctl", "--system", "--json=short", "call", "org.freedesktop.UDisks2", path,
                            "org.freedesktop.DBus.Properties", "GetAll", "s", interface])
    if code:
        return sleeping_drive(drive, previous) if sleeping else None
    properties, = json.loads(raw)["data"]
    values = {key: item["data"] for key, item in properties.items()}
    checked = values.get("SmartUpdated")
    if not checked:
        return sleeping_drive(drive, previous) if sleeping else None
    temp = values.get("SmartTemperature")
    temp = round(temp - 273.15, 1) if temp else None
    if is_ata:
        bad = max(0, values.get("SmartNumBadSectors", 0))
        issues = []
        failing = values.get("SmartFailing")
        health_check = "SMART failed" if failing else "SMART passed" if failing is False else "Unavailable"
        if failing:
            issues.append(("Critical", "SMART health check failed", REPLACE_DRIVE))
        if bad:
            issues.append(("Warning", f"{bad} bad sectors reported", BACK_UP))
        raw, code, _ = udisks_call(path, interface, "SmartGetAttributes", "1", "nowakeup", "b", "true")
        attributes = {}
        if not code:
            rows, = json.loads(raw)["data"]
            attributes = {row[1].lower().replace("_", "-"): row[6] for row in rows if row[7] == 3 and row[6] >= 0}
        for attr, label in (("current-pending-sector", "Pending sectors"),
                            ("reallocated-sector-count", "Reallocated sectors"),
                            ("offline-uncorrectable", "Uncorrectable sectors")):
            if attributes.get(attr):
                level = "Critical" if label == "Uncorrectable sectors" else "Warning"
                issues.append((level, f"{label}: {attributes[attr]}", BACK_UP))
        readings = drive_readings([("Temperature", f"{temp} °C" if temp is not None else None),
                                   ("Bad sectors", bad), ("Pending sectors", attributes.get("current-pending-sector")),
                                   ("Reallocated sectors", attributes.get("reallocated-sector-count")),
                                   ("Uncorrectable sectors", attributes.get("offline-uncorrectable"))])
    else:
        raw, code, _ = udisks_call(path, interface, "SmartGetAttributes", "0")
        health = {}
        if not code:
            report, = json.loads(raw)["data"]
            health = {key: item["data"] for key, item in report.items()}
        warnings = values.get("SmartCriticalWarning")
        health_check = "SMART warning" if warnings == ["temperature"] else (
            "SMART failed" if warnings else "SMART passed" if warnings == [] else "Unavailable")
        issues = nvme_issues(warnings, health.get("media_errors"), health.get("percent_used"))
        wear = health.get("percent_used")
        threshold = health.get("wctemp")
        if not warnings and temp is not None and threshold and values["SmartTemperature"] >= threshold:
            issues.append(("Warning", "Temperature reached the drive's warning threshold", COOL_DRIVE))
        readings = drive_readings([("Temperature", f"{temp} °C" if temp is not None else None),
                                   ("Endurance used", f"{wear}%" if wear is not None else None),
                                   ("Media errors", health.get("media_errors"))])
    result = drive_result(drive, readings, issues, status=health_check, checked=checked, note="Source: UDisks SMART data.")
    return sleeping_drive(drive, result) if sleeping else result


def sleeping_drive(drive, previous):
    return drive_result(drive, previous["readings"] if previous else (),
                        previous["issues"] if previous else (), status="Sleeping",
                        checked=previous["checked"] if previous else 0,
                        note="The check left this drive asleep. Any readings shown are from its last successful check.")


def nvme_issues(warnings, errors, wear):
    issues = []
    if warnings:
        temperature_only = warnings == ["temperature"] or warnings == 2
        reason = "NVMe temperature warning" if temperature_only else (
            "NVMe critical warning: " + ", ".join(warnings) if isinstance(warnings, list)
            else f"NVMe critical warning flags: 0x{warnings:02x}")
        issues.append(("Warning" if temperature_only else "Critical", reason, COOL_DRIVE if temperature_only else REPLACE_DRIVE))
    if errors:
        issues.append(("Warning", f"Media and data integrity errors: {errors}", BACK_UP))
    if wear is not None and wear >= 100:
        issues.append(("Warning", f"Rated endurance used: {wear}%", WEAR_DRIVE))
    return issues


def read_drive_health(drive, previous):
    try:
        udisks = udisks_read_health(drive, previous)
    except (KeyError, TypeError, ValueError):
        udisks = None
    if udisks:
        return udisks
    # On USB, querying unknown bridges can wake a sleeping disk before a power check.
    if drive["kind"] == "USB":
        return drive_result(drive, note="SMART data is unavailable through this USB device. For an external HDD or SSD, try an enclosure that supports SMART passthrough.")
    args = ["smartctl", "-j", "-H", "-A"]
    if drive["kind"] == "HDD":
        args += ["-n", "standby,3,5"]
    raw, code, _ = command([*args, drive["device"]], timeout=5)
    if drive["kind"] == "HDD" and code == 3:
        return sleeping_drive(drive, previous)
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    smart = data.get("smart_status", {}).get("passed")
    nvme = data.get("nvme_smart_health_information_log", {})
    if smart is None and not nvme:
        return drive_result(drive, note="SMART data could not be read. The device may not support SMART or require additional access through UDisks or smartmontools.")
    temp = data.get("temperature", {}).get("current")
    wear, errors = nvme.get("percentage_used"), nvme.get("media_errors")
    issues = nvme_issues(nvme.get("critical_warning"), errors, wear)
    if smart is False and not nvme.get("critical_warning"):
        issues.append(("Critical", "SMART health check failed", REPLACE_DRIVE))
    readings = drive_readings([("Temperature", f"{temp} °C" if temp is not None else None),
                               ("Endurance used", f"{wear}%" if wear is not None else None), ("Media errors", errors)])
    attributes = {row["name"]: row.get("raw", {}).get("value") for row in data.get("ata_smart_attributes", {}).get("table", [])}
    for attr, label in (("Current_Pending_Sector", "Pending sectors"),
                        ("Reallocated_Sector_Ct", "Reallocated sectors"),
                        ("Offline_Uncorrectable", "Uncorrectable sectors")):
        count = attributes.get(attr)
        if count is not None:
            readings.append((label, count))
        if count:
            level = "Critical" if label == "Uncorrectable sectors" else "Warning"
            issues.append((level, f"{label}: {count}", BACK_UP))
    flags = nvme.get("critical_warning")
    if flags == 2:
        status = "SMART warning"
    elif flags or smart is False:
        status = "SMART failed"
    else:
        status = "SMART passed" if smart is True else "Unavailable"
    return drive_result(drive, readings, issues, status=status, note="Source: smartmontools.")


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
    timeout_total = None
    drop_total = carrier_total = 0
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
                    traffic_totals[index] += sum(row[2+index] for row in snapshot["net"] if row[0] != "lo") * span
                traffic_seconds += span
            previous_tick = tick
            snapshot["traffic"] = {key: value for key, value in traffic.items() if key != "peers"}
            timeouts, drops, carrier_losses = snapshot["network_health"]
            if timeouts is not None:
                timeout_total = (timeout_total or 0) + timeouts
            drop_total += drops
            carrier_total += carrier_losses
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
            snapshot["scope"] = "Restricted PID view" if read("/proc/1/comm") not in ("systemd", "init") else "Host PID view"
            with db:
                store_peers(db, now, traffic["peers"])
                if last_error:
                    record(db, now, 6, "Monitor", "Collector", "System sampling recovered.")
                    last_error = None
                for event in events:
                    record(db, now, *event)
                if tick >= next_sample:
                    cpu_thermal = snapshot.get("cpu_temperature")
                    db.execute("INSERT OR REPLACE INTO cpu_temperature_history VALUES(?,?)",
                               (int(now), cpu_thermal[0] if cpu_thermal else None))
                    db.executemany("INSERT OR REPLACE INTO drive_activity_history VALUES(?,?,?,?)",
                                   [(int(now), key, *data[:2]) for key, data in snapshot["drive_activity"].items()])
                    db.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?)",
                               (int(now), snapshot["cpu"], snapshot["memory"], snapshot["pressure"].get("io.full"),
                                sum(row[3] for row in snapshot["disk"]),
                                sum(row[2]+row[3] for row in snapshot["net"] if row[0] != "lo")))
                    db.executemany("INSERT OR REPLACE INTO gpu_history VALUES(?,?,?,?,?,?,?,?)",
                                   [(int(now), slot, row[0], *row[2:]) for slot, row in enumerate(snapshot["gpu"])])
                    db.execute("INSERT OR REPLACE INTO traffic_history VALUES(?,?,?)",
                               (int(now), *(total / traffic_seconds if traffic_seconds else 0 for total in traffic_totals)))
                    db.execute("INSERT OR REPLACE INTO network_health_history VALUES(?,?,?,?)",
                               (int(now), timeout_total, drop_total, carrier_total))
                    traffic_totals = [0.0, 0.0]
                    traffic_seconds = 0.0
                    timeout_total = None
                    drop_total = carrier_total = 0
                    next_sample = tick + 15
                if tick >= next_cleanup:
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
