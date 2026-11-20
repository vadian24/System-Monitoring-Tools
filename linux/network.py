"""Local socket observations and offline IP country lookup."""
from collections import OrderedDict
from datetime import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import time

import psutil

DEFAULT_WATCH = frozenset({"KR", "KP"})
DEFAULT_PROBE_TARGET = "1.1.1.1"
COUNTRY_CODE = re.compile(r"[A-Z]{2}")
OWNER = re.compile(r'"([^"]+)",pid=(\d+)')


def validate_probe_target(text):
    if not text:
        return ""
    address = ipaddress.ip_address(text)
    if address.is_unspecified or address.is_multicast or address.is_link_local or "%" in text:
        raise ValueError("Enter a unicast IPv4 or IPv6 address, or leave it blank to disable the internet probe.")
    return str(address)


def probe_target(state):
    try:
        return validate_probe_target((state / "probe-target").read_text().strip())
    except (OSError, ValueError):
        return DEFAULT_PROBE_TARGET


def save_probe_target(state, text):
    target = validate_probe_target(text.strip())
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = state / f".probe-target-{os.getpid()}.tmp"
    temporary.write_text(target + "\n", encoding="utf-8")
    temporary.replace(state / "probe-target")
    return target


def default_gateway():
    routes = []
    try:
        lines = Path("/proc/net/route").read_text().splitlines()[1:]
    except OSError:
        return ""
    for line in lines:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000" or fields[7] != "00000000":
            continue
        try:
            address = int(fields[2], 16)
            if address and int(fields[3], 16) & 3 == 3:
                gateway = str(ipaddress.IPv4Address(address.to_bytes(4, "little")))
                routes.append((int(fields[6]), gateway))
        except (ValueError, OverflowError):
            continue
    return min(routes)[1] if routes else ""


def ping_metrics(target, command):
    output, code, error = command(["ping", "-n", "-c", "3", "-i", "0.3", "-W", "1", "--", target], timeout=3)
    if "Network is unreachable" in error:
        return None, None, "No route to target"
    if code not in (0, 1):
        return None, None, "Ping unavailable"
    loss = re.search(r"(\d+(?:\.\d+)?)% packet loss", output)
    latency = re.search(r"=\s*[\d.]+/([\d.]+)/[\d.]+/[\d.]+\s+ms", output)
    if not loss:
        return None, None, "No probe result"
    lost = float(loss[1])
    return float(latency[1]) if latency else None, lost, "No replies" if lost == 100 else ""


def connectivity_samples(state, command):
    targets = (("router", default_gateway(), "No IPv4 default gateway"),
               ("internet", probe_target(state), "Disabled"))
    for kind, target, status in targets:
        metrics = ping_metrics(target, command) if target else (None, None, status)
        yield kind, target, *metrics


def tcp_timeout_count():
    try:
        lines = Path("/proc/net/netstat").read_text().splitlines()
        for header, values in zip(lines[::2], lines[1::2]):
            if header.startswith("TcpExt:") and values.startswith("TcpExt:"):
                return int(dict(zip(header.split(), values.split()))["TCPTimeouts"])
    except (OSError, KeyError, ValueError):
        pass
    return None


