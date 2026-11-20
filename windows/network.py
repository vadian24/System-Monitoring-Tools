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
import sys

import psutil

WINDOWS = sys.platform == "win32"
if WINDOWS:
    try:
        import maxminddb
    except ImportError:
        maxminddb = None

DEFAULT_PROBE_TARGET = "1.1.1.1"
DEFAULT_WATCH = frozenset({"KR", "KP"})
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
        self.reader = None
        self.reader_signature = None

    def begin(self):
        self.remaining = 12
        candidates = [self.state / "GeoLite2-Country.mmdb"]
        if not WINDOWS:
            candidates += [Path("/var/lib/GeoIP/GeoLite2-Country.mmdb"), Path("/usr/share/GeoIP/GeoLite2-Country.mmdb")]
        for path in candidates:
            if path.is_file():
                break
        else:
            self.database = None
            self.status = "Country database unavailable"
            return
        if not WINDOWS and not shutil.which("mmdblookup"):
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
        if WINDOWS:
            if maxminddb is None:
                self.database = None
                self.status = "MaxMind reader unavailable"
                return
            if self.reader is None or signature != self.reader_signature:
                if self.reader is not None:
                    self.reader.close()
                try:
                    # Keep the country database in memory so the original file can be updated.
                    self.reader = maxminddb.open_database(str(path), mode=maxminddb.MODE_MEMORY)
                    self.reader_signature = signature
                except (OSError, ValueError) as error:
                    self.reader = None
                    self.database = None
                    self.status = f"Offline GeoIP database unreadable · {error}"
                    return
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
        if WINDOWS:
            try:
                entry = self.reader.get(host) or {}
                code = entry.get("country", {}).get("iso_code", "")
            except (OSError, ValueError):
                code = ""
        else:
            code = self.command_lookup(host)
        self.cache[host] = code
        if len(self.cache) > 4096:
            self.cache.popitem(last=False)
        return code

    def command_lookup(self, host):
        result, status, error = self.command(["mmdblookup", "--file", str(self.database), "--ip", host,
                                               "country", "iso_code"], timeout=0.3)
        match = re.search(r'"([A-Z]{2})"\s+<utf8_string>', result)
        if status and not re.search(r"entry not found|could not find an entry|lookup path does not match", error, re.I):
            self.status = f"Offline GeoIP lookup failed · {error[:120]}"
            return ""
        return match[1] if match else ""


class NetworkSampler:
    def __init__(self, state, command):
        self.state = state
        self.command = command
        self.geo = CountryLookup(state, command)
        self.previous = {}

    def sample(self):
        if WINDOWS:
            output, status, error = "", -1, "Network access restricted"
        else:
            output, status, error = self.command(["ss", "-H", "-O", "-n", "-t", "-u", "-a", "-p", "-e", "-i"],
                                                 timeout=1)
        if status == 0 and (output.strip() or not error):
            sockets = parse_ss(output)
            coverage = "Partial socket view · " + error.splitlines()[0][:120] if error else "Socket view · process ownership where permitted"
        else:
            try:
                sockets = self.fallback()
                coverage = "Windows socket view · per-peer byte counters unavailable" if WINDOWS else "Limited socket view · TCP byte counters unavailable"
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
