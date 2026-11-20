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


def linux_gpu_worker(sampler):
    previous = {}
    last = time.monotonic()
    while not STOP.is_set():
        rows_by_pci = nvidia_gpu()
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
        sampler.gpu_metrics = rows, ("Display adapters detected. Readings depend on the graphics driver."
                                     if rows else "No display adapters detected")
        STOP.wait(4)


class Sampler:
    def __init__(self):
        self.gpu_metrics = [], "Checking display adapters"
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
        gpus, gpu_status = self.gpu_metrics
        return dict(ts=time.time(), cpu=cpu, cores=cores, memory=memory.percent, ram_used=memory.total-memory.available,
                    ram_total=memory.total, swap=swap.percent, pressure=psi, disk=drives, net=interfaces, gpu=gpus,
                    gpu_status=gpu_status, drm=drm_metrics(), processes=processes, inventory=self.inventory,
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


def run_samples(db, token=None):
    sampler = Sampler()
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