class NetworkHealthSampler:
    def __init__(self):
        self.timeouts = None
        self.carrier = {}
        self.last = None

    def sample(self, interfaces, previous_interfaces):
        tick = time.clock_gettime(time.CLOCK_BOOTTIME)
        continuous = self.last is not None and tick - self.last <= 10
        self.last = tick
        current_timeouts = tcp_timeout_count()
        timeouts = None
        if continuous and current_timeouts is not None and self.timeouts is not None:
            if current_timeouts >= self.timeouts:
                timeouts = current_timeouts - self.timeouts
        self.timeouts = current_timeouts

        drops = carrier_losses = 0
        carrier = {}
        events = []
        for name, current in interfaces.items():
            if name == "lo":
                continue
            previous = previous_interfaces.get(name)
            if continuous and previous is not None:
                dropped = max(0, current.dropin - previous.dropin) + max(0, current.dropout - previous.dropout)
                drops += dropped
                if dropped:
                    events.append((4, "Network", name, "Interface reported packet drops."))
            device = Path("/sys/class/net") / name
            if not (device / "device").exists():
                continue
            try:
                count = int((device / "carrier_down_count").read_text())
            except (OSError, ValueError):
                continue
            carrier[name] = count
            if continuous and name in self.carrier and count > self.carrier[name]:
                carrier_losses += count - self.carrier[name]
                events.append((4, "Network", name, "Physical network interface lost carrier."))
        self.carrier = carrier
        if timeouts:
            events.append((4, "Network", "TCP", "Linux recorded TCP retransmission timeouts."))
        return timeouts, drops, carrier_losses, events


def watch_codes(value):
    return frozenset(code for code in re.split(r"[\s,]+", value.upper()) if COUNTRY_CODE.fullmatch(code))


def watchlist(state):
    try:
        value = json.loads((state / "network-settings.json").read_text())["countries"]
        return watch_codes(value) if isinstance(value, str) else DEFAULT_WATCH
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_WATCH


def save_watchlist(state, codes):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / "network-settings.json"
    temporary = state / f".network-settings-{os.getpid()}.tmp"
    temporary.write_text(json.dumps({"countries": " ".join(sorted(codes))}), encoding="utf-8")
    temporary.replace(path)


def endpoint(value):
    host, _, port = value.rpartition(":")
    try:
        if not port.isdecimal() or int(port) == 0:
            return None
        address = ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
        if address.is_unspecified:
            return None
        return str(getattr(address, "ipv4_mapped", None) or address), int(port)
    except ValueError:
        return None


def parse_ss(output):
    rows = []
    for line in output.splitlines():
        columns = line.split(None, 6)
        if len(columns) < 6 or columns[0] not in ("tcp", "udp"):
            continue
        protocol, status, _, _, local, remote = columns[:6]
        peer = endpoint(remote)
        if peer is None or status in ("LISTEN", "TIME-WAIT", "CLOSE"):
            continue
        details = columns[6] if len(columns) > 6 else ""
        owners = OWNER.findall(details)
        name, pid = owners[0] if owners else ("", "")
        fields = dict(re.findall(r"\b(ino|sk|bytes_sent|bytes_received):(\w+)", details))
        socket_id = fields.get("sk") or fields.get("ino") or local
        # TCP_INFO omits zero byte fields on a fresh socket. Require TCP_INFO before interpreting absence as zero.
        has_counts = protocol == "tcp" and bool(re.search(r"\b(?:rto|cwnd|mss|bytes_sent|bytes_received):", details))
        sent = int(fields.get("bytes_sent", 0)) if has_counts else None
        received = int(fields.get("bytes_received", 0)) if has_counts else None
        rows.append(((protocol, local, *peer, socket_id), protocol, *peer, name, pid, status, sent, received))
    return rows


class CountryLookup:
    def __init__(self, state, command):
        self.state = state
        self.command = command
        self.cache = OrderedDict()
        self.database = None
        self.signature = None
        self.status = ""
        self.remaining = 0

    def begin(self):
        self.remaining = 12
        for path in (self.state / "GeoLite2-Country.mmdb", Path("/var/lib/GeoIP/GeoLite2-Country.mmdb"),
                     Path("/usr/share/GeoIP/GeoLite2-Country.mmdb")):
            if path.is_file():
                break
        else:
            self.database = None
            self.status = "Country database unavailable"
            return
        if not shutil.which("mmdblookup"):
            self.database = None
            self.status = "mmdblookup unavailable"
            return
        try:
            stamp = path.stat().st_mtime
        except OSError:
            self.database = None
            self.status = "Country database unavailable"
            return
        signature = (path, stamp)
        if self.signature != signature:
            self.cache.clear()
            self.signature = signature
        self.database = path
        age = " · file is over a year old" if datetime.now().timestamp() - stamp > 365 * 86400 else ""
        self.status = f"Offline GeoIP · {datetime.fromtimestamp(stamp):%b %Y} file{age}"

    def lookup(self, host):
        address = ipaddress.ip_address(host)
        if not address.is_global or address.is_multicast:
            return "Local"
        if self.database is None:
            return ""
        if host in self.cache:
            self.cache.move_to_end(host)
            return self.cache[host]
        if not self.remaining:
            return ""
        self.remaining -= 1
        result, status, error = self.command(["mmdblookup", "--file", str(self.database), "--ip", host,
                                               "country", "iso_code"], timeout=0.3)
        match = re.search(r'"([A-Z]{2})"\s+<utf8_string>', result)
        if status and not re.search(r"entry not found|could not find an entry|lookup path does not match", error, re.I):
            self.status = f"Offline GeoIP lookup failed · {error[:120]}"
            return ""
        code = match[1] if match else ""
        self.cache[host] = code
        if len(self.cache) > 4096:
            self.cache.popitem(last=False)
        return code


class NetworkSampler:
    def __init__(self, state, command):
        self.state = state
        self.command = command
        self.geo = CountryLookup(state, command)
        self.previous = {}

    def sample(self):
        output, status, error = self.command(["ss", "-H", "-O", "-n", "-t", "-u", "-a", "-p", "-e", "-i"],
                                             timeout=1)
        if status == 0 and (output.strip() or not error):
            sockets = parse_ss(output)
            coverage = "Partial socket view · " + error.splitlines()[0][:120] if error else "Socket view · process ownership where permitted"
        else:
            try:
                sockets = self.fallback()
                coverage = "Limited socket view · TCP byte counters unavailable"
            except (OSError, psutil.Error):
                sockets = []
                coverage = f"Remote socket data unavailable · {error[:130]}"
        self.geo.begin()
        watched = watchlist(self.state)
        cache = {}
        observed = {}
        for key, protocol, host, port, name, pid, status, sent, received in sockets:
            prior = self.previous.get(key)
            if not name and prior:
                name = prior[2]
            cache[key] = sent, received, name
            sent_delta = max(0, sent-prior[0]) if sent is not None and prior and prior[0] is not None else None
            read_delta = max(0, received-prior[1]) if received is not None and prior and prior[1] is not None else None
            country = self.geo.lookup(host)
            identity = protocol, host, port, name
            row = observed.setdefault(identity, {"country": country, "pids": set(), "states": set(), "sent": None,
                                                  "received": None, "watched": country in watched})
            if pid:
                row["pids"].add(pid)
            row["states"].add(status)
            for metric, delta in (("sent", sent_delta), ("received", read_delta)):
                if delta is not None:
                    row[metric] = (row[metric] or 0) + delta
        self.previous = cache
        rows = [[*key, row["country"], ", ".join(sorted(row["pids"], key=int)), ", ".join(sorted(row["states"])),
                 row["sent"], row["received"], row["watched"]] for key, row in observed.items()]
        return {"peers": rows, "coverage": coverage, "geo": self.geo.status, "countries": sorted(watched)}

    @staticmethod
    def fallback():
        rows = []
        for connection in psutil.net_connections("inet"):
            if not connection.raddr or connection.status in ("TIME_WAIT", "CLOSE"):
                continue
            name = ""
            if connection.pid:
                try:
                    name = psutil.Process(connection.pid).name()
                except psutil.Error:
                    pass
            protocol = "tcp" if connection.type == socket.SOCK_STREAM else "udp"
            address = endpoint(f"{connection.raddr.ip}:{connection.raddr.port}")
            if address:
                pid = str(connection.pid) if connection.pid is not None else ""
                key = protocol, connection.laddr, *address, pid, connection.fd
                rows.append((key, protocol, *address, name, pid, connection.status, None, None))
        return rows
