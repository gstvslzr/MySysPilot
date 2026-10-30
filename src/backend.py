#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MySysPilot v1.4.1 — Linux system optimizer, monitor, disk utility, duplicate finder
and internet speed test, in one local dashboard.

Architecture
  * One self-contained Python 3 file (stdlib only). It runs a small HTTP server bound to
    127.0.0.1 and opens the embedded dashboard UI in a browser app window.
  * Every /api/ call requires a random per-launch token (header X-MSP-Token) and a
    loopback Host header (blocks CSRF / DNS-rebinding from other web pages).
  * Privileged actions go through pkexec (polkit password prompt) or through the
    UDisks2 D-Bus API with interactive polkit authorization (same path GNOME Disks uses).

Optional components (feature degrades gracefully when missing)
  * python3-gi (PyGObject)  -> Disk Utility write actions, SMART, benchmark, imaging
  * udisks2                 -> Disk Utility (installed by default on Ubuntu desktop)
  * smartmontools           -> detailed SMART attribute tables (smartctl)
  * snapd / flatpak         -> Snap / Flatpak tabs in the Uninstaller

Data leaving this machine (nothing else is sent anywhere):
  * Docker install (only when you click Install): apt downloads from download.docker.com or your distro mirrors.
  * Speed Test: the LibreSpeed public server list (librespeed.org) and test traffic
    to the server you select (Cloudflare speed endpoints or a LibreSpeed server).
    Your public IP is visible to that server, as with any speed test. No results are uploaded.

Usage
  python3 MySysPilot_v1.0.py                 # start and open the app window
  python3 MySysPilot_v1.0.py --no-browser    # start server only, print URL
  python3 MySysPilot_v1.0.py --port 8787
  python3 MySysPilot_v1.0.py --install-desktop   # add an app-menu launcher (~/.local)
Do NOT run it with sudo; it asks for elevation per action.
"""

import argparse
import collections
import configparser
import fnmatch
import glob
import hashlib
import http.client
import json
import mmap
import os
import platform
import pwd
import re
import secrets
import shutil
import signal
import socket
import ssl
import stat as statmod
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "MySysPilot"
APP_VERSION = "1.4.1"
APP_BUILD = "2026-10-16"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"

HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local", "share")), "mysyspilot")
LIBRESPEED_LIST = "https://librespeed.org/backend-servers/servers.php"

TOKEN = secrets.token_urlsafe(24)
ALLOWED_HOSTS = set()
PORT = 0
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
NCPU = os.cpu_count() or 1

UI_HTML = "__UI_HTML_PLACEHOLDER__"

# --------------------------------------------------------------------------- helpers


class ApiError(Exception):
    pass


def which(name):
    p = shutil.which(name)
    if p:
        return p
    for d in ("/usr/sbin", "/sbin", "/usr/bin", "/bin", "/snap/bin"):
        c = os.path.join(d, name)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def run(cmd, timeout=60, input_text=None, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           input=input_text, env=env)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def priv(cmd, timeout=1800):
    """Run a command as root through pkexec (polkit GUI prompt). cmd[0] is resolved to an absolute path."""
    exe = which(cmd[0]) or cmd[0]
    cmd = [exe] + list(cmd[1:])
    if os.geteuid() == 0:
        return run(cmd, timeout)
    pk = which("pkexec")
    if not pk:
        return 126, "", "pkexec is not installed (sudo apt install pkexec)"
    rc, out, err = run([pk] + cmd, timeout)
    if rc in (126, 127) and not out:
        err = (err or "").strip() or "Authorization was cancelled or denied."
    return rc, out, err


def priv_ok(cmd, timeout=1800):
    rc, out, err = priv(cmd, timeout)
    if rc != 0:
        raise ApiError((err or out or f"exit {rc}").strip()[-1500:])
    return out


def read_file(path, default=""):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def path_size(path):
    """Apparent size of a file or directory tree (no symlink following)."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not statmod.S_ISDIR(st.st_mode):
        return st.st_size
    total = 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def ensure_data_dir():
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)


# --------------------------------------------------------------------------- jobs

JOBS = {}
JOBS_LOCK = threading.Lock()


def job_start(kind, fn, *args):
    jid = secrets.token_hex(6)
    job = {"id": jid, "kind": kind, "state": "running", "progress": 0.0, "message": "Starting…",
           "result": None, "error": None, "started": time.time(), "cancel": False, "live": {}, "_hashed": 0}
    with JOBS_LOCK:
        JOBS[jid] = job
        # keep the table small
        if len(JOBS) > 60:
            for k in sorted(JOBS, key=lambda k: JOBS[k]["started"])[:20]:
                if JOBS[k]["state"] != "running":
                    JOBS.pop(k, None)

    def wrap():
        try:
            job["result"] = fn(job, *args)
            job["state"] = "cancelled" if job["cancel"] else "done"
            if job["state"] == "done":
                job["progress"] = 1.0
        except Exception as e:  # noqa
            job["state"] = "error"
            job["error"] = str(e) or e.__class__.__name__
    threading.Thread(target=wrap, daemon=True, name=f"job-{kind}").start()
    return jid


def cmd_job(job, cmd, use_priv=True, env=None):
    job["message"] = "Waiting for authorization…" if use_priv else "Running…"
    if use_priv:
        rc, out, err = priv(cmd)
    else:
        rc, out, err = run(cmd, timeout=3600, env=env)
    if rc != 0:
        raise ApiError((err or out or f"exit {rc}").strip()[-2000:])
    job["message"] = "Completed"
    return {"output": (out or "")[-6000:]}


# --------------------------------------------------------------------------- system stats


def _cpu_times():
    out = []
    for line in read_file("/proc/stat").splitlines():
        if line.startswith("cpu"):
            parts = line.split()
            vals = [int(x) for x in parts[1:9]]
            idle = vals[3] + vals[4]
            out.append((parts[0], sum(vals), idle))
    return out


def _net_bytes():
    rx = tx = 0
    for line in read_file("/proc/net/dev").splitlines()[2:]:
        if ":" not in line:
            continue
        name, data = line.split(":", 1)
        name = name.strip()
        if name == "lo" or name.startswith(("veth", "docker", "br-", "virbr")):
            continue
        f = data.split()
        rx += int(f[0])
        tx += int(f[8])
    return rx, tx


def _disk_io():
    disks = set()
    try:
        for n in os.listdir("/sys/block"):
            if n.startswith(("loop", "ram", "zram", "dm-", "md", "sr")):
                continue
            disks.add(n)
    except OSError:
        pass
    r = w = 0
    for line in read_file("/proc/diskstats").splitlines():
        f = line.split()
        if len(f) > 9 and f[2] in disks:
            r += int(f[5]) * 512
            w += int(f[9]) * 512
    return r, w


def _meminfo():
    m = {}
    for line in read_file("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        try:
            m[k] = int(v.split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    return m


def _temps():
    temps = []
    cpu_temp = None
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = read_file(os.path.join(hw, "name")).strip()
        for tin in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            try:
                c = int(read_file(tin).strip()) / 1000.0
            except ValueError:
                continue
            if c <= -40 or c > 150:
                continue
            label = read_file(tin.replace("_input", "_label")).strip() or name
            temps.append({"sensor": name, "label": label, "c": round(c, 1)})
            if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz") and cpu_temp is None:
                if name != "coretemp" or label.lower().startswith("package") or "tctl" in label.lower():
                    cpu_temp = c
    if cpu_temp is None:
        for t in temps:
            if t["sensor"] in ("coretemp", "k10temp", "zenpower"):
                cpu_temp = t["c"]
                break
    if cpu_temp is None:
        for tz in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            try:
                c = int(read_file(os.path.join(tz, "temp")).strip()) / 1000.0
                if 0 < c < 150:
                    cpu_temp = c
                    break
            except ValueError:
                pass
    return temps[:24], (round(cpu_temp, 1) if cpu_temp is not None else None)


def _cpu_freq():
    vals = []
    for f in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            vals.append(int(read_file(f).strip()) / 1000.0)
        except ValueError:
            pass
    if not vals:
        for line in read_file("/proc/cpuinfo").splitlines():
            if line.startswith("cpu MHz"):
                try:
                    vals.append(float(line.split(":")[1]))
                except ValueError:
                    pass
    return round(sum(vals) / len(vals)) if vals else None


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="sampler")
        self.lock = threading.Lock()
        self.hist = collections.deque(maxlen=180)
        self.latest = {}
        self.prev = None

    def run(self):
        while True:
            try:
                self.sample()
            except Exception:
                pass
            time.sleep(1.0)

    def sample(self):
        now = time.time()
        cpu = _cpu_times()
        net = _net_bytes()
        dio = _disk_io()
        cur = {"t": now, "cpu": cpu, "net": net, "dio": dio}
        if self.prev is None:
            self.prev = cur
            return
        p = self.prev
        dt = max(now - p["t"], 1e-3)
        prev_cpu = {c[0]: c for c in p["cpu"]}
        usage = {}
        for name, tot, idle in cpu:
            if name in prev_cpu:
                dtot = tot - prev_cpu[name][1]
                didle = idle - prev_cpu[name][2]
                usage[name] = round(100.0 * (dtot - didle) / dtot, 1) if dtot > 0 else 0.0
        cores = [usage.get(f"cpu{i}", 0.0) for i in range(len(cpu) - 1)]
        mem = _meminfo()
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        swap_t = mem.get("SwapTotal", 0)
        swap_u = swap_t - mem.get("SwapFree", 0)
        try:
            sv = os.statvfs("/")
            root_total = sv.f_blocks * sv.f_frsize
            root_free = sv.f_bavail * sv.f_frsize
            root_used = root_total - sv.f_bfree * sv.f_frsize
        except OSError:
            root_total = root_free = root_used = 0
        la = read_file("/proc/loadavg").split()
        temps, cpu_temp = _temps()
        try:
            uptime = float(read_file("/proc/uptime").split()[0])
        except (ValueError, IndexError):
            uptime = 0
        snap = {
            "t": now,
            "cpu": usage.get("cpu", 0.0),
            "cores": cores,
            "freq_mhz": _cpu_freq(),
            "cpu_temp": cpu_temp,
            "temps": temps,
            "mem": {"total": total, "used": total - avail, "avail": avail,
                    "cached": mem.get("Cached", 0) + mem.get("SReclaimable", 0),
                    "buffers": mem.get("Buffers", 0),
                    "pct": round(100.0 * (total - avail) / total, 1) if total else 0},
            "swap": {"total": swap_t, "used": swap_u,
                     "pct": round(100.0 * swap_u / swap_t, 1) if swap_t else 0},
            "net": {"rx_bps": max(0, (net[0] - p["net"][0]) * 8 / dt),
                    "tx_bps": max(0, (net[1] - p["net"][1]) * 8 / dt),
                    "rx_total": net[0], "tx_total": net[1]},
            "disk_io": {"read_Bps": max(0, (dio[0] - p["dio"][0]) / dt),
                        "write_Bps": max(0, (dio[1] - p["dio"][1]) / dt)},
            "load": [float(x) for x in la[:3]] if len(la) >= 3 else [0, 0, 0],
            "procs": int(la[3].split("/")[1]) if len(la) >= 4 and "/" in la[3] else 0,
            "running": int(la[3].split("/")[0]) if len(la) >= 4 and "/" in la[3] else 0,
            "uptime": uptime,
            "root": {"total": root_total, "used": root_used, "free": root_free,
                     "pct": round(100.0 * root_used / root_total, 1) if root_total else 0},
        }
        self.prev = cur
        with self.lock:
            self.latest = snap
            self.hist.append({"t": now, "cpu": snap["cpu"], "cores": cores, "mem": snap["mem"]["pct"],
                              "swap": snap["swap"]["pct"], "rx": snap["net"]["rx_bps"],
                              "tx": snap["net"]["tx_bps"], "dr": snap["disk_io"]["read_Bps"],
                              "dw": snap["disk_io"]["write_Bps"]})


SAMPLER = Sampler()


def api_stats(q, b):
    with SAMPLER.lock:
        return dict(SAMPLER.latest)


def api_history(q, b):
    with SAMPLER.lock:
        return {"points": list(SAMPLER.hist)}


def api_sysinfo(q, b):
    osr = {}
    for line in read_file("/etc/os-release").splitlines():
        k, _, v = line.partition("=")
        osr[k] = v.strip().strip('"')
    cpu_model = ""
    for line in read_file("/proc/cpuinfo").splitlines():
        if line.startswith(("model name", "Hardware", "Model")):
            cpu_model = line.split(":", 1)[1].strip()
            break
    btime = 0
    for line in read_file("/proc/stat").splitlines():
        if line.startswith("btime"):
            btime = int(line.split()[1])
    gpu = ""
    if which("lspci"):
        rc, out, _ = run(["lspci"], timeout=5)
        for line in out.splitlines():
            if re.search(r"VGA|3D controller|Display controller", line):
                gpu = line.split(": ", 1)[-1]
                break
    mem = _meminfo()
    return {
        "hostname": socket.gethostname(),
        "os": osr.get("PRETTY_NAME", platform.system()),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_model": cpu_model,
        "cpu_count": NCPU,
        "mem_total": mem.get("MemTotal", 0),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "session": os.environ.get("XDG_SESSION_TYPE", ""),
        "user": pwd.getpwuid(os.getuid()).pw_name,
        "boot_time": btime,
        "gpu": gpu,
        "python": platform.python_version(),
    }


def _gi_available():
    try:
        import gi  # noqa
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio  # noqa
        return True
    except Exception:
        return False


def api_meta(q, b):
    tools = {t: bool(which(t)) for t in ("pkexec", "systemctl", "journalctl", "apt-get", "dpkg-query",
                                         "snap", "flatpak", "smartctl", "udisksctl", "gio", "xdg-open",
                                         "lsblk", "zstd", "sgdisk", "sfdisk", "docker", "sg", "e2fsck")}
    tools["python3-gi"] = _gi_available()
    return {"name": APP_NAME, "version": APP_VERSION, "build": APP_BUILD, "port": PORT,
            "pid": os.getpid(), "euid": os.geteuid(), "tools": tools, "data_dir": DATA_DIR,
            "python": platform.python_version()}


# --------------------------------------------------------------------------- processes

_PROC_PREV = {}
_PROC_LOCK = threading.Lock()


def api_processes(q, b):
    now = time.time()
    uid_cache = {}
    procs = []
    mem_total = _meminfo().get("MemTotal", 1)
    with _PROC_LOCK:
        new_prev = {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                raw = read_file(f"/proc/{pid}/stat")
                if not raw:
                    continue
                lpar, rpar = raw.find("("), raw.rfind(")")
                name = raw[lpar + 1:rpar]
                f = raw[rpar + 2:].split()
                state = f[0]
                utime, stime = int(f[11]), int(f[12])
                nice = int(f[16])
                threads = int(f[17])
                start = int(f[19])
                rss = int(f[21]) * PAGE_SIZE
                uid = os.stat(f"/proc/{pid}").st_uid
                if uid not in uid_cache:
                    try:
                        uid_cache[uid] = pwd.getpwuid(uid).pw_name
                    except KeyError:
                        uid_cache[uid] = str(uid)
                ticks = utime + stime
                key = (int(pid), start)
                cpu = 0.0
                if key in _PROC_PREV:
                    pt, pticks = _PROC_PREV[key]
                    dt = now - pt
                    if dt > 0:
                        cpu = 100.0 * (ticks - pticks) / CLK_TCK / dt / NCPU
                new_prev[key] = (now, ticks)
                cmd = read_file(f"/proc/{pid}/cmdline").replace("\x00", " ").strip()
                procs.append({"pid": int(pid), "name": name, "user": uid_cache[uid], "uid": uid,
                              "state": state, "cpu": round(max(cpu, 0), 1), "rss": rss,
                              "mem": round(100.0 * rss / mem_total, 1), "threads": threads,
                              "nice": nice, "cmd": cmd[:400]})
            except (OSError, IndexError, ValueError):
                continue
        _PROC_PREV.clear()
        _PROC_PREV.update(new_prev)
    return {"processes": procs, "my_uid": os.getuid(), "ncpu": NCPU}


def api_process_signal(q, b):
    pid = int(b.get("pid", 0))
    sig = str(b.get("signal", "TERM")).upper()
    if pid <= 1:
        raise ApiError("Refusing to signal PID 0/1.")
    if pid == os.getpid():
        raise ApiError("That is MySysPilot itself — use Quit in About.")
    sigs = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "STOP": signal.SIGSTOP, "CONT": signal.SIGCONT,
            "HUP": signal.SIGHUP, "INT": signal.SIGINT}
    if sig not in sigs:
        raise ApiError("Unsupported signal")
    try:
        os.kill(pid, sigs[sig])
        return {"ok": True, "elevated": False}
    except PermissionError:
        priv_ok(["kill", "-s", sig, str(pid)], timeout=120)
        return {"ok": True, "elevated": True}
    except ProcessLookupError:
        raise ApiError("Process no longer exists.")


def api_process_renice(q, b):
    pid = int(b.get("pid", 0))
    nice = int(b.get("nice", 0))
    if pid <= 1 or not -20 <= nice <= 19:
        raise ApiError("Invalid pid/nice")
    try:
        os.setpriority(os.PRIO_PROCESS, pid, nice)
        return {"ok": True}
    except PermissionError:
        priv_ok(["renice", "-n", str(nice), "-p", str(pid)], timeout=120)
        return {"ok": True, "elevated": True}


# --------------------------------------------------------------------------- services

SERVICE_RE = re.compile(r"^[A-Za-z0-9@_.:\-\\]+\.service$")


def api_services(q, b):
    if not which("systemctl"):
        raise ApiError("systemctl not found (systemd required).")
    units = {}
    rc, out, _ = run(["systemctl", "list-units", "--type=service", "--all", "--no-pager", "-o", "json"])
    try:
        for u in json.loads(out):
            units[u["unit"]] = {"name": u["unit"], "load": u.get("load"), "active": u.get("active"),
                                "sub": u.get("sub"), "description": u.get("description", "")}
    except Exception:
        rc, out, _ = run(["systemctl", "list-units", "--type=service", "--all", "--no-pager",
                          "--plain", "--no-legend"])
        for line in out.splitlines():
            p = line.split(None, 4)
            if len(p) >= 4:
                units[p[0]] = {"name": p[0], "load": p[1], "active": p[2], "sub": p[3],
                               "description": p[4] if len(p) > 4 else ""}
    rc, out, _ = run(["systemctl", "list-unit-files", "--type=service", "--no-pager", "-o", "json"])
    files = {}
    try:
        for f in json.loads(out):
            files[f["unit_file"]] = f.get("state")
    except Exception:
        rc, out, _ = run(["systemctl", "list-unit-files", "--type=service", "--no-pager", "--no-legend"])
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 2:
                files[p[0]] = p[1]
    for name, state in files.items():
        if name not in units:
            if "@" in name and name.endswith("@.service"):
                continue  # templates
            units[name] = {"name": name, "load": "loaded", "active": "inactive", "sub": "dead",
                           "description": ""}
    for name, u in units.items():
        u["enabled"] = files.get(name, "")
    return {"services": sorted(units.values(), key=lambda x: x["name"].lower())}


def api_service_action(q, b):
    name = str(b.get("name", ""))
    action = str(b.get("action", ""))
    if not SERVICE_RE.match(name):
        raise ApiError("Invalid unit name")
    if action not in ("start", "stop", "restart", "enable", "disable", "mask", "unmask"):
        raise ApiError("Invalid action")
    priv_ok(["systemctl", action, name], timeout=300)
    return {"ok": True}


# --------------------------------------------------------------------------- startup applications

AUTOSTART_USER = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "autostart")
AUTOSTART_SYS = ["/etc/xdg/autostart"]


def _parse_desktop(path):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        cp.read_string(read_file(path))
        return dict(cp["Desktop Entry"]) if "Desktop Entry" in cp else {}
    except Exception:
        return {}


def _set_desktop_key(text, key, value):
    lines = text.splitlines()
    out, in_sec, done = [], False, False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("["):
            if in_sec and not done:
                out.append(f"{key}={value}")
                done = True
            in_sec = s == "[Desktop Entry]"
        elif in_sec and re.match(rf"^{re.escape(key)}\s*=", s):
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        if not any(l.strip() == "[Desktop Entry]" for l in lines):
            out.insert(0, "[Desktop Entry]")
        out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def api_startup(q, b):
    entries = {}
    for d in AUTOSTART_SYS:
        for p in sorted(glob.glob(os.path.join(d, "*.desktop"))):
            entries[os.path.basename(p)] = {"file": os.path.basename(p), "system_path": p, "user_path": None}
    for p in sorted(glob.glob(os.path.join(AUTOSTART_USER, "*.desktop"))):
        e = entries.setdefault(os.path.basename(p), {"file": os.path.basename(p), "system_path": None})
        e["user_path"] = p
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")
    result = []
    for e in entries.values():
        d = _parse_desktop(e["user_path"] or e["system_path"])
        if not d:
            continue
        only = [x for x in d.get("OnlyShowIn", "").split(";") if x]
        notin = [x for x in d.get("NotShowIn", "").split(";") if x]
        applies = (not only or any(x in desktop for x in only)) and not any(x in desktop for x in notin)
        enabled = d.get("Hidden", "false").lower() != "true" and \
            d.get("X-GNOME-Autostart-enabled", "true").lower() != "false"
        result.append({"file": e["file"], "name": d.get("Name", e["file"]), "exec": d.get("Exec", ""),
                       "comment": d.get("Comment", ""), "icon": d.get("Icon", ""), "enabled": enabled,
                       "user": bool(e["user_path"]), "system": bool(e["system_path"]),
                       "applies": applies, "path": e["user_path"] or e["system_path"]})
    result.sort(key=lambda x: (not x["applies"], x["name"].lower()))
    return {"entries": result, "user_dir": AUTOSTART_USER}


def _safe_desktop_name(name):
    if not re.match(r"^[A-Za-z0-9_.@+\-]+\.desktop$", name or ""):
        raise ApiError("Invalid .desktop file name")
    return name


def api_startup_toggle(q, b):
    fname = _safe_desktop_name(b.get("file"))
    enabled = bool(b.get("enabled"))
    os.makedirs(AUTOSTART_USER, exist_ok=True)
    user_path = os.path.join(AUTOSTART_USER, fname)
    if os.path.exists(user_path):
        text = read_file(user_path)
    else:
        src = next((os.path.join(d, fname) for d in AUTOSTART_SYS if os.path.exists(os.path.join(d, fname))), None)
        if not src:
            raise ApiError("Entry not found")
        text = read_file(src)
    text = _set_desktop_key(text, "X-GNOME-Autostart-enabled", "true" if enabled else "false")
    text = _set_desktop_key(text, "Hidden", "false" if enabled else "true")
    with open(user_path, "w") as f:
        f.write(text)
    return {"ok": True}


def api_startup_add(q, b):
    name = str(b.get("name", "")).strip()
    cmd = str(b.get("exec", "")).strip()
    comment = str(b.get("comment", "")).strip()
    if not name or not cmd or "\n" in name + cmd + comment:
        raise ApiError("Name and command are required (single line).")
    slug = re.sub(r"[^A-Za-z0-9_\-]+", "-", name).strip("-").lower() or "startup-entry"
    os.makedirs(AUTOSTART_USER, exist_ok=True)
    path = os.path.join(AUTOSTART_USER, f"{slug}.desktop")
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(AUTOSTART_USER, f"{slug}-{n}.desktop")
    with open(path, "w") as f:
        f.write("[Desktop Entry]\nType=Application\n"
                f"Name={name}\nExec={cmd}\nComment={comment}\n"
                "X-GNOME-Autostart-enabled=true\nHidden=false\nNoDisplay=false\n")
    return {"ok": True, "file": os.path.basename(path)}


def api_startup_delete(q, b):
    fname = _safe_desktop_name(b.get("file"))
    p = os.path.join(AUTOSTART_USER, fname)
    if not os.path.exists(p):
        raise ApiError("Only entries in your user autostart folder can be removed.")
    os.remove(p)
    return {"ok": True}


# --------------------------------------------------------------------------- system cleaner

CLEAN_LAST = {}


def _journal_usage():
    if not which("journalctl"):
        return 0
    rc, out, err = run(["journalctl", "--disk-usage"], timeout=20)
    m = re.search(r"take up ([\d.]+)\s*([KMGT]?)i?B?", out + err)
    if not m:
        return 0
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2)]
    return int(float(m.group(1)) * mult)


def api_cleaner_scan(q, b):
    cats = {"apt": [], "crash": [], "logs": [], "journal": [], "cache": [], "thumbs": [], "trash": [], "snap": []}

    def add(cat, path, size, label=None, root=False):
        cats[cat].append({"id": secrets.token_hex(5), "path": path, "label": label or path,
                          "size": size, "root": root})

    for p in glob.glob("/var/cache/apt/archives/*.deb") + glob.glob("/var/cache/apt/archives/partial/*"):
        if os.path.isfile(p):
            add("apt", p, path_size(p), root=True)
    for p in glob.glob("/var/crash/*"):
        try:
            own = os.stat(p).st_uid == os.getuid()
        except OSError:
            own = False
        add("crash", p, path_size(p), root=not own)
    for root, dirs, files in os.walk("/var/log", onerror=lambda e: None):
        if root.startswith("/var/log/journal"):
            continue
        for f in files:
            if re.search(r"(\.gz|\.xz|\.bz2|\.zst|\.old|\.\d+)$", f):
                p = os.path.join(root, f)
                add("logs", p, path_size(p), root=True)
    ju = _journal_usage()
    if ju:
        add("journal", "journal://vacuum-7d", ju,
            label="systemd journal — vacuum to the last 7 days (current total shown)", root=True)
    cache_dir = os.path.join(HOME, ".cache")
    if os.path.isdir(cache_dir):
        for e in sorted(os.listdir(cache_dir)):
            p = os.path.join(cache_dir, e)
            if e == "thumbnails":
                for t in glob.glob(os.path.join(p, "*")):
                    add("thumbs", t, path_size(t))
                continue
            sz = path_size(p)
            if sz > 0:
                add("cache", p, sz)
    trash = os.path.join(HOME, ".local", "share", "Trash")
    for p in glob.glob(os.path.join(trash, "files", "*")) + glob.glob(os.path.join(trash, "files", ".*")):
        add("trash", p, path_size(p), label=os.path.basename(p))
    if which("snap"):
        rc, out, _ = run(["snap", "list", "--all"], timeout=30)
        for line in out.splitlines()[1:]:
            f = line.split()
            if len(f) >= 6 and "disabled" in f[-1]:
                name, rev = f[0], f[2]
                sz = path_size(f"/var/lib/snapd/snaps/{name}_{rev}.snap")
                add("snap", f"snap://{name}/{rev}", sz, label=f"{name} (old revision {rev})", root=True)
    CLEAN_LAST.clear()
    for c, items in cats.items():
        for it in items:
            CLEAN_LAST[it["id"]] = dict(it, cat=c)
    summary = {c: {"count": len(v), "size": sum(i["size"] for i in v)} for c, v in cats.items()}
    for c in cats:
        cats[c].sort(key=lambda x: -x["size"])
    return {"categories": cats, "summary": summary}


def _clean_job(job, ids):
    items = [CLEAN_LAST[i] for i in ids if i in CLEAN_LAST]
    if not items:
        raise ApiError("Nothing selected (rescan first).")
    freed, failed = 0, []
    root_paths, snaps, journal = [], [], False
    trash_info = os.path.join(HOME, ".local", "share", "Trash", "info")
    total = len(items)
    for n, it in enumerate(items):
        if job["cancel"]:
            break
        job["progress"] = n / max(total, 1) * 0.6
        p = it["path"]
        if p.startswith("journal://"):
            journal = True
            continue
        if p.startswith("snap://"):
            name, rev = p[7:].split("/", 1)
            if re.match(r"^[a-z0-9][a-z0-9\-]*$", name) and rev.isdigit():
                snaps.append((name, rev, it["size"]))
            continue
        if it["root"]:
            root_paths.append(it)
            continue
        job["message"] = f"Removing {os.path.basename(p)}"
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            elif os.path.lexists(p):
                os.remove(p)
            if it["cat"] == "trash":
                info = os.path.join(trash_info, os.path.basename(p) + ".trashinfo")
                if os.path.exists(info):
                    os.remove(info)
            freed += it["size"]
        except PermissionError:
            root_paths.append(it)
        except Exception as e:
            failed.append(f"{p}: {e}")
    if (root_paths or snaps or journal) and not job["cancel"]:
        job["message"] = "Waiting for authorization (privileged items)…"
        job["progress"] = 0.65
        script = 'set -u; if [ "$#" -gt 0 ]; then rm -rf -- "$@"; fi'
        if journal:
            script += "; journalctl --vacuum-time=7d >/dev/null 2>&1 || true"
        for name, rev, _ in snaps:
            script += f"; snap remove {name} --revision={rev} >/dev/null 2>&1 || true"
        rc, out, err = priv(["sh", "-c", script, "mysyspilot"] + [i["path"] for i in root_paths])
        if rc == 0:
            freed += sum(i["size"] for i in root_paths) + sum(s[2] for s in snaps)
            if journal:
                after = _journal_usage()
                jit = next((i for i in items if i["path"].startswith("journal://")), None)
                if jit:
                    freed += max(0, jit["size"] - after)
        else:
            failed.append(f"Privileged clean failed: {(err or out).strip()[:400]}")
    job["message"] = "Done"
    return {"freed": freed, "failed": failed}


def api_cleaner_clean(q, b):
    ids = [str(x) for x in b.get("ids", [])]
    return {"job": job_start("clean", _clean_job, ids)}


# --------------------------------------------------------------------------- packages (uninstaller)

PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+.\-:]*$")


def api_packages(q, b):
    kind = q.get("kind", "apt")
    pkgs = []
    if kind == "apt":
        if not which("dpkg-query"):
            raise ApiError("dpkg-query not found (Debian/Ubuntu only).")
        manual = set()
        if which("apt-mark"):
            rc, out, _ = run(["apt-mark", "showmanual"], timeout=60)
            manual = set(out.split())
        rc, out, _ = run(["dpkg-query", "-W", "-f",
                          "${db:Status-Abbrev}\t${Package}\t${Version}\t${Installed-Size}\t"
                          "${Architecture}\t${Section}\t${binary:Summary}\n"], timeout=60)
        for line in out.splitlines():
            f = line.split("\t")
            if len(f) < 7 or not f[0].startswith("ii"):
                continue
            pkgs.append({"name": f[1], "version": f[2], "size": int(f[3] or 0) * 1024, "arch": f[4],
                         "section": f[5], "summary": f[6], "manual": f[1] in manual})
    elif kind == "snap":
        if not which("snap"):
            raise ApiError("snapd is not installed.")
        rc, out, _ = run(["snap", "list"], timeout=60)
        for line in out.splitlines()[1:]:
            f = line.split()
            if len(f) >= 4:
                sz = path_size(f"/var/lib/snapd/snaps/{f[0]}_{f[2]}.snap")
                pkgs.append({"name": f[0], "version": f[1], "rev": f[2], "size": sz,
                             "summary": " ".join(f[4:5]) if len(f) > 4 else "",
                             "notes": f[-1] if len(f) >= 6 else "", "manual": True})
    elif kind == "flatpak":
        if not which("flatpak"):
            raise ApiError("Flatpak is not installed.")
        rc, out, _ = run(["flatpak", "list", "--app", "--columns=application,name,version,size,installation"],
                         timeout=60)
        for line in out.splitlines():
            f = line.split("\t")
            if len(f) >= 5:
                pkgs.append({"name": f[0], "summary": f[1], "version": f[2], "size_text": f[3],
                             "installation": f[4], "size": 0, "manual": True})
    else:
        raise ApiError("Unknown package kind")
    return {"packages": pkgs}


def api_package_remove(q, b):
    kind = b.get("kind")
    names = [str(n) for n in b.get("names", [])]
    purge = bool(b.get("purge"))
    if not names or not all(PKG_RE.match(n) for n in names):
        raise ApiError("Invalid package name(s)")
    if kind == "apt":
        cmd = ["env", "DEBIAN_FRONTEND=noninteractive", which("apt-get") or "apt-get",
               "purge" if purge else "remove", "-y", "--"] + names
        return {"job": job_start("pkg-remove", stream_job, cmd)}
    if kind == "snap":
        cmd = ["snap", "remove"] + names + (["--purge"] if purge else [])
        return {"job": job_start("pkg-remove", stream_job, cmd)}
    if kind == "flatpak":
        inst = b.get("installation", "system")
        cmd = ["flatpak", "uninstall", "-y", "--noninteractive"] + \
              (["--user"] if inst == "user" else ["--system"]) + names
        return {"job": job_start("pkg-remove", stream_job, cmd, False)}
    raise ApiError("Unknown package kind")


def api_package_autoremove(q, b):
    cmd = ["env", "DEBIAN_FRONTEND=noninteractive", which("apt-get") or "apt-get", "autoremove", "-y"]
    return {"job": job_start("autoremove", stream_job, cmd)}


# --------------------------------------------------------------------------- APT repositories


def _apt_source_files():
    files = []
    if os.path.exists("/etc/apt/sources.list"):
        files.append("/etc/apt/sources.list")
    files += sorted(glob.glob("/etc/apt/sources.list.d/*.list"))
    files += sorted(glob.glob("/etc/apt/sources.list.d/*.sources"))
    return files


def api_repos(q, b):
    repos = []
    for path in _apt_source_files():
        text = read_file(path)
        if path.endswith(".sources"):
            for idx, stanza in enumerate(re.split(r"\n\s*\n", text)):
                fields = {}
                for line in stanza.splitlines():
                    if line.startswith("#") or ":" not in line or line.startswith((" ", "\t")):
                        continue
                    k, _, v = line.partition(":")
                    fields[k.strip().lower()] = v.strip()
                if "uris" not in fields:
                    continue
                repos.append({"file": path, "format": "deb822", "index": idx,
                              "enabled": fields.get("enabled", "yes").lower() != "no",
                              "types": fields.get("types", ""), "uri": fields.get("uris", ""),
                              "suite": fields.get("suites", ""), "components": fields.get("components", "")})
        else:
            for idx, line in enumerate(text.splitlines()):
                m = re.match(r"^\s*(#\s*)?(deb|deb-src)\s+(\[[^\]]*\]\s+)?(\S+)\s+(\S+)\s*(.*)$", line)
                if not m:
                    continue
                repos.append({"file": path, "format": "list", "index": idx, "enabled": not m.group(1),
                              "types": m.group(2), "uri": m.group(4), "suite": m.group(5),
                              "components": m.group(6).split("#")[0].strip()})
    return {"repos": repos}


def api_repo_toggle(q, b):
    path = str(b.get("file", ""))
    idx = int(b.get("index", -1))
    enable = bool(b.get("enabled"))
    if path not in _apt_source_files():
        raise ApiError("Unknown sources file")
    text = read_file(path)
    if path.endswith(".sources"):
        stanzas = re.split(r"(\n\s*\n)", text)
        real = [i for i in range(0, len(stanzas), 2)]
        if idx < 0 or idx >= len(real):
            raise ApiError("Stanza not found")
        st = stanzas[real[idx]]
        lines = [l for l in st.splitlines() if not re.match(r"^Enabled\s*:", l, re.I)]
        if not enable:
            lines.append("Enabled: no")
        stanzas[real[idx]] = "\n".join(lines)
        new = "".join(stanzas)
        if not new.endswith("\n"):
            new += "\n"
    else:
        lines = text.splitlines()
        if idx < 0 or idx >= len(lines):
            raise ApiError("Line not found")
        l = lines[idx]
        lines[idx] = re.sub(r"^\s*#\s*", "", l) if enable else ("# " + l if not l.lstrip().startswith("#") else l)
        new = "\n".join(lines) + "\n"
    ensure_data_dir()
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix="apt-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(new)
    os.chmod(tmp, 0o644)
    try:
        script = ('mkdir -p /var/backups/mysyspilot && cp -- "$1" "/var/backups/mysyspilot/$(basename "$1").'
                  '$(date +%Y%m%d%H%M%S)" && cp -- "$2" "$1" && chmod 0644 "$1"')
        priv_ok(["sh", "-c", script, "mysyspilot", path, tmp], timeout=300)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return {"ok": True}


# --------------------------------------------------------------------------- disk utility (lsblk + UDisks2)

UD = "org.freedesktop.UDisks2"
UD_BLOCK = "/org/freedesktop/UDisks2/block_devices/"
CRITICAL_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "/opt", "/srv", "[SWAP]"}


def _gio():
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        return Gio, GLib
    except Exception:
        raise ApiError("This action needs PyGObject. Install it with: sudo apt install python3-gi")


def _ud_escape(name):
    return "".join(c if c.isalnum() else "_%02x" % ord(c) for c in name)


def _vopts(d):
    _, GLib = _gio()
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, bool):
            out[k] = GLib.Variant("b", v)
        elif isinstance(v, int):
            out[k] = GLib.Variant("t", v)
        elif isinstance(v, list):
            out[k] = GLib.Variant("as", [str(x) for x in v])
        else:
            out[k] = GLib.Variant("s", str(v))
    return out


def ud_call(path, iface, method, sig=None, args=None, timeout_ms=6 * 3600 * 1000, want_fd=False):
    Gio, GLib = _gio()
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    params = GLib.Variant(sig, tuple(args)) if sig else None
    flags = Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION
    try:
        if want_fd:
            res, fds = bus.call_with_unix_fd_list_sync(UD, path, iface, method, params, None, flags,
                                                       timeout_ms, None, None)
            idx = res.unpack()[0]
            return fds.get(idx)
        res = bus.call_sync(UD, path, iface, method, params, None, flags, timeout_ms, None)
        return res.unpack() if res is not None else None
    except GLib.Error as e:
        msg = e.message or str(e)
        msg = re.sub(r"^GDBus\.Error:[\w.]+:\s*", "", msg)
        raise ApiError(msg)


def ud_props(path, iface):
    try:
        r = ud_call(path, "org.freedesktop.DBus.Properties", "GetAll", "(s)", [iface], timeout_ms=10000)
        return r[0] if r else {}
    except ApiError:
        return {}


def _bytes_str(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).rstrip(b"\x00").decode(errors="replace")
    if isinstance(v, list):
        return bytes(v).rstrip(b"\x00").decode(errors="replace")
    return v


def _lsblk():
    if not which("lsblk"):
        raise ApiError("lsblk not found (util-linux).")
    full = ("NAME,KNAME,MAJ:MIN,PATH,PKNAME,SIZE,TYPE,FSTYPE,FSVER,LABEL,UUID,PARTLABEL,PARTUUID,PARTTYPENAME,MOUNTPOINTS,"
            "FSUSED,FSAVAIL,FSUSE%,MODEL,SERIAL,VENDOR,TRAN,ROTA,RM,RO,HOTPLUG,PTTYPE,STATE,LOG-SEC,PHY-SEC")
    basic = ("NAME,KNAME,MAJ:MIN,PATH,PKNAME,SIZE,TYPE,FSTYPE,LABEL,UUID,PARTLABEL,PARTUUID,MOUNTPOINT,FSUSED,FSAVAIL,"
             "MODEL,SERIAL,VENDOR,TRAN,ROTA,RM,RO,HOTPLUG,PTTYPE,STATE,LOG-SEC,PHY-SEC")
    for cols in (full, basic):
        rc, out, err = run(["lsblk", "-J", "-b", "-o", cols], timeout=20)
        if rc == 0:
            return json.loads(out).get("blockdevices", [])
    raise ApiError("lsblk failed: " + err.strip())


def _mountinfo():
    """{ "maj:min": [mountpoints] } straight from the kernel (always current)."""
    m = {}
    for line in read_file("/proc/self/mountinfo").splitlines():
        f = line.split()
        if len(f) < 5:
            continue
        mp = re.sub(r"\\([0-7]{3})", lambda x: chr(int(x.group(1), 8)), f[4])
        m.setdefault(f[2], []).append(mp)
    for line in read_file("/proc/swaps").splitlines()[1:]:
        f = line.split()
        if f and f[0].startswith("/dev/"):
            try:
                st = os.stat(os.path.realpath(f[0]))
                m.setdefault(f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}", []).append("[SWAP]")
            except OSError:
                pass
    return m


def _devnum(path):
    st = os.stat(os.path.realpath(path))
    return f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"


def _settle(timeout=4):
    """Wait for udev to finish processing pending events (label/fstype/uuid changes)."""
    if which("udevadm"):
        run(["udevadm", "settle", "-t", str(timeout)], timeout=timeout + 2)


def _disk_stamp():
    h = hashlib.sha1()
    for p in ("/proc/self/mountinfo", "/proc/partitions", "/proc/swaps"):
        h.update(read_file(p).encode(errors="replace"))
    for p in sorted(glob.glob("/run/udev/data/b*")):
        try:
            h.update(f"{p}:{os.stat(p).st_mtime_ns}".encode())
        except OSError:
            pass
    for p in sorted(glob.glob("/sys/class/block/*/size")):
        h.update(f"{p}:{read_file(p).strip()}".encode())
    return h.hexdigest()


def api_disks_stamp(q, b):
    return {"stamp": _disk_stamp()}


def _toint(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _norm(dev, mi=None):
    mps = dev.get("mountpoints")
    if mps is None:
        mps = [dev.get("mountpoint")] if dev.get("mountpoint") else []
    mps = [m for m in mps if m]
    if mi is not None and dev.get("maj:min") in mi:
        for m in mi[dev["maj:min"]]:
            if m not in mps:
                mps.append(m)
    kname = dev.get("kname") or dev.get("name")
    start = _toint(read_file(f"/sys/class/block/{kname}/start").strip() or None)
    n = {
        "name": dev.get("name"), "kname": kname, "path": dev.get("path") or f"/dev/{kname}",
        "pkname": dev.get("pkname"), "size": _toint(dev.get("size")) or 0, "type": dev.get("type"),
        "fstype": dev.get("fstype"), "fsver": dev.get("fsver"), "label": dev.get("label"),
        "uuid": dev.get("uuid"), "partuuid": dev.get("partuuid"), "partlabel": dev.get("partlabel"), "parttypename": dev.get("parttypename"),
        "mountpoints": mps, "fsused": _toint(dev.get("fsused")), "fsavail": _toint(dev.get("fsavail")),
        "model": (dev.get("model") or "").strip(), "serial": dev.get("serial"),
        "vendor": (dev.get("vendor") or "").strip(), "tran": dev.get("tran"), "rota": dev.get("rota"),
        "rm": dev.get("rm"), "ro": dev.get("ro"), "hotplug": dev.get("hotplug"), "pttype": dev.get("pttype"),
        "state": dev.get("state"), "logsec": _toint(dev.get("log-sec")), "physec": _toint(dev.get("phy-sec")),
        "start": start * 512 if start is not None else None,
        "majmin": dev.get("maj:min"),
        "children": [_norm(c, mi) for c in dev.get("children", []) or []],
    }
    return n


def _all_mounts(node):
    m = list(node.get("mountpoints") or [])
    for c in node.get("children", []):
        m += _all_mounts(c)
    return m


def _free_regions(disk):
    size = disk["size"]
    if not disk.get("pttype"):
        return []
    MiB = 1024 * 1024
    first = MiB
    last = size - (33 * 512 if disk.get("pttype") == "gpt" else 0)
    last = (last // MiB) * MiB
    parts = sorted([c for c in disk["children"] if c.get("type") == "part" and c.get("start") is not None],
                   key=lambda c: c["start"])
    regions, cur = [], first
    for p in parts:
        if p["start"] - cur >= 2 * MiB:
            regions.append({"offset": cur, "size": p["start"] - cur})
        cur = max(cur, ((p["start"] + p["size"] + MiB - 1) // MiB) * MiB)
    if last - cur >= 2 * MiB:
        regions.append({"offset": cur, "size": last - cur})
    return regions


def _drive_health(disk_kname):
    """Lightweight SMART summary from UDisks2 properties (no root needed)."""
    try:
        Gio, GLib = _gio()
    except ApiError:
        return None
    bp = UD_BLOCK + _ud_escape(disk_kname)
    blk = ud_props(bp, "org.freedesktop.UDisks2.Block")
    drive = blk.get("Drive")
    if not drive or drive == "/":
        return None
    d = ud_props(drive, "org.freedesktop.UDisks2.Drive")
    info = {"drive": drive, "model": d.get("Model"), "vendor": d.get("Vendor"), "serial": d.get("Serial"),
            "revision": d.get("Revision"), "wwn": d.get("WWN"), "bus": d.get("ConnectionBus"),
            "rotation": d.get("RotationRate"), "removable": d.get("Removable"),
            "ejectable": d.get("Ejectable"), "can_poweroff": d.get("CanPowerOff"), "smart": None}
    ata = ud_props(drive, "org.freedesktop.UDisks2.Drive.Ata")
    if ata:
        t = ata.get("SmartTemperature") or 0
        info["smart"] = {"type": "ata", "supported": ata.get("SmartSupported"), "enabled": ata.get("SmartEnabled"),
                         "failing": ata.get("SmartFailing"),
                         "temp_c": round(t - 273.15, 1) if t else None,
                         "power_on_hours": round((ata.get("SmartPowerOnSeconds") or 0) / 3600),
                         "attrs_failing": ata.get("SmartNumAttributesFailing"),
                         "attrs_failed_past": ata.get("SmartNumAttributesFailedInThePast"),
                         "bad_sectors": ata.get("SmartNumBadSectors"),
                         "selftest_status": ata.get("SmartSelftestStatus"),
                         "selftest_remaining": ata.get("SmartSelftestPercentRemaining"),
                         "updated": ata.get("SmartUpdated"),
                         "pm_supported": ata.get("PmSupported")}
    nvme = ud_props(drive, "org.freedesktop.UDisks2.NVMe.Controller")
    if nvme:
        t = nvme.get("SmartTemperature") or 0
        warn = nvme.get("SmartCriticalWarning") or []
        info["smart"] = {"type": "nvme", "supported": True, "enabled": True, "failing": bool(warn),
                         "critical_warning": warn, "temp_c": round(t - 273.15, 1) if t else None,
                         "power_on_hours": nvme.get("SmartPowerOnHours"),
                         "selftest_status": nvme.get("SmartSelftestStatus"),
                         "selftest_remaining": nvme.get("SmartSelftestPercentRemaining"),
                         "updated": nvme.get("SmartUpdated")}
    return info


_HEALTH_CACHE = {}


def _drive_health_cached(kname, fresh=False):
    now = time.time()
    c = _HEALTH_CACHE.get(kname)
    if c and not fresh and now - c[0] < 30:
        return c[1]
    v = _drive_health(kname)
    _HEALTH_CACHE[kname] = (now, v)
    return v


def api_disks(q, b):
    if q.get("settle") == "1":
        _settle()
    stamp = _disk_stamp()
    mi = _mountinfo()
    devs = [_norm(d, mi) for d in _lsblk()]
    show_snap = q.get("snap") == "1"
    disks = []
    for d in devs:
        if d["type"] == "loop":
            if d["size"] == 0:
                continue
            mps = _all_mounts(d)
            if not show_snap and any(m.startswith("/snap/") for m in mps):
                continue
        if d["size"] == 0:
            continue
        d["free"] = _free_regions(d) if d["type"] == "disk" else []
        d["critical"] = bool(set(_all_mounts(d)) & CRITICAL_MOUNTS)
        try:
            d["health"] = _drive_health_cached(d["kname"], q.get("settle") == "1") if d["type"] == "disk" else None
        except Exception:
            d["health"] = None
        try:
            _annotate_health(d)
        except Exception:
            pass
        disks.append(d)
    return {"disks": disks, "gi": _gi_available(), "smartctl": bool(which("smartctl")), "stamp": stamp,
            "ts": time.time()}


def _validate_dev(dev):
    if not isinstance(dev, str) or not re.match(r"^/dev/[A-Za-z0-9/_.\-]+$", dev):
        raise ApiError("Invalid device path")
    real = os.path.realpath(dev)
    try:
        if not statmod.S_ISBLK(os.stat(real).st_mode):
            raise ApiError("Not a block device")
    except FileNotFoundError:
        raise ApiError("Device not found")
    return real, os.path.basename(real)


def _find_node(kname):
    mi = _mountinfo()
    for d in [_norm(x, mi) for x in _lsblk()]:
        stack = [d]
        while stack:
            n = stack.pop()
            if n["kname"] == kname:
                return n, d
            stack += n.get("children", [])
    raise ApiError("Device not found in lsblk")


def _guard_destructive(dev, kname, confirm):
    node, top = _find_node(kname)
    mounts = set(_all_mounts(node))
    if mounts & CRITICAL_MOUNTS:
        raise ApiError(f"Refused: {dev} holds a system mount ({', '.join(sorted(mounts & CRITICAL_MOUNTS))}).")
    if confirm != dev:
        raise ApiError(f"Confirmation text must exactly match {dev}.")
    return node


def api_disk_smart(q, b):
    dev, kname = _validate_dev(q.get("dev", ""))
    out = {"dev": dev, "summary": _drive_health(kname), "attributes": [], "source": None, "raw": None}
    if q.get("detail") != "1":
        return out
    if which("smartctl"):
        rc, so, se = priv(["smartctl", "-j", "-a", dev], timeout=120)
        try:
            j = json.loads(so)
        except Exception:
            raise ApiError((se or so or "smartctl failed").strip()[:600])
        out["source"] = "smartctl"
        out["passed"] = (j.get("smart_status") or {}).get("passed")
        for a in (j.get("ata_smart_attributes") or {}).get("table", []):
            out["attributes"].append({"id": a.get("id"), "name": a.get("name"), "value": a.get("value"),
                                      "worst": a.get("worst"), "thresh": a.get("thresh"),
                                      "raw": (a.get("raw") or {}).get("string"),
                                      "when_failed": a.get("when_failed") or "",
                                      "prefail": (a.get("flags") or {}).get("prefailure")})
        nv = j.get("nvme_smart_health_information_log")
        if nv:
            for k, v in nv.items():
                out["attributes"].append({"id": "", "name": k.replace("_", " "), "value": v if not isinstance(v, list) else ", ".join(map(str, v)),
                                          "worst": "", "thresh": "", "raw": "", "when_failed": ""})
        out["selftests"] = ((j.get("ata_smart_self_test_log") or {}).get("standard") or {}).get("table", [])[:10]
        out["model"] = j.get("model_name")
        out["firmware"] = j.get("firmware_version")
        out["capacity"] = (j.get("user_capacity") or {}).get("bytes")
        return out
    s = out["summary"] or {}
    if s.get("smart", {}) and s["smart"].get("type") == "ata":
        r = ud_call(s["drive"], "org.freedesktop.UDisks2.Drive.Ata", "SmartGetAttributes", "(a{sv})", [_vopts({})],
                    timeout_ms=60000)
        out["source"] = "udisks2"
        for a in r[0]:
            aid, name, flags, value, worst, thresh, pretty, unit, exp = a
            out["attributes"].append({"id": aid, "name": name, "value": value, "worst": worst, "thresh": thresh,
                                      "raw": pretty, "when_failed": "", "prefail": bool(flags & 1)})
        return out
    raise ApiError("Detailed SMART needs smartmontools: sudo apt install smartmontools")


def _benchmark_job(job, dev, kname, samples, sample_mib, access_samples):
    bp = UD_BLOCK + _ud_escape(kname)
    blk = ud_props(bp, "org.freedesktop.UDisks2.Block")
    size = int(blk.get("Size") or 0)
    if size <= 0:
        raise ApiError("Unknown device size")
    job["message"] = "Waiting for authorization…"
    _, GLib = _gio()
    fd = ud_call(bp, "org.freedesktop.UDisks2.Block", "OpenForBenchmark", "(a{sv})",
                 [{"writable": GLib.Variant("b", False)}], want_fd=True)
    chunk = sample_mib * 1024 * 1024
    buf = mmap.mmap(-1, chunk)
    small = mmap.mmap(-1, 4096)
    rates, access = [], []
    try:
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except Exception:
            pass
        span = max(size - chunk, 0)
        for i in range(samples):
            if job["cancel"]:
                break
            off = (span * i // max(samples - 1, 1)) // 4096 * 4096
            t0 = time.perf_counter()
            n = os.preadv(fd, [buf], off)
            dt = time.perf_counter() - t0
            rates.append({"pos": off / size * 100, "Bps": n / dt if dt > 0 else 0})
            job["progress"] = 0.8 * (i + 1) / samples
            job["message"] = f"Read sample {i + 1}/{samples}"
            job["live"] = {"rates": rates[-samples:]}
        rnd = secrets.SystemRandom()
        for i in range(access_samples):
            if job["cancel"]:
                break
            off = rnd.randrange(0, max(size - 4096, 1)) // 4096 * 4096
            t0 = time.perf_counter()
            os.preadv(fd, [small], off)
            access.append({"pos": off / size * 100, "ms": (time.perf_counter() - t0) * 1000})
            job["progress"] = 0.8 + 0.2 * (i + 1) / access_samples
            job["message"] = f"Access-time sample {i + 1}/{access_samples}"
    finally:
        os.close(fd)
        buf.close()
        small.close()
    r = [x["Bps"] for x in rates] or [0]
    a = [x["ms"] for x in access] or [0]
    return {"dev": dev, "size": size, "rates": rates, "access": access,
            "avg_Bps": sum(r) / len(r), "min_Bps": min(r), "max_Bps": max(r), "avg_access_ms": sum(a) / len(a),
            "sample_mib": sample_mib}


def _image_create_job(job, dev, kname, dest):
    bp = UD_BLOCK + _ud_escape(kname)
    size = int(ud_props(bp, "org.freedesktop.UDisks2.Block").get("Size") or 0)
    job["message"] = "Waiting for authorization…"
    fd = ud_call(bp, "org.freedesktop.UDisks2.Block", "OpenForBackup", "(a{sv})", [_vopts({})], want_fd=True)
    done = 0
    t0 = time.time()
    try:
        with open(dest, "xb") as out:
            while True:
                if job["cancel"]:
                    break
                data = os.read(fd, 4 * 1024 * 1024)
                if not data:
                    break
                out.write(data)
                done += len(data)
                el = time.time() - t0
                job["progress"] = done / size if size else 0
                job["message"] = f"{done / 1e9:.2f} / {size / 1e9:.2f} GB  ·  {done / el / 1e6 if el else 0:.0f} MB/s"
    finally:
        os.close(fd)
    if job["cancel"]:
        try:
            os.remove(dest)
        except OSError:
            pass
    return {"dest": dest, "bytes": done}


def _image_restore_job(job, dev, kname, src):
    bp = UD_BLOCK + _ud_escape(kname)
    size = int(ud_props(bp, "org.freedesktop.UDisks2.Block").get("Size") or 0)
    isize = os.path.getsize(src)
    if isize > size:
        raise ApiError(f"Image ({isize} B) is larger than the device ({size} B).")
    job["message"] = "Waiting for authorization…"
    fd = ud_call(bp, "org.freedesktop.UDisks2.Block", "OpenForRestore", "(a{sv})", [_vopts({})], want_fd=True)
    done = 0
    t0 = time.time()
    try:
        with open(src, "rb") as f:
            while not job["cancel"]:
                data = f.read(4 * 1024 * 1024)
                if not data:
                    break
                mv = memoryview(data)
                while mv:
                    n = os.write(fd, mv)
                    mv = mv[n:]
                done += len(data)
                el = time.time() - t0
                job["progress"] = done / isize if isize else 1
                job["message"] = f"{done / 1e9:.2f} / {isize / 1e9:.2f} GB  ·  {done / el / 1e6 if el else 0:.0f} MB/s"
        job["message"] = "Flushing to device…"
        os.fsync(fd)
    finally:
        os.close(fd)
    return {"bytes": done}


def _udisks_job(job, label, steps, names=None):
    names = names or ([label] if len(steps) == 1 else [f"Step {i + 1}" for i in range(len(steps))])
    stg = _Stages(job, [(n, 10) for n in names] + [("Refresh", 1)])
    out = None
    for i, (path, iface, method, sig, args) in enumerate(steps):
        stg.go(names[i], None, f"{names[i]} (authorize if prompted)…" if i == 0 else f"{names[i]}…", indeterminate=True)
        w = None
        if _gi_available():
            w = _UdWatch(job, [path, _drive_obj(path)], stg, names[i])
            w.start()
        try:
            out = ud_call(path, iface, method, sig, args)
        finally:
            if w:
                w.stop()
        stg.go(names[i], 1.0)
    stg.go("Refresh", None, "Waiting for the system to register the change…")
    _settle(5)
    _HEALTH_CACHE.clear()
    return {"result": str(out) if out is not None else None}


def _mount_verified(action, dev, bp, FS):
    """Mount/unmount through UDisks2 (python3-gi, or udisksctl as fallback), then confirm the change
    in the kernel's mount table before answering, so the UI never shows a stale state."""
    if _gi_available():
        if action == "mount":
            ud_call(bp, FS, "Mount", "(a{sv})", [_vopts({})])
        else:
            ud_call(bp, FS, "Unmount", "(a{sv})", [_vopts({})])
    elif which("udisksctl"):
        rc, out, err = run(["udisksctl", action, "-b", dev], timeout=300)
        if rc != 0:
            raise ApiError((err or out).strip() or f"udisksctl {action} failed")
    else:
        raise ApiError("Mounting needs UDisks2: sudo apt install udisks2 python3-gi")
    num = _devnum(dev)
    want = action == "mount"
    mps = []
    for _ in range(50):
        mps = [m for m in _mountinfo().get(num, []) if m != "[SWAP]"]
        if bool(mps) == want:
            break
        time.sleep(0.1)
    _settle(2)
    _HEALTH_CACHE.clear()
    ok = bool(mps) == want
    if want:
        msg = f"Mounted at {', '.join(mps)}" if ok else "Mount reported success but the kernel does not list it yet"
    else:
        msg = f"Unmounted {dev}" if ok else f"Unmount reported success but {dev} is still mounted at {', '.join(mps)}"
    return {"ok": True, "verified": ok, "mountpoints": mps, "message": msg}


def api_disk_action(q, b):
    action = str(b.get("action", ""))
    if action == "attach_image":
        f = os.path.abspath(os.path.expanduser(str(b.get("file", ""))))
        if not os.path.isfile(f):
            raise ApiError("Image file not found")
        ro = ["-r"] if b.get("readonly", True) else []
        if not which("udisksctl"):
            raise ApiError("udisksctl not found (sudo apt install udisks2)")
        rc, out, err = run(["udisksctl", "loop-setup", "-f", f] + ro, timeout=120)
        if rc != 0:
            raise ApiError((err or out).strip())
        return {"ok": True, "message": out.strip()}
    dev, kname = _validate_dev(b.get("dev", ""))
    bp = UD_BLOCK + _ud_escape(kname)
    B, FS, PT, PA, ENC, DRV, ATA, LOOP = ("org.freedesktop.UDisks2." + x for x in
                                          ("Block", "Filesystem", "PartitionTable", "Partition", "Encrypted",
                                           "Drive", "Drive.Ata", "Loop"))
    confirm = b.get("confirm")
    p = b.get("params") or {}

    def drive_path():
        d = ud_props(bp, B).get("Drive")
        if not d or d == "/":
            raise ApiError("No drive object for this device")
        return d

    if action in ("mount", "unmount"):
        return _mount_verified(action, dev, bp, FS)
    if action == "setlabel":
        lab = str(p.get("label", ""))[:64]
        ud_call(bp, FS, "SetLabel", "(sa{sv})", [lab, _vopts({})])
        return {"ok": True}
    if action == "partname":
        ud_call(bp, PA, "SetName", "(sa{sv})", [str(p.get("name", ""))[:36], _vopts({})])
        return {"ok": True}
    if action == "takeown":
        ud_call(bp, FS, "TakeOwnership", "(a{sv})", [_vopts({"recursive": bool(p.get("recursive"))})])
        return {"ok": True}
    if action == "check":
        def chk(job):
            job["message"] = "Checking filesystem (authorize if prompted)…"
            r = ud_call(bp, FS, "Check", "(a{sv})", [_vopts({})])
            return {"consistent": bool(r[0]) if r else None}
        return {"job": job_start("fs-check", chk)}
    if action == "repair":
        _guard_destructive(dev, kname, confirm)

        def rep(job):
            job["message"] = "Repairing filesystem (authorize if prompted)…"
            r = ud_call(bp, FS, "Repair", "(a{sv})", [_vopts({})])
            return {"repaired": bool(r[0]) if r else None}
        return {"job": job_start("fs-repair", rep)}
    if action == "unlock":
        r = ud_call(bp, ENC, "Unlock", "(sa{sv})", [str(p.get("passphrase", "")), _vopts({})])
        return {"ok": True, "message": f"Unlocked as {r[0]}"}
    if action == "lock":
        ud_call(bp, ENC, "Lock", "(a{sv})", [_vopts({})])
        return {"ok": True}
    if action == "changepass":
        ud_call(bp, ENC, "ChangePassphrase", "(ssa{sv})",
                [str(p.get("old", "")), str(p.get("new", "")), _vopts({})])
        return {"ok": True}
    if action == "format":
        _guard_destructive(dev, kname, confirm)
        fstype = str(p.get("fstype", "ext4"))
        if fstype not in ("ext4", "xfs", "btrfs", "vfat", "exfat", "ntfs", "f2fs", "swap", "empty", "gpt", "dos"):
            raise ApiError("Unsupported type")
        o = {}
        if fstype not in ("gpt", "dos", "empty"):
            o["take-ownership"] = True
            o["update-partition-type"] = True
            if p.get("label"):
                o["label"] = str(p["label"])[:32]
            if p.get("passphrase"):
                o["encrypt.passphrase"] = str(p["passphrase"])
                o["encrypt.type"] = "luks2"
        if p.get("erase") == "zero":
            o["erase"] = "zero"
        return {"job": job_start("format", _udisks_job, f"Formatting {dev} as {fstype}",
                                 [(bp, B, "Format", "(sa{sv})", [fstype, _vopts(o)])], ["Format"])}
    if action == "create_partition":
        node, top = _find_node(kname)
        if node["type"] != "disk":
            raise ApiError("Select a whole disk to create a partition.")
        offset, size = int(p.get("offset", 0)), int(p.get("size", 0))
        reg = next((r for r in _free_regions(node) if r["offset"] <= offset < r["offset"] + r["size"]), None)
        if not reg or size <= 0 or offset + size > reg["offset"] + reg["size"]:
            raise ApiError("Requested range is outside the free region.")
        fstype = str(p.get("fstype", "ext4"))
        if fstype not in ("ext4", "xfs", "btrfs", "vfat", "exfat", "ntfs", "f2fs", "swap", "empty"):
            raise ApiError("Unsupported filesystem")
        fo = {"take-ownership": True} if fstype not in ("empty", "swap") else {}
        if p.get("label"):
            fo["label"] = str(p["label"])[:32]
        if p.get("passphrase"):
            fo["encrypt.passphrase"] = str(p["passphrase"])
            fo["encrypt.type"] = "luks2"
        name = str(p.get("name", ""))[:36] if node.get("pttype") == "gpt" else ""
        return {"job": job_start("partition", _udisks_job, f"Creating partition on {dev}",
                                 [(bp, PT, "CreatePartitionAndFormat", "(ttssa{sv}sa{sv})",
                                   [offset, size, "", name, _vopts({}), fstype, _vopts(fo)])], ["Create & format"])}
    if action == "delete_partition":
        _guard_destructive(dev, kname, confirm)
        return {"job": job_start("delete", _udisks_job, f"Deleting {dev}",
                                 [(bp, PA, "Delete", "(a{sv})", [_vopts({})])], ["Delete"])}
    if action == "resize":
        node = _guard_destructive(dev, kname, confirm)
        new = int(p.get("size", 0))
        if new <= 0:
            raise ApiError("Invalid size")
        steps = []
        if new < node["size"]:
            if node.get("fstype"):
                steps.append((bp, FS, "Resize", "(ta{sv})", [new, _vopts({})]))
            steps.append((bp, PA, "Resize", "(ta{sv})", [new, _vopts({})]))
        else:
            steps.append((bp, PA, "Resize", "(ta{sv})", [new, _vopts({})]))
            if node.get("fstype"):
                steps.append((bp, FS, "Resize", "(ta{sv})", [0, _vopts({})]))
        nm = [("Shrink filesystem" if s[2] == "Resize" and s[1].endswith("Filesystem") else
               "Resize partition" if s[1].endswith("Partition") else "Grow filesystem") for s in steps]
        if len(nm) == 2 and nm[0] == "Resize partition":
            nm[1] = "Grow filesystem"
        return {"job": job_start("resize", _udisks_job, f"Resizing {dev}", steps, nm)}
    if action == "poweroff":
        dp = drive_path()
        ud_call(dp, DRV, "PowerOff", "(a{sv})", [_vopts({})])
        return {"ok": True}
    if action == "eject":
        ud_call(drive_path(), DRV, "Eject", "(a{sv})", [_vopts({})])
        return {"ok": True}
    if action == "standby":
        ud_call(drive_path(), ATA, "PmStandby", "(a{sv})", [_vopts({})])
        return {"ok": True}
    if action == "wakeup":
        ud_call(drive_path(), ATA, "PmWakeup", "(a{sv})", [_vopts({})])
        return {"ok": True}
    if action in ("smart_test", "smart_abort"):
        dp = drive_path()
        nvme = bool(ud_props(dp, "org.freedesktop.UDisks2.NVMe.Controller"))
        iface = "org.freedesktop.UDisks2.NVMe.Controller" if nvme else ATA
        if action == "smart_abort":
            ud_call(dp, iface, "SmartSelftestAbort", "(a{sv})", [_vopts({})])
        else:
            t = str(p.get("type", "short"))
            if t not in ("short", "extended", "conveyance"):
                raise ApiError("Invalid test type")
            ud_call(dp, iface, "SmartSelftestStart", "(sa{sv})", [t, _vopts({})])
        return {"ok": True}
    if action == "loop_detach":
        ud_call(bp, LOOP, "Delete", "(a{sv})", [_vopts({})])
        return {"ok": True}
    if action == "benchmark":
        samples = max(10, min(int(p.get("samples", 50)), 500))
        smib = max(1, min(int(p.get("sample_mib", 10)), 100))
        acc = max(0, min(int(p.get("access", 200)), 2000))
        return {"job": job_start("benchmark", _benchmark_job, dev, kname, samples, smib, acc)}
    if action == "image_create":
        dest = os.path.abspath(os.path.expanduser(str(p.get("dest", ""))))
        if not dest or os.path.exists(dest):
            raise ApiError("Destination must be a new file path (it must not already exist).")
        if not os.path.isdir(os.path.dirname(dest)):
            raise ApiError("Destination folder does not exist.")
        return {"job": job_start("image-create", _image_create_job, dev, kname, dest)}
    if action == "image_restore":
        _guard_destructive(dev, kname, confirm)
        src = os.path.abspath(os.path.expanduser(str(p.get("src", ""))))
        if not os.path.isfile(src):
            raise ApiError("Image file not found")
        node, _ = _find_node(kname)
        if _all_mounts(node):
            raise ApiError("Unmount all filesystems on the target first.")
        return {"job": job_start("image-restore", _image_restore_job, dev, kname, src)}
    raise ApiError("Unknown disk action")


def api_fstab(q, b):
    dev, kname = _validate_dev(q.get("dev", ""))
    node, _ = _find_node(kname)
    keys = {dev, f"UUID={node.get('uuid')}", f"LABEL={node.get('label')}", f"PARTUUID={node.get('partuuid')}"}
    rows = []
    for line in read_file("/etc/fstab").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and s.split()[0] in keys:
            rows.append(s)
    return {"entries": rows}


# --------------------------------------------------------------------------- duplicate finder

DUPES_LAST = {"groups": [], "paths": {}}
SKIP_ROOTS = ("/proc", "/sys", "/dev", "/run", "/snap")


def _hash_file(path, limit=None, job=None):
    h = hashlib.blake2b(digest_size=20)
    n = 0
    with open(path, "rb") as f:
        while True:
            if job and job["cancel"]:
                return None
            size = 1024 * 1024 if limit is None else min(1024 * 1024, limit - n)
            if size <= 0:
                break
            data = f.read(size)
            if not data:
                break
            h.update(data)
            n += len(data)
            if job is not None:
                job["_hashed"] = job.get("_hashed", 0) + len(data)
    return h.hexdigest()


def _dupes_job(job, roots, min_size, hidden, excludes, max_size, includes=None):
    by_size = collections.defaultdict(list)
    seen = set()
    scanned = 0
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: None):
            if job["cancel"]:
                return None
            if dirpath.startswith(SKIP_ROOTS) and not root.startswith(SKIP_ROOTS):
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if (hidden or not d.startswith("."))
                       and not any(fnmatch.fnmatch(d, x) for x in excludes)
                       and not os.path.join(dirpath, d).startswith(SKIP_ROOTS)]
            for fn in files:
                if not hidden and fn.startswith("."):
                    continue
                if any(fnmatch.fnmatch(fn, x) for x in excludes):
                    continue
                if includes and not any(fnmatch.fnmatch(fn.lower(), x.lower()) for x in includes):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not statmod.S_ISREG(st.st_mode) or st.st_size < min_size:
                    continue
                if max_size and st.st_size > max_size:
                    continue
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                by_size[st.st_size].append((p, st.st_mtime))
                scanned += 1
                if scanned % 400 == 0:
                    job["message"] = f"Scanning… {scanned:,} files"
                    job["live"] = {"phase": "scan", "files": scanned}
    cands = {s: l for s, l in by_size.items() if len(l) > 1}
    job["live"] = {"phase": "hash", "files": scanned, "candidates": sum(len(l) for l in cands.values())}
    total_bytes = sum(min(s, 65536) * len(l) for s, l in cands.items())
    total_bytes += sum(s * len(l) for s, l in cands.items() if s > 65536)
    total_bytes = max(total_bytes, 1)
    job["_hashed"] = 0
    groups = []
    for size, files in sorted(cands.items(), key=lambda kv: -kv[0]):
        if job["cancel"]:
            return None
        heads = collections.defaultdict(list)
        for p, mt in files:
            try:
                hh = _hash_file(p, 65536, job)
            except OSError:
                continue
            if hh:
                heads[hh].append((p, mt))
        for hh, lst in heads.items():
            if len(lst) < 2:
                continue
            if size <= 65536:
                full = {hh: lst}
            else:
                full = collections.defaultdict(list)
                for p, mt in lst:
                    try:
                        fh = _hash_file(p, None, job)
                    except OSError:
                        continue
                    if fh:
                        full[fh].append((p, mt))
            for fh, fl in full.items():
                if len(fl) > 1:
                    groups.append({"hash": fh, "size": size, "wasted": size * (len(fl) - 1),
                                   "files": [{"path": p, "mtime": mt} for p, mt in fl]})
        job["progress"] = min(0.99, job["_hashed"] / total_bytes)
        job["message"] = f"Comparing… {len(groups):,} duplicate groups"
        job["live"] = {"phase": "hash", "files": scanned, "groups": len(groups),
                       "wasted": sum(g["wasted"] for g in groups)}
    groups.sort(key=lambda g: -g["wasted"])
    truncated = len(groups) > 5000
    groups = groups[:5000]
    DUPES_LAST["groups"] = groups
    DUPES_LAST["paths"] = {f["path"]: gi for gi, g in enumerate(groups) for f in g["files"]}
    job["message"] = f"Found {len(groups):,} groups"
    return {"groups": groups, "scanned": scanned, "wasted": sum(g["wasted"] for g in groups),
            "truncated": truncated}


def api_dupes_start(q, b):
    roots = [r for r in (b.get("roots") or [HOME]) if str(r).strip()]
    excludes = [x.strip() for x in (b.get("excludes") or []) if x.strip()]
    min_size = max(1, int(b.get("min_size", 1024)))
    max_size = int(b.get("max_size", 0) or 0)
    includes = [x.strip() for x in (b.get("includes") or []) if x.strip()]
    return {"job": job_start("dupes", _dupes_job, roots, min_size, bool(b.get("hidden")), excludes, max_size,
                             includes)}


def api_dupes_delete(q, b):
    paths = [str(p) for p in b.get("paths", [])]
    mode = b.get("mode", "trash")
    sel = set(paths)
    for p in paths:
        if p not in DUPES_LAST["paths"]:
            raise ApiError(f"Not part of the last scan: {p}")
    for gi in {DUPES_LAST["paths"][p] for p in paths}:
        g = DUPES_LAST["groups"][gi]
        if all(f["path"] in sel for f in g["files"]):
            raise ApiError("Refused: at least one copy of every group must be kept.")
    done, failed = [], []
    for p in paths:
        try:
            if mode == "trash":
                if not which("gio"):
                    raise RuntimeError("gio not available; choose permanent delete")
                rc, out, err = run(["gio", "trash", "--", p], timeout=60)
                if rc != 0:
                    raise RuntimeError(err.strip() or "gio trash failed")
            else:
                os.remove(p)
            done.append(p)
        except Exception as e:
            failed.append(f"{p}: {e}")
    return {"done": done, "failed": failed}


def api_open(q, b):
    p = os.path.abspath(os.path.expanduser(str(b.get("path", ""))))
    if not os.path.exists(p):
        raise ApiError("Path not found")
    target = p if os.path.isdir(p) else os.path.dirname(p)
    if not which("xdg-open"):
        raise ApiError("xdg-open not available")
    subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return {"ok": True}


# --------------------------------------------------------------------------- speed test

CF_SERVER = {"id": "cf", "name": "Cloudflare — nearest edge (anycast)", "sponsor": "Cloudflare",
             "kind": "cf", "dl": "https://speed.cloudflare.com/__down", "ul": "https://speed.cloudflare.com/__up",
             "ping": "https://speed.cloudflare.com/__down?bytes=0", "country": "Global"}
_SERVERS = {"t": 0, "list": [], "error": None}


def _join(base, path):
    if base.startswith("//"):
        base = "https:" + base
    return base.rstrip("/") + "/" + path.lstrip("/")


def _fetch_servers():
    if time.time() - _SERVERS["t"] < 3600 and _SERVERS["list"]:
        return _SERVERS["list"]
    out = [CF_SERVER]
    try:
        req = urllib.request.Request(LIBRESPEED_LIST, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        for s in data:
            base = s.get("server", "")
            if not base:
                continue
            name = s.get("name", "")
            country = name.split(",")[-1].split("(")[0].strip() if "," in name else ""
            out.append({"id": f"ls{s.get('id')}", "name": name, "sponsor": s.get("sponsorName", ""),
                        "kind": "ls", "dl": _join(base, s.get("dlURL", "garbage.php")),
                        "ul": _join(base, s.get("ulURL", "empty.php")),
                        "ping": _join(base, s.get("pingURL", "empty.php")), "country": country})
        _SERVERS["error"] = None
    except Exception as e:
        _SERVERS["error"] = f"LibreSpeed list unavailable: {e}"
    _SERVERS["t"] = time.time()
    _SERVERS["list"] = out
    return out


def _conn(url, timeout=10):
    u = urllib.parse.urlsplit(url)
    ctx = ssl.create_default_context()
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    port = u.port or (443 if u.scheme == "https" else 80)
    if u.scheme != "https":
        raise ApiError("Only HTTPS servers are used")
    if proxy:
        pu = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        c = http.client.HTTPSConnection(pu.hostname, pu.port or 8080, timeout=timeout, context=ctx)
        hdr = {}
        if pu.username:
            import base64
            cred = base64.b64encode(f"{urllib.parse.unquote(pu.username)}:{urllib.parse.unquote(pu.password or '')}".encode()).decode()
            hdr["Proxy-Authorization"] = "Basic " + cred
        c.set_tunnel(u.hostname, port, headers=hdr)
    else:
        c = http.client.HTTPSConnection(u.hostname, port, timeout=timeout, context=ctx)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return c, path


def _with_param(path, key, val):
    return path + ("&" if "?" in path else "?") + f"{key}={val}"


def _ping(server, samples=10, job=None):
    c, path = _conn(server["ping"], timeout=6)
    times = []
    try:
        c.request("GET", _with_param(path, "r", secrets.token_hex(4)), headers={"User-Agent": USER_AGENT})
        c.getresponse().read()
        for i in range(samples):
            if job and job["cancel"]:
                break
            t0 = time.perf_counter()
            c.request("GET", _with_param(path, "r", secrets.token_hex(4)),
                      headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
            c.getresponse().read()
            times.append((time.perf_counter() - t0) * 1000)
            if job:
                job["live"] = {"phase": "ping", "ping_samples": times[:]}
                job["progress"] = 0.08 * (i + 1) / samples
    finally:
        c.close()
    if not times:
        raise ApiError("Ping failed")
    jitter = sum(abs(times[i] - times[i - 1]) for i in range(1, len(times))) / max(len(times) - 1, 1)
    return min(times), sum(times) / len(times), jitter


def _transfer(job, server, direction, streams, duration, p0, p1):
    counters = [0] * streams
    stop = threading.Event()
    errors = []
    deadline = time.time() + duration
    payload = os.urandom(1024 * 1024)

    def dl_worker(i):
        buf = bytearray(256 * 1024)
        while not stop.is_set():
            try:
                c, path = _conn(server["dl"], timeout=10)
                if server["kind"] == "cf":
                    path = _with_param(path, "bytes", 50_000_000)
                else:
                    path = _with_param(path, "ckSize", 50)
                path = _with_param(path, "r", secrets.token_hex(4))
                c.request("GET", path, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache",
                                                "Accept-Encoding": "identity"})
                r = c.getresponse()
                while not stop.is_set():
                    n = r.readinto(buf)
                    if not n:
                        break
                    counters[i] += n
                c.close()
            except Exception as e:
                errors.append(str(e))
                time.sleep(0.3)
                if len(errors) > 20:
                    return

    def ul_worker(i):
        size = 10 * 1024 * 1024
        while not stop.is_set():
            try:
                c, path = _conn(server["ul"], timeout=10)
                c.putrequest("POST", _with_param(path, "r", secrets.token_hex(4)), skip_accept_encoding=True)
                c.putheader("User-Agent", USER_AGENT)
                c.putheader("Content-Type", "application/octet-stream")
                c.putheader("Content-Length", str(size))
                c.endheaders()
                sent = 0
                while sent < size and not stop.is_set():
                    chunk = memoryview(payload)[:min(256 * 1024, size - sent)]
                    c.send(chunk)
                    sent += len(chunk)
                    counters[i] += len(chunk)
                if sent >= size:
                    c.getresponse().read()
                c.close()
            except Exception as e:
                errors.append(str(e))
                time.sleep(0.3)
                if len(errors) > 20:
                    return

    worker = dl_worker if direction == "download" else ul_worker
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(streams)]
    t_start = time.time()
    for t in threads:
        t.start()
    samples = []  # (t, total_bytes)
    curve = []
    warm = min(2.0, duration * 0.2)
    while time.time() < deadline and not job["cancel"]:
        time.sleep(0.2)
        now = time.time()
        tot = sum(counters)
        samples.append((now, tot))
        win = [s for s in samples if s[0] >= now - 1.0]
        rate = (win[-1][1] - win[0][1]) * 8 / (win[-1][0] - win[0][0]) if len(win) > 1 and win[-1][0] > win[0][0] else 0
        curve.append(round(rate / 1e6, 2))
        job["live"] = dict(job["live"], phase=direction, mbps=round(rate / 1e6, 2), curve=curve[-200:])
        job["progress"] = p0 + (p1 - p0) * min(1, (now - t_start) / duration)
        if errors and len(errors) > 20 and not any(t.is_alive() for t in threads):
            break
    stop.set()
    end = time.time()
    total = sum(counters)
    base = next((s for s in samples if s[0] >= t_start + warm), samples[0] if samples else (t_start, 0))
    span = end - base[0]
    mbps = (total - base[1]) * 8 / span / 1e6 if span > 0 else 0
    if total == 0:
        raise ApiError(f"{direction} failed: " + (errors[-1] if errors else "no data"))
    return round(mbps, 2), curve, total


def _speed_job(job, sid, streams, duration):
    servers = _fetch_servers()
    srv = next((s for s in servers if s["id"] == sid), None)
    if not srv:
        raise ApiError("Unknown server")
    colo = None
    if srv["kind"] == "cf":
        try:
            c, path = _conn("https://speed.cloudflare.com/cdn-cgi/trace", timeout=6)
            c.request("GET", path, headers={"User-Agent": USER_AGENT})
            t = c.getresponse().read().decode(errors="replace")
            c.close()
            m = re.search(r"^colo=(\w+)", t, re.M)
            colo = m.group(1) if m else None
        except Exception:
            pass
    job["live"] = {"phase": "ping", "server": srv["name"], "colo": colo}
    job["message"] = "Measuring latency…"
    pmin, pavg, jitter = _ping(srv, 10, job)
    job["live"].update({"ping": round(pmin, 1), "jitter": round(jitter, 1)})
    job["message"] = "Download…"
    dl, dl_curve, dl_bytes = _transfer(job, srv, "download", streams, duration, 0.08, 0.54)
    job["live"].update({"download": dl})
    if job["cancel"]:
        return None
    job["message"] = "Upload…"
    ul, ul_curve, ul_bytes = _transfer(job, srv, "upload", streams, duration, 0.54, 1.0)
    res = {"ts": time.time(), "server": srv["name"], "server_id": sid, "sponsor": srv["sponsor"], "colo": colo,
           "ping": round(pmin, 1), "ping_avg": round(pavg, 1), "jitter": round(jitter, 1),
           "download": dl, "upload": ul, "dl_curve": dl_curve, "ul_curve": ul_curve,
           "dl_bytes": dl_bytes, "ul_bytes": ul_bytes, "streams": streams, "duration": duration}
    try:
        ensure_data_dir()
        hp = os.path.join(DATA_DIR, "speed_history.json")
        hist = json.loads(read_file(hp, "[]") or "[]")
        hist.insert(0, {k: v for k, v in res.items() if k not in ("dl_curve", "ul_curve")})
        with open(hp, "w") as f:
            json.dump(hist[:100], f)
    except Exception:
        pass
    job["message"] = "Complete"
    return res


def api_speed_servers(q, b):
    lst = _fetch_servers()
    return {"servers": lst, "warning": _SERVERS["error"]}


def _pingall_job(job, ids):
    servers = [s for s in _fetch_servers() if not ids or s["id"] in ids]
    res = {}
    lock = threading.Lock()
    sem = threading.Semaphore(10)
    done = [0]

    def one(s):
        with sem:
            try:
                pmin, _, _ = _ping(s, 3)
                v = round(pmin, 1)
            except Exception:
                v = None
            with lock:
                res[s["id"]] = v
                done[0] += 1
                job["progress"] = done[0] / len(servers)
                job["live"] = {"pings": dict(res)}
    ts = [threading.Thread(target=one, args=(s,), daemon=True) for s in servers]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    return {"pings": res}


def api_speed_pingall(q, b):
    return {"job": job_start("pingall", _pingall_job, b.get("ids") or [])}


def api_speed_start(q, b):
    sid = str(b.get("server", "cf"))
    streams = max(1, min(int(b.get("streams", 4)), 8))
    duration = max(4, min(int(b.get("duration", 10)), 30))
    return {"job": job_start("speedtest", _speed_job, sid, streams, duration)}


def api_speed_history(q, b):
    hp = os.path.join(DATA_DIR, "speed_history.json")
    try:
        return {"history": json.loads(read_file(hp, "[]") or "[]")}
    except Exception:
        return {"history": []}


def api_speed_history_clear(q, b):
    hp = os.path.join(DATA_DIR, "speed_history.json")
    if os.path.exists(hp):
        os.remove(hp)
    return {"ok": True}


# --------------------------------------------------------------------------- v1.1 additions
import grp
import gzip
import shlex


def stream_job(job, cmd, use_priv=True, env=None, ok_codes=(0,), parse=None):
    """Run a command, streaming combined output into job['live']['log']."""
    exe = which(cmd[0])
    if not exe:
        hint = {"sfdisk": "fdisk", "sgdisk": "gdisk", "e2fsck": "e2fsprogs", "fsck.vfat": "dosfstools",
                "fsck.exfat": "exfatprogs", "ntfsfix": "ntfs-3g", "xfs_repair": "xfsprogs"}.get(cmd[0], cmd[0])
        raise ApiError(f"{cmd[0]} is not installed (sudo apt install {hint}).")
    cmd = [exe] + list(cmd[1:])
    if use_priv and os.geteuid() != 0:
        pk = which("pkexec")
        if not pk:
            raise ApiError("pkexec is not installed (sudo apt install pkexec)")
        cmd = [pk] + cmd
        job["message"] = "Waiting for authorization…"
    log = []
    if not isinstance(job.get("live"), dict):
        job["live"] = {}
    job["live"]["log"] = log
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                         env=env, errors="replace")
    for line in p.stdout:
        line = line.rstrip("\n")
        if parse is not None:
            try:
                if parse(line):
                    continue
            except Exception:
                pass
        log.append(line[:2000])
        if len(log) > 1500:
            del log[:500]
        if line.strip():
            job["message"] = line.strip()[:160]
        if job["cancel"]:
            p.terminate()
            break
    rc = p.wait()
    if job["cancel"]:
        return {"output": "\n".join(log[-400:]), "cancelled": True}
    if rc not in ok_codes:
        if rc in (126, 127) and use_priv and len(log) <= 1:
            raise ApiError("Authorization was cancelled or denied.")
        raise ApiError(f"Command failed (exit {rc}).\n" + "\n".join(log[-15:]))
    job["message"] = "Completed"
    return {"output": "\n".join(log[-600:]), "rc": rc}


def _priv_popen(cmd, **kw):
    exe = which(cmd[0]) or cmd[0]
    cmd = [exe] + list(cmd[1:])
    if os.geteuid() != 0:
        pk = which("pkexec")
        if not pk:
            raise ApiError("pkexec is not installed (sudo apt install pkexec)")
        cmd = [pk] + cmd
    return subprocess.Popen(cmd, **kw)


def _os_release():
    osr = {}
    for line in read_file("/etc/os-release").splitlines():
        k, _, v = line.partition("=")
        osr[k] = v.strip().strip('"')
    return osr


# --------------------------------------------------------------------------- v1.3 progress reporting + filesystem health

class _Stages:
    """Weighted multi-step progress for one job: job['live']['steps'] / ['step'] and job['progress']."""

    def __init__(self, job, steps):
        self.job, self.steps = job, steps          # [(label, weight)]
        self.total = float(sum(w for _, w in steps)) or 1.0
        job.setdefault("live", {})
        job["live"]["steps"] = [s for s, _ in steps]
        job["live"]["step"] = 0
        job["live"]["indeterminate"] = True

    def go(self, label, frac=None, msg=None, indeterminate=None):
        names = [s for s, _ in self.steps]
        i = names.index(label) if label in names else self.job["live"].get("step", 0)
        before = sum(w for _, w in self.steps[:i])
        w = self.steps[i][1]
        self.job["live"]["step"] = i
        if frac is not None:
            self.job["progress"] = min(0.995, (before + w * max(0.0, min(1.0, frac))) / self.total)
            self.job["live"]["indeterminate"] = False
        else:
            self.job["progress"] = min(0.995, before / self.total)
        if indeterminate is not None:
            self.job["live"]["indeterminate"] = indeterminate
        if msg:
            self.job["message"] = msg

    def skip(self, label):
        live = self.job["live"]
        live.setdefault("skipped", [])
        if label not in live["skipped"]:
            live["skipped"].append(label)


class _RateMeter:
    """Throughput / ETA for data-moving jobs (backup, restore, verify)."""

    def __init__(self, job, total, label="MB/s"):
        self.job, self.total, self.t0 = job, max(int(total or 0), 1), time.time()
        self.win = collections.deque(maxlen=12)
        self.last = 0
        job.setdefault("live", {})
        job["live"].update(series=[], total=self.total, done=0, unit=label, indeterminate=False)

    def tick(self, done, frac_base=0.0, frac_span=1.0):
        now = time.time()
        self.win.append((now, done))
        if now - self.last < 0.4:
            return
        self.last = now
        (t1, d1), (t2, d2) = self.win[0], self.win[-1]
        rate = (d2 - d1) / (t2 - t1) if t2 > t1 else 0
        live = self.job["live"]
        live["done"], live["rate"] = done, rate
        live["eta"] = (self.total - done) / rate if rate > 0 and done < self.total else None
        live["series"] = (live.get("series") or [])[-119:] + [round(rate / 1e6, 2)]
        self.job["progress"] = min(0.995, frac_base + frac_span * done / self.total)


E2PASS = {1: "inodes, blocks and sizes", 2: "directory structure", 3: "directory connectivity",
          4: "reference counts", 5: "group summary information"}


def _progress_parser(job, tool, stages=None, stage=None):
    """Return a stream_job line hook that turns tool output into real progress."""
    def setp(frac, msg):
        if stages:
            stages.go(stage, frac, msg)
        else:
            job["progress"] = min(0.995, frac)
            job["live"]["indeterminate"] = False
            job["message"] = msg

    if tool == "e2fsck":
        def hook(line):
            m = re.match(r"^\s*([1-5]) (\d+) (\d+) \S+\s*$", line)
            if not m:
                return False
            ps, cur, mx = int(m.group(1)), int(m.group(2)), int(m.group(3))
            setp(((ps - 1) + (cur / mx if mx else 0)) / 5,
                 f"e2fsck pass {ps}/5 — {E2PASS.get(ps, '')} ({100 * cur / max(mx, 1):.0f}%)")
            job["live"]["pass"] = ps
            return True
        return hook
    if tool == "xfs_repair":
        def hook(line):
            m = re.match(r"^Phase (\d) - (.*)$", line.strip())
            if m:
                n = int(m.group(1))
                setp((n - 1) / 7, f"xfs_repair phase {n}/7 — {m.group(2).strip()}")
            return False
        return hook
    return None


def _ud_jobs():
    r = ud_call("/org/freedesktop/UDisks2", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                timeout_ms=5000)
    out = []
    for path, ifs in ((r[0] if r else {}) or {}).items():
        j = ifs.get("org.freedesktop.UDisks2.Job")
        if j:
            out.append(dict(j, path=path))
    return out


class _UdWatch(threading.Thread):
    """Mirror UDisks2 Job objects (Progress, Rate, ExpectedEndTime) for our device into job['live']."""

    def __init__(self, job, obj_paths, stages=None, stage=None):
        super().__init__(daemon=True)
        self.job, self.paths, self.stages, self.stage = job, set(p for p in obj_paths if p), stages, stage
        self.ev = threading.Event()

    def run(self):
        while not self.ev.is_set():
            try:
                for j in _ud_jobs():
                    if not set(j.get("Objects") or []) & self.paths:
                        continue
                    live = self.job["live"]
                    live["udisks_op"] = j.get("Operation")
                    rate = j.get("Rate") or 0
                    if rate:
                        live["rate"] = rate
                        live["series"] = (live.get("series") or [])[-119:] + [round(rate / 1e6, 2)]
                    end = j.get("ExpectedEndTime") or 0
                    live["eta"] = max(0, end / 1e6 - time.time()) if end else None
                    if j.get("ProgressValid"):
                        frac = float(j.get("Progress") or 0)
                        if self.stages:
                            self.stages.go(self.stage, frac)
                        else:
                            self.job["progress"] = min(0.995, frac)
                            live["indeterminate"] = False
                    break
            except Exception:
                pass
            self.ev.wait(0.7)

    def stop(self):
        self.ev.set()


def _drive_obj(bp):
    try:
        d = ud_props(bp, "org.freedesktop.UDisks2.Block").get("Drive")
        return d if d and d != "/" else None
    except Exception:
        return None


# ---------------------------------------------------------------- filesystem health (needs-repair detection)

FS_STATUS_FILE = lambda: os.path.join(DATA_DIR, "fs_status.json")  # noqa: E731
_FS_LOCK = threading.Lock()


def _fs_status():
    try:
        return json.loads(read_file(FS_STATUS_FILE(), "{}") or "{}")
    except Exception:
        return {}


def _fs_record(key, mode, clean, engine):
    if not key:
        return
    with _FS_LOCK:
        ensure_data_dir()
        st = _fs_status()
        st[key] = {"ts": time.time(), "mode": mode, "clean": clean, "engine": engine}
        with open(FS_STATUS_FILE(), "w") as f:
            json.dump(st, f, indent=1)


_KLOG = {"t": 0, "ev": {}, "ok": None, "src": None}
KPATS = [
    (re.compile(r"(?:FAT|exFAT)-fs \((\w+)\): Volume was not properly unmounted"), "dirty",
     "Volume was not properly unmounted (dirty flag set)"),
    (re.compile(r"EXT4-fs \((\w+)\): warning: mounting fs with errors"), "fserror",
     "ext4 reports errors recorded in the filesystem"),
    (re.compile(r"EXT4-fs error \(device (\w+)\)"), "fserror", "ext4 filesystem error"),
    (re.compile(r"ntfs3(?:\(|: )(\w+)\)?:? .*(?:dirty|mark volume as dirty)", re.I), "dirty",
     "NTFS volume is marked dirty"),
    (re.compile(r"XFS \((\w+)\): (?:Corruption|Metadata corruption|Internal error)"), "fserror",
     "XFS metadata corruption"),
    (re.compile(r"BTRFS (?:error|critical) \(device (\w+)"), "fserror", "Btrfs error"),
    (re.compile(r"(?:I/O error, dev|Buffer I/O error on dev(?:ice)?) (\w+)"), "io", "Disk I/O error"),
]


def _parse_klog_line(line):
    for rx, kind, text in KPATS:
        m = rx.search(line)
        if m:
            return m.group(1), kind, text
    return None


def _kernel_events():
    """{kname: [(ts, kind, text)]} from this boot's kernel log; cached 15 s."""
    if time.time() - _KLOG["t"] < 15:
        return _KLOG["ev"]
    ev, ok, src = {}, False, None
    lines = []
    if which("journalctl"):
        rc, out, err = run(["journalctl", "-k", "-b", "-q", "--no-pager", "-o", "short-unix", "-n", "30000"],
                           timeout=20)
        if rc == 0 and out.strip() and "insufficient permissions" not in (out + err).lower():
            ok, src = True, "journalctl"
            for line in out.splitlines():
                p = line.split(None, 1)
                try:
                    lines.append((float(p[0]), line))
                except (ValueError, IndexError):
                    continue
    if not ok and which("dmesg"):
        rc, out, err = run(["dmesg", "--time-format", "iso"], timeout=10)
        if rc == 0 and out.strip():
            ok, src = True, "dmesg"
            import datetime
            for line in out.splitlines():
                try:
                    ts = datetime.datetime.fromisoformat(line.split(None, 1)[0].replace(",", ".")).timestamp()
                except Exception:
                    ts = 0
                lines.append((ts, line))
    for ts, line in lines:
        r = _parse_klog_line(line)
        if r:
            ev.setdefault(r[0], []).append((ts, r[1], r[2]))
    _KLOG.update(t=time.time(), ev=ev, ok=ok, src=src)
    return ev


def _dev_since(kname):
    """When this device node appeared (events before that belong to whatever used the name earlier)."""
    try:
        return os.stat(f"/dev/{kname}").st_ctime - 2
    except OSError:
        return 0


def _disk_of(kname):
    p = os.path.realpath(f"/sys/class/block/{kname}")
    return os.path.basename(os.path.dirname(p)) if os.path.exists(os.path.join(p, "partition")) else kname


def _fs_health(node, ev, st):
    k = node["kname"]
    key = node.get("uuid") or node["path"]
    rec = st.get(key)
    last_ok = rec["ts"] if rec and rec.get("clean") else 0
    since = _dev_since(k)
    issues = []
    for ts, kind, text in ev.get(k, []):
        if kind in ("dirty", "fserror") and ts > last_ok and ts >= since and text not in issues:
            issues.append(text)
    if node.get("fstype", "") in ("ext2", "ext3", "ext4") or os.path.exists(f"/sys/fs/ext4/{k}"):
        try:
            n = int(read_file(f"/sys/fs/ext4/{k}/errors_count", "0").strip() or 0)
            if n > 0:
                issues.append(f"ext4 has recorded {n} error(s) since the last fsck")
        except ValueError:
            pass
    if rec and rec.get("clean") is False:
        issues.append(f"Last {rec.get('mode', 'check')} ({time.strftime('%Y-%m-%d %H:%M', time.localtime(rec['ts']))}) "
                      "reported problems")
    state = "repair" if issues else ("clean" if rec and rec.get("clean") else "unknown")
    return {"state": state, "issues": issues, "checked": rec["ts"] if rec else None,
            "engine": rec.get("engine") if rec else None}


def _annotate_health(disk):
    ev = _kernel_events()
    st = _fs_status()

    def walk(n):
        if n.get("fstype") and n["fstype"] not in ("crypto_LUKS", "LVM2_member", "swap", "linux_raid_member"):
            n["fshealth"] = _fs_health(n, ev, st)
        for c in n.get("children", []):
            walk(c)
    walk(disk)
    since = _dev_since(disk["kname"])
    io = 0
    for k, lst in ev.items():
        if _disk_of(k) == disk["kname"] or k == disk["kname"]:
            io += sum(1 for ts, kind, _ in lst if kind == "io" and ts >= since)
    disk["io_errors"] = io
    disk["klog"] = _KLOG["ok"]


# ---------------------------------------------------------------- filesystem maintenance

FSCK = {
    "ext2": (["e2fsck", "-f", "-n", "-C", "1"], ["e2fsck", "-f", "-y", "-C", "1"]),
    "ext3": (["e2fsck", "-f", "-n", "-C", "1"], ["e2fsck", "-f", "-y", "-C", "1"]),
    "ext4": (["e2fsck", "-f", "-n", "-C", "1"], ["e2fsck", "-f", "-y", "-C", "1"]),
    "vfat": (["fsck.vfat", "-n"], ["fsck.vfat", "-a", "-w"]),
    "exfat": (["fsck.exfat", "-n"], ["fsck.exfat", "-y"]),
    "ntfs": (["ntfsfix", "-n"], ["ntfsfix", "-d"]),
    "xfs": (["xfs_repair", "-n"], ["xfs_repair"]),
    "btrfs": (["btrfs", "check", "--readonly"], None),
    "f2fs": (["fsck.f2fs", "--dry-run"], ["fsck.f2fs", "-f", "-y"]),
}


def _fs_maint_job(job, mode, dev, kname, engine, auto_unmount, remount):
    node, _ = _find_node(kname)
    fstype = node.get("fstype") or ""
    if not fstype and which("blkid"):
        rc, out, _ = priv(["blkid", "-o", "value", "-s", "TYPE", dev], timeout=60)
        fstype = out.strip() if rc == 0 else ""
    was_mounted = list(node.get("mountpoints") or [])
    if set(was_mounted) & CRITICAL_MOUNTS:
        raise ApiError("This is a running system volume. Boot from a live USB to check or repair it.")
    bp = UD_BLOCK + _ud_escape(kname)
    FS = "org.freedesktop.UDisks2.Filesystem"
    result = {"engine": None, "clean": None, "output": ""}
    WORK = "Check" if mode == "check" else "Repair"
    stg = _Stages(job, ([("Unmount", 1)] if was_mounted else []) + [(WORK, 12)] +
                  ([("Remount", 1)] if was_mounted and remount else []) + [("Refresh", 1)])
    if was_mounted:
        if not auto_unmount:
            raise ApiError("The volume is mounted. Enable 'unmount automatically' or unmount it first.")
        stg.go("Unmount", None, "Unmounting…")
        r = _mount_verified("unmount", dev, bp, FS)
        if not r["verified"]:
            raise ApiError(r["message"] + " — close any program using it and try again.")
    try:
        used_udisks = False
        if engine in ("auto", "udisks"):
            w = None
            try:
                stg.go(WORK, None, ("Checking" if mode == "check" else "Repairing") +
                       " with UDisks2 (authorize if prompted)…", indeterminate=True)
                if _gi_available():
                    w = _UdWatch(job, [bp, _drive_obj(bp)], stg, WORK)
                    w.start()
                r = ud_call(bp, FS, "Check" if mode == "check" else "Repair", "(a{sv})", [_vopts({})])
                result["engine"] = "UDisks2"
                result["clean"] = bool(r[0]) if r else None
                used_udisks = True
            except ApiError as e:
                if engine == "udisks":
                    raise
                result["udisks_error"] = str(e)
            finally:
                if w:
                    w.stop()
        if not used_udisks:
            tool = FSCK.get(fstype)
            if not tool:
                raise ApiError(f"No checker available for filesystem type '{fstype or 'unknown'}'.")
            cmd = tool[0] if mode == "check" else tool[1]
            if cmd is None:
                raise ApiError(f"Automatic repair of {fstype} is not offered (btrfs check --repair can destroy data). "
                               "Use 'btrfs scrub' on the mounted filesystem instead.")
            if not which(cmd[0]):
                raise ApiError(f"{cmd[0]} is not installed.")
            stg.go(WORK, None, f"Running {cmd[0]} (authorize if prompted)…", indeterminate=True)
            r = stream_job(job, cmd + [dev], ok_codes=tuple(range(0, 256)),
                           parse=_progress_parser(job, cmd[0], stg, WORK))
            rc = r.get("rc", 0)
            result["engine"] = " ".join(cmd[:2])
            result["output"] = r.get("output", "")
            if fstype.startswith("ext"):
                result["clean"] = rc == 0 if mode == "check" else rc in (0, 1)
                if rc >= 8:
                    raise ApiError("e2fsck operational error.\n" + result["output"][-1500:])
            else:
                result["clean"] = rc == 0
    finally:
        if was_mounted and remount:
            try:
                stg.go("Remount", None, "Remounting…")
                r = _mount_verified("mount", dev, bp, FS)
                result["remounted"] = r["verified"]
                result["mountpoints"] = r["mountpoints"]
                if not r["verified"]:
                    result["remount_error"] = r["message"]
            except Exception as e:  # noqa
                result["remount_error"] = str(e)
    stg.go("Refresh", None, "Waiting for the system to register the change…")
    _settle(3)
    _HEALTH_CACHE.clear()
    _fs_record(node.get("uuid") or dev, mode, result.get("clean"), result.get("engine"))
    job["message"] = "Done"
    return result


# ---------------------------------------------------------------- backups

BACKUPS_FILE = lambda: os.path.join(DATA_DIR, "backups.json")  # noqa: E731
_BK_LOCK = threading.Lock()


def _bk_load():
    try:
        return json.loads(read_file(BACKUPS_FILE(), "[]") or "[]")
    except Exception:
        return []


def _bk_add(entry):
    with _BK_LOCK:
        ensure_data_dir()
        lst = _bk_load()
        lst.insert(0, entry)
        with open(BACKUPS_FILE(), "w") as f:
            json.dump(lst[:300], f, indent=1)


def _sha256_file(path, job=None, label="Computing checksum", stages=None):
    h = hashlib.sha256()
    size = os.path.getsize(path) or 1
    done = 0
    meter = _RateMeter(job, size) if job is not None and stages is None else None
    with open(path, "rb") as f:
        while True:
            if job and job["cancel"]:
                return None
            data = f.read(8 * 1024 * 1024)
            if not data:
                break
            h.update(data)
            done += len(data)
            if job:
                job["message"] = f"{label}… {100 * done / size:.0f}%"
                if stages:
                    stages[0].go(stages[1], done / size)
                elif meter:
                    meter.tick(done)
    return h.hexdigest()


def _with_ext(dest, mode, comp):
    base = {"image": ".img", "files": ".tar", "ptable": ".sfdisk"}[mode]
    ext = base + {"none": "", "gzip": ".gz", "zstd": ".zst"}.get(comp, "")
    return dest if dest.endswith(ext) else dest + ext


def _open_writer(dest, comp):
    if comp == "zstd":
        z = which("zstd")
        if not z:
            raise ApiError("zstd is not installed (sudo apt install zstd).")
        if os.path.exists(dest):
            raise ApiError("Destination exists")
        p = subprocess.Popen([z, "-q", "-T0", "-3", "-", "-o", dest], stdin=subprocess.PIPE)
        return p.stdin, p
    if comp == "gzip":
        return gzip.open(dest, "xb", compresslevel=3), None
    return open(dest, "xb"), None


def _backup_job(job, mode, dev, kname, dest, comp, checksum, label):
    dest = _with_ext(dest, mode, comp)
    if os.path.exists(dest):
        raise ApiError(f"{dest} already exists.")
    node, _ = _find_node(kname)
    t0 = time.time()
    done = 0
    src_proc = fd = None
    if mode == "image":
        bp = UD_BLOCK + _ud_escape(kname)
        total = int(ud_props(bp, "org.freedesktop.UDisks2.Block").get("Size") or node["size"] or 1)
        job["message"] = "Waiting for authorization…"
        fd = ud_call(bp, "org.freedesktop.UDisks2.Block", "OpenForBackup", "(a{sv})", [_vopts({})], want_fd=True)
        read = lambda: os.read(fd, 4 * 1024 * 1024)  # noqa: E731
    else:
        mps = node.get("mountpoints") or []
        if not mps:
            raise ApiError("Files backup needs the volume to be mounted.")
        mp = mps[0]
        total = node.get("fsused") or 1
        tar = which("tar")
        src_proc = _priv_popen([tar, "--one-file-system", "--numeric-owner", "--xattrs", "--acls", "-cpf", "-",
                                "-C", mp, "."], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        job["message"] = "Waiting for authorization…"
        read = lambda: src_proc.stdout.read(4 * 1024 * 1024)  # noqa: E731
    writer, wproc = _open_writer(dest, comp)
    stg = _Stages(job, [("Read & " + ("compress" if comp != "none" else "write"), 20)] +
                  ([("Checksum", 4)] if checksum else []) + [("Register", 1)])
    meter = _RateMeter(job, total)
    span = 20 / (25.0 if checksum else 21.0)
    try:
        while not job["cancel"]:
            data = read()
            if not data:
                break
            writer.write(data)
            done += len(data)
            el = max(time.time() - t0, 1e-3)
            meter.tick(done, 0, span)
            job["live"]["step"] = 0
            job["message"] = f"{done / 1e9:.2f} / {total / 1e9:.2f} GB read · {done / el / 1e6:.0f} MB/s"
    finally:
        try:
            writer.close()
        except Exception:
            pass
        if wproc:
            wproc.wait()
        if fd is not None:
            os.close(fd)
        if src_proc:
            if job["cancel"]:
                src_proc.terminate()
            rc = src_proc.wait()
            err = src_proc.stderr.read().decode(errors="replace") if src_proc.stderr else ""
            if rc not in (0, 1) and not job["cancel"]:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                raise ApiError(f"tar failed (exit {rc}): {err.strip()[-800:] or 'authorization cancelled?'}")
    if job["cancel"]:
        try:
            os.remove(dest)
        except OSError:
            pass
        return None
    if wproc and wproc.returncode:
        raise ApiError("zstd compression failed")
    digest = None
    if checksum:
        stg.go("Checksum", 0, "Computing SHA-256…")
        digest = _sha256_file(dest, job, stages=(stg, "Checksum"))
        with open(dest + ".sha256", "w") as f:
            f.write(f"{digest}  {os.path.basename(dest)}\n")
    entry = {"ts": time.time(), "type": mode, "dev": dev, "label": label, "file": dest, "source_bytes": done,
             "size": os.path.getsize(dest), "compression": comp, "sha256": digest,
             "fstype": node.get("fstype"), "duration": round(time.time() - t0, 1)}
    _bk_add(entry)
    job["message"] = "Backup complete"
    return entry


def _open_reader(src):
    if src.endswith(".zst"):
        z = which("zstd")
        if not z:
            raise ApiError("zstd is not installed (sudo apt install zstd).")
        p = subprocess.Popen([z, "-dcq", src], stdout=subprocess.PIPE)
        return p.stdout, p, None
    if src.endswith(".gz"):
        g = gzip.open(src, "rb")
        return g, None, g.fileobj
    f = open(src, "rb")
    return f, None, f


def _verify_sidecar(job, src):
    side = src + ".sha256"
    if not os.path.exists(side):
        return None
    want = read_file(side).split()[0].lower()
    got = _sha256_file(src, job, "Verifying checksum")
    if got is None:
        return None
    if got != want:
        raise ApiError(f"Checksum mismatch for {os.path.basename(src)} — the backup file is corrupted.")
    return True


def _restore_image_job(job, dev, kname, src, verify):
    if verify:
        job["message"] = "Verifying checksum before restore…"
        _verify_sidecar(job, src)
    bp = UD_BLOCK + _ud_escape(kname)
    size = int(ud_props(bp, "org.freedesktop.UDisks2.Block").get("Size") or 0)
    fsize = os.path.getsize(src)
    if not src.endswith((".gz", ".zst")) and fsize > size:
        raise ApiError(f"Image ({fsize} B) is larger than the device ({size} B).")
    reader, rproc, pos = _open_reader(src)
    job["message"] = "Waiting for authorization…"
    fd = ud_call(bp, "org.freedesktop.UDisks2.Block", "OpenForRestore", "(a{sv})", [_vopts({})], want_fd=True)
    done = 0
    t0 = time.time()
    meter = _RateMeter(job, fsize if pos is not None else size)
    try:
        while not job["cancel"]:
            data = reader.read(4 * 1024 * 1024)
            if not data:
                break
            if done + len(data) > size:
                raise ApiError("Image data is larger than the target device; restore stopped.")
            mv = memoryview(data)
            while mv:
                n = os.write(fd, mv)
                mv = mv[n:]
            done += len(data)
            el = max(time.time() - t0, 1e-3)
            frac = (pos.tell() / fsize) if pos is not None and fsize else done / max(size, 1)
            meter.tick(pos.tell() if pos is not None else done)
            job["live"]["written"] = done
            job["progress"] = min(0.99, frac)
            job["message"] = f"{done / 1e9:.2f} GB written · {done / el / 1e6:.0f} MB/s"
        job["message"] = "Flushing to device…"
        os.fsync(fd)
    finally:
        os.close(fd)
        reader.close()
        if rproc:
            rproc.wait()
    return {"bytes": done}


def _restore_files_job(job, src, target, verify):
    if verify:
        _verify_sidecar(job, src)
    return stream_job(job, ["tar", "--numeric-owner", "--xattrs", "--acls", "-xpf", src, "-C", target],
                      ok_codes=(0, 1))


def api_backups(q, b):
    out = []
    for e in _bk_load():
        e = dict(e)
        e["exists"] = os.path.exists(e.get("file", ""))
        out.append(e)
    return {"backups": out, "zstd": bool(which("zstd")), "sgdisk": bool(which("sgdisk"))}


def _bk_verify_job(job, f):
    if not os.path.exists(f + ".sha256"):
        raise ApiError("No .sha256 file next to this backup.")
    _verify_sidecar(job, f)
    return {"ok": True}


def api_backup_verify(q, b):
    f = str(b.get("file", ""))
    if f not in {e.get("file") for e in _bk_load()}:
        raise ApiError("Unknown backup")
    return {"job": job_start("bk-verify", _bk_verify_job, f)}


def api_backup_forget(q, b):
    f = str(b.get("file", ""))
    with _BK_LOCK:
        lst = [e for e in _bk_load() if e.get("file") != f]
        ensure_data_dir()
        with open(BACKUPS_FILE(), "w") as fh:
            json.dump(lst, fh, indent=1)
    if b.get("delete_file") and os.path.isfile(f):
        os.remove(f)
        if os.path.exists(f + ".sha256"):
            os.remove(f + ".sha256")
    return {"ok": True}


def _ptable_backup(dev, dest):
    dest = _with_ext(dest, "ptable", "none")
    if os.path.exists(dest):
        raise ApiError(f"{dest} already exists.")
    out = priv_ok(["sfdisk", "--dump", dev], timeout=120)
    with open(dest, "x") as f:
        f.write(out)
    entry = {"ts": time.time(), "type": "ptable", "dev": dev, "label": "Partition table", "file": dest,
             "size": os.path.getsize(dest), "compression": "none", "sha256": None, "source_bytes": len(out)}
    _bk_add(entry)
    return entry


def api_disk_action2(q, b):
    """v1.1 disk actions; falls back to v1.0 handler."""
    action = str(b.get("action", ""))
    p = b.get("params") or {}
    if action in ("check", "repair"):
        dev, kname = _validate_dev(b.get("dev", ""))
        if action == "repair" and b.get("confirm") != dev:
            raise ApiError(f"Confirmation text must exactly match {dev}.")
        engine = p.get("engine", "auto")
        if engine not in ("auto", "udisks", "fsck"):
            raise ApiError("Invalid engine")
        return {"job": job_start("fs-" + action, _fs_maint_job, action, dev, kname, engine,
                                 bool(p.get("auto_unmount", True)), bool(p.get("remount", True)))}
    if action in ("backup", "image_create"):
        dev, kname = _validate_dev(b.get("dev", ""))
        mode = p.get("mode", "image")
        comp = p.get("compression", "none")
        if mode not in ("image", "files") or comp not in ("none", "gzip", "zstd"):
            raise ApiError("Invalid backup options")
        dest = os.path.abspath(os.path.expanduser(str(p.get("dest", ""))))
        if not os.path.isdir(os.path.dirname(dest)):
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
            except OSError:
                raise ApiError("Destination folder does not exist and could not be created.")
        node, _ = _find_node(kname)
        try:
            ddev = os.stat(os.path.dirname(dest)).st_dev
            same = any(os.stat(m).st_dev == ddev for m in _all_mounts(node) if m and m.startswith("/"))
        except OSError:
            same = False
        if same:
            raise ApiError("The destination folder is on the volume being backed up. Save the backup to another drive.")
        label = node.get("label") or node.get("model") or node.get("name")
        return {"job": job_start("backup", _backup_job, mode, dev, kname, dest, comp, bool(p.get("checksum", True)),
                                 label)}
    if action == "image_restore":
        dev, kname = _validate_dev(b.get("dev", ""))
        _guard_destructive(dev, kname, b.get("confirm"))
        src = os.path.abspath(os.path.expanduser(str(p.get("src", ""))))
        if not os.path.isfile(src):
            raise ApiError("Image file not found")
        node, _ = _find_node(kname)
        if _all_mounts(node):
            raise ApiError("Unmount all filesystems on the target first.")
        if src.endswith((".tar", ".tar.gz", ".tar.zst")):
            raise ApiError("That is a files archive. Use 'Restore files' on a mounted volume instead.")
        return {"job": job_start("image-restore", _restore_image_job, dev, kname, src, bool(p.get("verify", True)))}
    if action == "files_restore":
        src = os.path.abspath(os.path.expanduser(str(p.get("src", ""))))
        target = os.path.abspath(os.path.expanduser(str(p.get("target", ""))))
        if not os.path.isfile(src):
            raise ApiError("Archive not found")
        if not os.path.isdir(target):
            raise ApiError("Target folder does not exist")
        if target in ("/", "/usr", "/etc", "/boot", "/var"):
            raise ApiError("Refusing to extract over a system directory.")
        if b.get("confirm") != target:
            raise ApiError(f"Confirmation text must exactly match {target}.")
        return {"job": job_start("files-restore", _restore_files_job, src, target, bool(p.get("verify", True)))}
    if action == "pt_verify":
        dev, kname = _validate_dev(b.get("dev", ""))
        return {"job": job_start("pt-verify", stream_job, ["sfdisk", "--verify", dev], True, None, (0, 1))}
    if action == "pt_backup":
        dev, kname = _validate_dev(b.get("dev", ""))
        dest = os.path.abspath(os.path.expanduser(str(p.get("dest", ""))))
        if not os.path.isdir(os.path.dirname(dest)):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
        e = _ptable_backup(dev, dest)
        return {"ok": True, "message": f"Partition table saved to {e['file']}"}
    if action == "pt_restore":
        dev, kname = _validate_dev(b.get("dev", ""))
        node = _guard_destructive(dev, kname, b.get("confirm"))
        if _all_mounts(node):
            raise ApiError("Unmount all partitions on this disk first.")
        src = os.path.abspath(os.path.expanduser(str(p.get("src", ""))))
        if not os.path.isfile(src):
            raise ApiError("Dump file not found")
        script = 'sfdisk "$1" < "$2" && (blockdev --rereadpt "$1" 2>/dev/null || true)'
        return {"job": job_start("pt-restore", stream_job, ["sh", "-c", script, "mysyspilot", dev, src])}
    if action == "pt_repair_gpt":
        dev, kname = _validate_dev(b.get("dev", ""))
        if b.get("confirm") != dev:
            raise ApiError(f"Confirmation text must exactly match {dev}.")
        if not which("sgdisk"):
            raise ApiError("sgdisk is not installed (sudo apt install gdisk).")
        script = 'sgdisk -e "$1" && sgdisk -v "$1"'
        return {"job": job_start("gpt-repair", stream_job, ["sh", "-c", script, "mysyspilot", dev])}
    return api_disk_action(q, b)


# ---------------------------------------------------------------- duplicates of a file + file/text search

SEARCH_LAST = {"paths": set()}


def _iter_files(job, roots, hidden, excludes, includes=None):
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: None):
            if job["cancel"]:
                return
            dirs[:] = [d for d in dirs if (hidden or not d.startswith("."))
                       and not any(fnmatch.fnmatch(d, x) for x in excludes)
                       and not os.path.join(dirpath, d).startswith(SKIP_ROOTS)]
            for fn in files:
                if not hidden and fn.startswith("."):
                    continue
                if any(fnmatch.fnmatch(fn, x) for x in excludes):
                    continue
                if includes and not any(fnmatch.fnmatch(fn.lower(), x.lower()) for x in includes):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if statmod.S_ISREG(st.st_mode):
                    yield p, st


def _dupes_of_job(job, ref, roots, hidden, excludes):
    ref = os.path.abspath(os.path.expanduser(ref))
    st = os.stat(ref)
    size = st.st_size
    refkey = (st.st_dev, st.st_ino)
    job["message"] = "Hashing reference file…"
    refhead = _hash_file(ref, 65536)
    reffull = _hash_file(ref) if size > 65536 else refhead
    matches, scanned, checked = [], 0, 0
    for p, s in _iter_files(job, roots, hidden, excludes):
        scanned += 1
        if scanned % 500 == 0:
            job["message"] = f"Scanning… {scanned:,} files · {len(matches)} match(es)"
            job["live"] = {"phase": "scan", "files": scanned, "groups": 1 if matches else 0,
                           "wasted": size * len(matches)}
        if s.st_size != size or (s.st_dev, s.st_ino) == refkey:
            continue
        checked += 1
        try:
            if _hash_file(p, 65536) != refhead:
                continue
            if size > 65536 and _hash_file(p) != reffull:
                continue
        except OSError:
            continue
        matches.append({"path": p, "mtime": s.st_mtime})
    if job["cancel"]:
        return None
    groups = []
    if matches:
        groups.append({"hash": reffull, "size": size, "wasted": size * len(matches), "reference": ref,
                       "files": [{"path": ref, "mtime": st.st_mtime, "ref": True}] + matches})
    DUPES_LAST["groups"] = groups
    DUPES_LAST["paths"] = {f["path"]: gi for gi, g in enumerate(groups) for f in g["files"]}
    job["message"] = f"{len(matches)} identical cop{'y' if len(matches) == 1 else 'ies'} found"
    return {"groups": groups, "scanned": scanned, "wasted": size * len(matches), "truncated": False,
            "reference": ref}


def api_dupes_of(q, b):
    ref = str(b.get("file", "")).strip()
    if not os.path.isfile(os.path.expanduser(ref)):
        raise ApiError("Reference file not found")
    roots = [r for r in (b.get("roots") or [HOME]) if str(r).strip()]
    excludes = [x.strip() for x in (b.get("excludes") or []) if x.strip()]
    return {"job": job_start("dupes-of", _dupes_of_job, ref, roots, bool(b.get("hidden")), excludes)}


def _search_job(job, roots, name, name_mode, text, case, text_regex, min_size, max_size, days, hidden,
                excludes, max_results, max_file):
    if name_mode == "regex" and name:
        nre = re.compile(name, 0 if case else re.I)
        name_ok = lambda fn: bool(nre.search(fn))  # noqa: E731
    elif name_mode == "glob" and name:
        pats = [x.strip() for x in name.split(",") if x.strip()]
        name_ok = lambda fn: any(fnmatch.fnmatch(fn if case else fn.lower(), x if case else x.lower()) for x in pats)  # noqa
    elif name:
        nl = name if case else name.lower()
        name_ok = lambda fn: nl in (fn if case else fn.lower())  # noqa: E731
    else:
        name_ok = lambda fn: True  # noqa: E731
    tre = None
    if text:
        tre = re.compile(text if text_regex else re.escape(text), 0 if case else re.I)
    cutoff = time.time() - days * 86400 if days else 0
    results, scanned, skipped = [], 0, 0
    truncated = False
    for p, st in _iter_files(job, roots, hidden, excludes):
        scanned += 1
        if scanned % 300 == 0:
            job["message"] = f"Scanned {scanned:,} files · {len(results):,} match(es)"
            job["live"] = {"phase": "search", "files": scanned, "matches": len(results)}
        fn = os.path.basename(p)
        if not name_ok(fn):
            continue
        if st.st_size < min_size or (max_size and st.st_size > max_size) or st.st_mtime < cutoff:
            continue
        hit = {"path": p, "size": st.st_size, "mtime": st.st_mtime}
        if tre is not None:
            if st.st_size > max_file:
                skipped += 1
                continue
            try:
                with open(p, "rb") as f:
                    head = f.read(8192)
                    if b"\x00" in head:
                        continue
                    f.seek(0)
                    count, first = 0, None
                    for n, raw in enumerate(f, 1):
                        line = raw.decode("utf-8", errors="replace")
                        m = tre.search(line)
                        if m:
                            count += len(tre.findall(line))
                            if first is None:
                                s = max(0, m.start() - 60)
                                first = (n, line[s:m.end() + 100].strip(), m.start() - s, m.end() - s)
                            if count >= 1000:
                                break
                    if not count:
                        continue
                    hit.update(line=first[0], preview=first[1][:220], hl=[first[2], first[3]], count=count)
            except (OSError, ValueError):
                continue
        results.append(hit)
        if len(results) >= max_results:
            truncated = True
            break
    SEARCH_LAST["paths"] = {r["path"] for r in results}
    job["message"] = f"{len(results):,} result(s)"
    return {"results": results, "scanned": scanned, "skipped_large": skipped, "truncated": truncated}


def api_search_start(q, b):
    roots = [r for r in (b.get("roots") or [HOME]) if str(r).strip()]
    name = str(b.get("name", "")).strip()
    text = str(b.get("text", ""))
    if not name and not text:
        raise ApiError("Enter a file name and/or text to search for.")
    nm = b.get("name_mode", "contains")
    if nm not in ("contains", "glob", "regex"):
        raise ApiError("Invalid name mode")
    try:
        if nm == "regex" and name:
            re.compile(name)
        if b.get("text_regex") and text:
            re.compile(text)
    except re.error as e:
        raise ApiError(f"Invalid regular expression: {e}")
    excludes = [x.strip() for x in (b.get("excludes") or []) if x.strip()]
    return {"job": job_start("search", _search_job, roots, name, nm, text, bool(b.get("case")),
                             bool(b.get("text_regex")), int(b.get("min_size", 0) or 0), int(b.get("max_size", 0) or 0),
                             int(b.get("days", 0) or 0), bool(b.get("hidden")), excludes,
                             max(1, min(int(b.get("max_results", 2000) or 2000), 20000)),
                             int(b.get("max_file_mb", 50) or 50) * 1024 * 1024)}


def _remove_paths(paths, mode):
    done, failed = [], []
    for p in paths:
        try:
            if mode == "trash":
                if not which("gio"):
                    raise RuntimeError("gio not available; choose permanent delete")
                rc, out, err = run(["gio", "trash", "--", p], timeout=60)
                if rc != 0:
                    raise RuntimeError(err.strip() or "gio trash failed")
            else:
                os.remove(p)
            done.append(p)
        except Exception as e:
            failed.append(f"{p}: {e}")
    return {"done": done, "failed": failed}


def api_search_delete(q, b):
    paths = [str(p) for p in b.get("paths", [])]
    for p in paths:
        if p not in SEARCH_LAST["paths"]:
            raise ApiError(f"Not part of the last search: {p}")
    r = _remove_paths(paths, b.get("mode", "trash"))
    SEARCH_LAST["paths"] -= set(r["done"])
    return r


# ---------------------------------------------------------------- docker

DK_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-:/@]{0,254}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_.\-]{0,31}\$?$")


def _docker_bin():
    d = which("docker")
    if not d:
        raise ApiError("Docker is not installed. Use the Install panel on this page.")
    return d


def dk(args, timeout=60):
    rc, out, err = run([_docker_bin()] + args, timeout=timeout)
    if rc != 0:
        msg = (err or out).strip()
        if "permission denied" in msg.lower():
            raise ApiError("PERMISSION: Your user cannot reach the Docker daemon. Add yourself to the docker group "
                           "(Setup tab), then use 'Relaunch with docker group' or log out and back in.")
        if "connect to the docker" in msg.lower() or "is the docker daemon running" in msg.lower():
            raise ApiError("DAEMON: The Docker daemon is not running. Start it from the Engine card.")
        raise ApiError(msg[-1500:] or f"docker exited with {rc}")
    return out


def dk_json(args, timeout=60):
    out = dk(args, timeout)
    res = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, list):
            res.extend(v)
        else:
            res.append(v)
    return res


def _ref(x):
    x = str(x)
    if not DK_REF.match(x):
        raise ApiError(f"Invalid Docker name or ID: {x!r}")
    return x


def api_docker_status(q, b):
    user = pwd.getpwuid(os.getuid()).pw_name
    osr = _os_release()
    fam = (osr.get("ID", "") + " " + osr.get("ID_LIKE", "")).lower()
    st = {"installed": bool(which("docker")), "user": user,
          "distro": {"id": osr.get("ID"), "name": osr.get("PRETTY_NAME"),
                     "codename": osr.get("UBUNTU_CODENAME") or osr.get("VERSION_CODENAME"),
                     "family": "ubuntu" if "ubuntu" in fam else "debian" if "debian" in fam else None},
          "sg": bool(which("sg")), "socket": os.path.exists("/var/run/docker.sock")}
    try:
        g = grp.getgrnam("docker")
        st["group_exists"] = True
        st["in_group"] = user in g.gr_mem or pwd.getpwuid(os.getuid()).pw_gid == g.gr_gid
        st["group_active"] = g.gr_gid in os.getgroups() or os.geteuid() == 0
    except KeyError:
        st["group_exists"] = st["in_group"] = st["group_active"] = False
    if which("systemctl"):
        st["service_active"] = run(["systemctl", "is-active", "docker.service"], timeout=10)[1].strip()
        st["service_enabled"] = run(["systemctl", "is-enabled", "docker.service"], timeout=10)[1].strip()
    pk = {}
    if which("dpkg-query"):
        rc, out, _ = run(["dpkg-query", "-W", "-f", "${Package}\t${Version}\t${db:Status-Abbrev}\n",
                          "docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin",
                          "docker-buildx-plugin", "docker.io", "docker-compose-v2", "docker-compose"], timeout=20)
        for line in out.splitlines():
            f = line.split("\t")
            if len(f) == 3 and f[2].startswith("ii"):
                pk[f[0]] = f[1]
    st["packages"] = pk
    st["source"] = "Docker official repo (docker-ce)" if "docker-ce" in pk else \
        "Distribution package (docker.io)" if "docker.io" in pk else ("Other / manual" if st["installed"] else None)
    st["daemon_json"] = read_file("/etc/docker/daemon.json", "")
    if st["installed"]:
        rc, out, err = run([which("docker"), "version", "--format", "{{json .}}"], timeout=20)
        try:
            v = json.loads(out.strip().splitlines()[0]) if out.strip() else {}
        except Exception:
            v = {}
        st["client"] = (v.get("Client") or {}).get("Version")
        st["server"] = (v.get("Server") or {}).get("Version") if v.get("Server") else None
        rc, out, err = run([which("docker"), "info", "--format", "{{json .}}"], timeout=25)
        info = {}
        try:
            info = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except Exception:
            pass
        if rc == 0 and info.get("ServerVersion"):
            st["access"] = "ok"
            keys = ("ServerVersion", "Containers", "ContainersRunning", "ContainersPaused", "ContainersStopped",
                    "Images", "Driver", "DockerRootDir", "OperatingSystem", "KernelVersion", "NCPU", "MemTotal",
                    "CgroupDriver", "CgroupVersion", "LoggingDriver", "Name", "Warnings", "SecurityOptions")
            st["info"] = {k: info.get(k) for k in keys}
            st["info"]["Swarm"] = (info.get("Swarm") or {}).get("LocalNodeState")
        else:
            e = ((info.get("ServerErrors") or [""])[0] if info else "") or err
            st["access"] = "denied" if "permission denied" in e.lower() else "down"
            st["access_error"] = e.strip()[-600:]
        rc, out, _ = run([which("docker"), "compose", "version", "--short"], timeout=15)
        st["compose"] = out.strip() if rc == 0 else None
        rc, out, _ = run([which("docker"), "buildx", "version"], timeout=15)
        st["buildx"] = out.strip().split()[1] if rc == 0 and len(out.split()) > 1 else None
    return st


def api_docker_list(q, b):
    what = q.get("what", "containers")
    if what == "containers":
        return {"items": dk_json(["ps", "-a", "--no-trunc", "--format", "{{json .}}"])}
    if what == "stats":
        return {"items": dk_json(["stats", "--no-stream", "--no-trunc", "--format", "{{json .}}"], timeout=30)}
    if what == "images":
        return {"items": dk_json(["image", "ls", "--format", "{{json .}}"])}
    if what == "volumes":
        return {"items": dk_json(["volume", "ls", "--format", "{{json .}}"])}
    if what == "networks":
        return {"items": dk_json(["network", "ls", "--format", "{{json .}}"])}
    if what == "compose":
        try:
            return {"items": dk_json(["compose", "ls", "-a", "--format", "json"])}
        except ApiError as e:
            if "PERMISSION" in str(e) or "DAEMON" in str(e):
                raise
            return {"items": [], "warning": str(e)}
    if what == "df":
        return {"items": dk_json(["system", "df", "--format", "{{json .}}"], timeout=60)}
    raise ApiError("Unknown list")


def api_docker_logs(q, b):
    cid = _ref(q.get("id", ""))
    tail = max(10, min(int(q.get("tail", 300) or 300), 10000))
    args = [_docker_bin(), "logs", "--tail", str(tail)]
    if q.get("ts") == "1":
        args.append("--timestamps")
    try:
        p = subprocess.run(args + [cid], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    except subprocess.TimeoutExpired:
        raise ApiError("Timed out reading logs")
    return {"log": p.stdout.decode("utf-8", errors="replace")[-400000:], "rc": p.returncode}


def api_docker_inspect(q, b):
    kind = q.get("kind", "container")
    if kind not in ("container", "image", "volume", "network"):
        raise ApiError("Invalid kind")
    out = dk([kind, "inspect", _ref(q.get("id", ""))])
    return {"data": json.loads(out)}


def _docker_install_script(method):
    if method == "official":
        return r"""set -e
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
case "$ID $ID_LIKE" in
  *ubuntu*) DIST=ubuntu; CODE="${UBUNTU_CODENAME:-$VERSION_CODENAME}";;
  *debian*) DIST=debian; CODE="$VERSION_CODENAME";;
  *) echo "Unsupported distribution: $ID (Docker's apt repo supports Ubuntu and Debian families)"; exit 2;;
esac
echo "==> Target: $DIST $CODE"
echo "==> Removing conflicting packages (if present)"
for p in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
  if dpkg -s "$p" >/dev/null 2>&1; then apt-get remove -y "$p"; fi
done
echo "==> Installing prerequisites"
apt-get update
apt-get install -y ca-certificates curl
echo "==> Adding Docker's GPG key and apt repository"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/$DIST/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
  "$DIST" "$CODE" "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.sources
apt-get update
echo "==> Installing Docker Engine"
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
"""
    return r"""set -e
export DEBIAN_FRONTEND=noninteractive
echo "==> Installing distribution packages"
apt-get update
apt-get install -y docker.io
apt-get install -y docker-compose-v2 || apt-get install -y docker-compose || echo "(compose package not available)"
"""


def _docker_post_script(enable, add_group):
    s = ""
    if enable:
        s += 'echo "==> Enabling and starting docker"\nsystemctl enable --now docker.service\n' \
             'systemctl enable containerd.service 2>/dev/null || true\n'
    if add_group:
        s += 'echo "==> Adding $1 to the docker group"\ngroupadd -f docker\nusermod -aG docker "$1"\n'
    s += 'echo "==> Done"\ndocker version || true\n'
    return s


def api_docker_action(q, b):
    kind = b.get("kind")
    action = b.get("action")
    ids = [_ref(x) for x in (b.get("ids") or [])]
    p = b.get("params") or {}
    user = pwd.getpwuid(os.getuid()).pw_name
    if kind == "setup":
        if action == "install":
            method = p.get("method", "official")
            if method not in ("official", "distro"):
                raise ApiError("Invalid method")
            if not which("apt-get"):
                raise ApiError("Automatic install supports apt-based systems (Ubuntu/Debian family) only.")
            script = _docker_install_script(method) + _docker_post_script(bool(p.get("enable", True)),
                                                                            bool(p.get("add_group")))
            return {"job": job_start("docker-install", stream_job, ["bash", "-c", script, "mysyspilot", user])}
        if action == "uninstall":
            purge_data = bool(p.get("purge_data"))
            if purge_data and b.get("confirm") != "DELETE DOCKER DATA":
                raise ApiError("Type DELETE DOCKER DATA to also remove images, containers and volumes.")
            script = ('set -e\nexport DEBIAN_FRONTEND=noninteractive\n'
                      'systemctl stop docker.service docker.socket 2>/dev/null || true\n'
                      'PK=""\nfor p in docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin '
                      'docker-ce-rootless-extras docker.io docker-compose-v2 docker-compose; do '
                      'dpkg -s "$p" >/dev/null 2>&1 && PK="$PK $p"; done\n'
                      'if [ -n "$PK" ]; then apt-get purge -y $PK; fi\napt-get autoremove -y\n')
            if purge_data:
                script += 'echo "==> Removing /var/lib/docker and /var/lib/containerd"\nrm -rf /var/lib/docker /var/lib/containerd\n'
            if p.get("remove_repo"):
                script += 'rm -f /etc/apt/sources.list.d/docker.sources /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc\n'
            script += 'echo "==> Docker removed"\n'
            return {"job": job_start("docker-uninstall", stream_job, ["bash", "-c", script])}
        if action in ("start", "stop", "restart", "enable", "disable"):
            units = ["docker.service", "docker.socket"] if action in ("stop", "disable") else ["docker.service"]
            priv_ok(["systemctl", action] + units, timeout=180)
            return {"ok": True}
        if action == "group_add":
            if not USER_RE.match(user):
                raise ApiError("Unexpected user name")
            priv_ok(["sh", "-c", 'groupadd -f docker && usermod -aG docker "$1"', "mysyspilot", user], timeout=120)
            return {"ok": True, "message": f"{user} added to the docker group. Relaunch or log out/in to apply."}
        if action == "group_remove":
            priv_ok(["gpasswd", "-d", user, "docker"], timeout=120)
            return {"ok": True}
        if action == "relaunch":
            if not which("sg"):
                raise ApiError("'sg' is not available; log out and back in instead.")
            script = os.path.abspath(sys.argv[0])
            cmd = f"sleep 1; exec {shlex.quote(sys.executable)} {shlex.quote(script)}"
            subprocess.Popen(["sg", "docker", "-c", cmd], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return {"ok": True}
        if action == "daemon_json":
            text = str(p.get("text", "")).strip() or "{}"
            try:
                obj = json.loads(text)
                if not isinstance(obj, dict):
                    raise ValueError("top level must be an object")
            except ValueError as e:
                raise ApiError(f"Invalid JSON: {e}")
            ensure_data_dir()
            fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix="daemon-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(obj, f, indent=2)
                f.write("\n")
            os.chmod(tmp, 0o644)
            try:
                s = ('mkdir -p /etc/docker /var/backups/mysyspilot && '
                     '{ [ -f /etc/docker/daemon.json ] && cp /etc/docker/daemon.json '
                     '"/var/backups/mysyspilot/daemon.json.$(date +%Y%m%d%H%M%S)" || true; } && '
                     'cp -- "$1" /etc/docker/daemon.json && chmod 0644 /etc/docker/daemon.json')
                if p.get("restart"):
                    s += " && systemctl restart docker.service"
                priv_ok(["sh", "-c", s, "mysyspilot", tmp], timeout=180)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return {"ok": True}
        raise ApiError("Unknown setup action")
    if kind == "container":
        if action in ("start", "stop", "restart", "pause", "unpause", "kill"):
            dk([action] + ids, timeout=180)
            return {"ok": True}
        if action == "rm":
            dk(["rm"] + (["-f"] if p.get("force") else []) + (["-v"] if p.get("volumes") else []) + ids, timeout=180)
            return {"ok": True}
        if action == "rename":
            dk(["rename", ids[0], _ref(p.get("name", ""))])
            return {"ok": True}
        if action == "update_restart":
            pol = p.get("policy", "no")
            if pol not in ("no", "always", "unless-stopped", "on-failure"):
                raise ApiError("Invalid restart policy")
            dk(["update", f"--restart={pol}"] + ids)
            return {"ok": True}
        if action == "exec":
            cmd = str(p.get("cmd", "")).strip()
            if not cmd:
                raise ApiError("Enter a command")
            argv = ["sh", "-c", cmd] if p.get("shell", True) else shlex.split(cmd)
            try:
                r = subprocess.run([_docker_bin(), "exec", ids[0]] + argv, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=int(p.get("timeout", 60)))
            except subprocess.TimeoutExpired:
                raise ApiError("Command timed out")
            return {"output": r.stdout.decode("utf-8", errors="replace")[-200000:], "rc": r.returncode}
        if action == "run":
            img = _ref(p.get("image", ""))
            argv = ["run", "-d"]
            if p.get("name"):
                argv += ["--name", _ref(p["name"])]
            for port in p.get("ports") or []:
                port = str(port).strip()
                if port:
                    if not re.match(r"^[0-9.:\[\]a-fA-F]*[0-9]+(-[0-9]+)?(/(tcp|udp|sctp))?$", port):
                        raise ApiError(f"Invalid port mapping: {port}")
                    argv += ["-p", port]
            for e in p.get("env") or []:
                e = str(e).strip()
                if e:
                    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*(=.*)?$", e):
                        raise ApiError(f"Invalid environment entry: {e}")
                    argv += ["-e", e]
            for v in p.get("volumes") or []:
                v = str(v).strip()
                if v:
                    if v.startswith("-") or "\n" in v:
                        raise ApiError(f"Invalid volume: {v}")
                    argv += ["-v", v]
            pol = p.get("restart", "no")
            if pol not in ("no", "always", "unless-stopped", "on-failure"):
                raise ApiError("Invalid restart policy")
            argv += ["--restart", pol]
            if p.get("network"):
                argv += ["--network", _ref(p["network"])]
            argv += [img] + (shlex.split(str(p.get("command", ""))) if p.get("command") else [])
            return {"job": job_start("docker-run", stream_job, [_docker_bin()] + argv, False)}
        raise ApiError("Unknown container action")
    if kind == "image":
        if action == "rm":
            dk(["image", "rm"] + (["-f"] if p.get("force") else []) + ids, timeout=180)
            return {"ok": True}
        if action == "pull":
            return {"job": job_start("docker-pull", stream_job, [_docker_bin(), "pull", _ref(p.get("image", ""))], False)}
        if action == "prune":
            return {"job": job_start("docker-prune", stream_job,
                                     [_docker_bin(), "image", "prune", "-f"] + (["-a"] if p.get("all") else []), False)}
        if action == "tag":
            dk(["image", "tag", ids[0], _ref(p.get("tag", ""))])
            return {"ok": True}
        if action == "build":
            ctx = os.path.realpath(os.path.expanduser(str(p.get("dir", "")).strip()))
            if not os.path.isfile(os.path.join(ctx, "Dockerfile")):
                raise ApiError(f"No Dockerfile in {ctx}")
            tag = _ref(str(p.get("tag", "")).strip().lower())
            return {"job": job_start("docker-build", stream_job, [_docker_bin(), "build", "-t", tag, ctx], False)}
        raise ApiError("Unknown image action")
    if kind == "volume":
        if action == "rm":
            dk(["volume", "rm"] + (["-f"] if p.get("force") else []) + ids)
            return {"ok": True}
        if action == "create":
            dk(["volume", "create", _ref(p.get("name", ""))])
            return {"ok": True}
        if action == "prune":
            return {"job": job_start("docker-prune", stream_job,
                                     [_docker_bin(), "volume", "prune", "-f"] + (["-a"] if p.get("all") else []), False)}
        raise ApiError("Unknown volume action")
    if kind == "network":
        if action == "rm":
            for i in ids:
                if i in ("bridge", "host", "none"):
                    raise ApiError("Built-in networks cannot be removed.")
            dk(["network", "rm"] + ids)
            return {"ok": True}
        if action == "create":
            drv = p.get("driver", "bridge")
            if drv not in ("bridge", "macvlan", "ipvlan", "overlay"):
                raise ApiError("Invalid driver")
            argv = ["network", "create", "--driver", drv]
            if p.get("subnet"):
                if not re.match(r"^[0-9a-fA-F.:]+/\d{1,3}$", str(p["subnet"])):
                    raise ApiError("Invalid subnet")
                argv += ["--subnet", str(p["subnet"])]
            dk(argv + [_ref(p.get("name", ""))])
            return {"ok": True}
        if action == "prune":
            return {"job": job_start("docker-prune", stream_job, [_docker_bin(), "network", "prune", "-f"], False)}
        raise ApiError("Unknown network action")
    if kind == "compose":
        files = [os.path.abspath(os.path.expanduser(str(f).strip())) for f in (p.get("files") or []) if str(f).strip()]
        for f in files:
            if not os.path.isfile(f):
                raise ApiError(f"Compose file not found: {f}")
        argv = [_docker_bin(), "compose"]
        if p.get("project"):
            argv += ["-p", _ref(str(p["project"]).lower())]
        for f in files:
            argv += ["-f", f]
        sub = {"up": ["up", "-d", "--remove-orphans"], "down": ["down"], "restart": ["restart"], "stop": ["stop"],
               "start": ["start"], "pull": ["pull"], "ps": ["ps", "-a"]}.get(action)
        if not sub:
            raise ApiError("Unknown compose action")
        if not files and not p.get("project"):
            raise ApiError("Compose project or file required")
        return {"job": job_start("compose-" + action, stream_job, argv + sub, False)}
    if kind == "system":
        if action == "prune":
            argv = [_docker_bin(), "system", "prune", "-f"] + (["-a"] if p.get("all") else []) + \
                   (["--volumes"] if p.get("volumes") else [])
            return {"job": job_start("docker-prune", stream_job, argv, False)}
        raise ApiError("Unknown system action")
    raise ApiError("Unknown Docker request")


# ---- v1.4 Docker examples & templates: write template files into a folder under $HOME
_TPL_NAME = re.compile(r"^(?:[A-Za-z0-9_][A-Za-z0-9._-]{0,63}/)?\.?[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")


def api_docker_template_save(q, b):
    home = os.path.realpath(HOME)
    d = os.path.realpath(os.path.expanduser(str(b.get("dir", "")).strip()))
    if not (d == home or d.startswith(home + os.sep)) or d == home:
        raise ApiError(f"Choose a folder inside your home directory ({home})")
    files = b.get("files") or {}
    if not isinstance(files, dict) or not files or len(files) > 20:
        raise ApiError("No template files")
    for n, c in files.items():
        if not _TPL_NAME.match(n) or ".." in n or not isinstance(c, str) or len(c) > 200_000:
            raise ApiError(f"Invalid template file: {n}")
    exist = [n for n in files if os.path.exists(os.path.join(d, n))]
    if exist and not b.get("overwrite"):
        return {"exists": exist, "dir": d}
    os.makedirs(d, mode=0o755, exist_ok=True)
    written = []
    for n, c in files.items():
        fp = os.path.join(d, n)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        if not os.path.realpath(os.path.dirname(fp)).startswith(d):
            raise ApiError(f"Refusing to write outside {d}")
        secret = os.path.basename(n) == ".env"
        fd = os.open(fp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600 if secret else 0o644)
        with os.fdopen(fd, "w") as f:
            f.write(c)
        if secret:
            os.chmod(fp, 0o600)
        written.append(fp)
    return {"ok": True, "dir": d, "written": written}


# --------------------------------------------------------------------------- v1.2 data recovery (TestDisk / PhotoRec style)
import errno
import struct

HELPER_INSTALLED = next((p for p in ("/usr/libexec/mysyspilot/msp-diskhelper", "/usr/lib/mysyspilot/msp-diskhelper")
                         if os.path.exists(p)), "/usr/libexec/mysyspilot/msp-diskhelper")
HELPER_SRC = r'''
import os, sys, struct, stat
def rx(n):
    b = bytearray()
    while len(b) < n:
        c = sys.stdin.buffer.read(n - len(b))
        if not c:
            raise SystemExit(0)
        b += c
    return bytes(b)
def main():
    out = sys.stdout.buffer
    if len(sys.argv) < 3:
        sys.exit(2)
    dev, mode = sys.argv[1], sys.argv[2]
    try:
        st = os.stat(dev)
        if not stat.S_ISBLK(st.st_mode):
            raise OSError("not a block device")
        fd = os.open(dev, os.O_RDONLY if mode == "r" else (os.O_RDWR | os.O_EXCL))
        size = os.lseek(fd, 0, os.SEEK_END)
    except OSError as e:
        m = str(e).encode()[:900]
        out.write(b"ER" + struct.pack("<I", len(m)) + m); out.flush(); return
    out.write(b"OK" + struct.pack("<Q", size)); out.flush()
    while True:
        op, off, n = struct.unpack("<BQI", rx(13))
        if op == 1:
            try:
                d = os.pread(fd, n, off)
            except OSError:
                d = b""
            out.write(struct.pack("<I", len(d)) + d); out.flush()
        elif op == 5:
            stride, ln = struct.unpack("<QI", rx(12))
            parts = []
            for i in range(n):
                try:
                    d = os.pread(fd, ln, off + i * stride)
                except OSError:
                    d = b""
                parts.append(d.ljust(ln, b"\0"))
            d = b"".join(parts)
            out.write(struct.pack("<I", len(d)) + d); out.flush()
        elif op == 2:
            data = rx(n)
            w = -1
            if mode == "rw":
                try:
                    w = os.pwrite(fd, data, off); os.fsync(fd)
                except OSError:
                    w = -1
            out.write(struct.pack("<i", w)); out.flush()
        else:
            break
main()
'''

MiB = 1024 * 1024


def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u64(b, o): return struct.unpack_from("<Q", b, o)[0]
def be16(b, o): return struct.unpack_from(">H", b, o)[0]
def be32(b, o): return struct.unpack_from(">I", b, o)[0]
def be64(b, o): return struct.unpack_from(">Q", b, o)[0]


def _txt(b):
    return bytes(b).split(b"\0")[0].decode("latin-1", errors="replace").strip()


class RawDev:
    """Random access to a block device (through a pkexec'd helper) or an image file."""

    def __init__(self, path, write=False):
        self.path = os.path.realpath(os.path.expanduser(path))
        self.lock = threading.Lock()
        self.p = self.f = None
        self.fd = None
        self.write_mode = write
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            raise ApiError(f"{path} not found")
        if statmod.S_ISREG(st.st_mode):
            self.f = open(self.path, "r+b" if write else "rb")
            self.size = os.path.getsize(self.path)
            self.kind = "image"
            return
        if not statmod.S_ISBLK(st.st_mode):
            raise ApiError("Target must be a block device or a disk image file")
        self.kind = "block"
        if os.geteuid() == 0 and not os.environ.get("MSP_TEST_HELPER"):
            try:
                self.fd = os.open(self.path, os.O_RDONLY if not write else (os.O_RDWR | os.O_EXCL))
            except OSError as e:
                raise ApiError(f"Cannot open {self.path}: {e.strerror}" +
                               (" — it is mounted or in use; unmount it first." if e.errno == errno.EBUSY else ""))
            self.size = os.lseek(self.fd, 0, os.SEEK_END)
            return
        pk = which("pkexec")
        if not pk and not os.environ.get("MSP_TEST_HELPER"):
            raise ApiError("pkexec is required for raw disk access (sudo apt install pkexec)")
        cmd = [HELPER_INSTALLED] if os.path.exists(HELPER_INSTALLED) else ["/usr/bin/python3", "-I", "-c", HELPER_SRC]
        pre = [] if os.environ.get("MSP_TEST_HELPER") else [pk]
        self.p = subprocess.Popen(pre + cmd + [self.path, "rw" if write else "r"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        hdr = self.p.stdout.read(2)
        if hdr == b"OK":
            self.size = u64(self._rx(8), 0)
        elif hdr == b"ER":
            n = u32(self._rx(4), 0)
            msg = self._rx(n).decode(errors="replace")
            self.close()
            raise ApiError(f"Cannot open {self.path}: {msg}" +
                           (" — it is mounted or in use; unmount it first." if "busy" in msg.lower() else ""))
        else:
            self.close()
            raise ApiError("Authorization was cancelled or denied.")

    def _rx(self, n):
        b = bytearray()
        while len(b) < n:
            c = self.p.stdout.read(n - len(b))
            if not c:
                raise ApiError("Disk helper stopped unexpectedly")
            b += c
        return bytes(b)

    def read(self, off, n):
        if off < 0 or off >= self.size or n <= 0:
            return b""
        n = min(n, self.size - off)
        with self.lock:
            if self.f:
                self.f.seek(off)
                return self.f.read(n)
            if self.p is None:
                return os.pread(self.fd, n, off)
            out = bytearray()
            while n > 0:
                k = min(n, 32 * MiB)
                self.p.stdin.write(struct.pack("<BQI", 1, off, k))
                self.p.stdin.flush()
                ln = u32(self._rx(4), 0)
                out += self._rx(ln)
                if ln < k:
                    break
                off += k
                n -= k
            return bytes(out)

    def strided(self, off, count, stride, ln):
        if self.p is None:
            return b"".join(self.read(off + i * stride, ln).ljust(ln, b"\0") for i in range(count))
        with self.lock:
            self.p.stdin.write(struct.pack("<BQI", 5, off, count) + struct.pack("<QI", stride, ln))
            self.p.stdin.flush()
            n = u32(self._rx(4), 0)
            return self._rx(n)

    def write(self, off, data):
        if not self.write_mode:
            raise ApiError("Device opened read-only")
        with self.lock:
            if self.f:
                self.f.seek(off)
                self.f.write(data)
                self.f.flush()
                os.fsync(self.f.fileno())
                return len(data)
            if self.p is None:
                w = os.pwrite(self.fd, data, off)
                os.fsync(self.fd)
                return w
            self.p.stdin.write(struct.pack("<BQI", 2, off, len(data)) + data)
            self.p.stdin.flush()
            w = struct.unpack("<i", self._rx(4))[0]
            if w != len(data):
                raise ApiError("Write failed on device")
            return w

    def close(self):
        try:
            if self.f:
                self.f.close()
            if self.fd is not None:
                os.close(self.fd)
            if self.p:
                try:
                    self.p.stdin.write(struct.pack("<BQI", 9, 0, 0))
                    self.p.stdin.flush()
                except Exception:
                    pass
                try:
                    self.p.wait(timeout=3)
                except Exception:
                    self.p.kill()
        except Exception:
            pass


def _sector_size(path):
    real = os.path.realpath(path)
    k = os.path.basename(real)
    v = read_file(f"/sys/class/block/{k}/queue/logical_block_size").strip()
    if not v:
        v = read_file(f"/sys/class/block/{k}/../queue/logical_block_size").strip()
    try:
        return int(v)
    except ValueError:
        return 512


# ---------------------------------------------------------------- filesystem signatures


def _ext_sb(sb):
    if len(sb) < 0x170 or sb[0x38:0x3A] != b"\x53\xef":
        return None
    logbs = u32(sb, 0x18)
    if logbs > 6:
        return None
    bs = 1024 << logbs
    bpg, ipg, inodes, blocks = u32(sb, 0x20), u32(sb, 0x28), u32(sb, 0), u32(sb, 4)
    inc, compat, roc = u32(sb, 0x60), u32(sb, 0x5C), u32(sb, 0x64)
    if inc & 0x80:
        blocks |= u32(sb, 0x150) << 32
    if not (bpg and bpg <= 8 * bs and ipg and inodes and blocks):
        return None
    if u32(sb, 0x4C) > 1:
        return None
    fdb = u32(sb, 0x14)
    kind = "ext4" if inc & (0x40 | 0x80 | 0x200) else "ext3" if compat & 0x4 else "ext2"
    groups = -(-(blocks - fdb) // bpg)
    return {"fs": kind, "bs": bs, "bpg": bpg, "blocks": blocks, "fdb": fdb, "gnr": u16(sb, 0x5A),
            "sparse": bool(roc & 1), "sparse2": bool(compat & 0x200), "label": _txt(sb[0x78:0x88]),
            "uuid": sb[0x68:0x78].hex(), "state": u16(sb, 0x3A), "mtime": u32(sb, 0x2C), "wtime": u32(sb, 0x30),
            "mnt_count": u16(sb, 0x34), "groups": groups, "size": blocks * bs,
            "backup_bgs": [u32(sb, 0x24C), u32(sb, 0x250)] if compat & 0x200 else None}


def _fat_bpb(b):
    if len(b) < 512 or b[510:512] != b"\x55\xaa":
        return None
    bps, spc, rsv, nfat = u16(b, 0x0B), b[0x0D], u16(b, 0x0E), b[0x10]
    if bps not in (512, 1024, 2048, 4096) or not spc or spc & (spc - 1) or not rsv or nfat not in (1, 2):
        return None
    rootent, tot16, fatsz16, tot32 = u16(b, 0x11), u16(b, 0x13), u16(b, 0x16), u32(b, 0x20)
    fat32 = fatsz16 == 0
    fatsz = u32(b, 0x24) if fat32 else fatsz16
    tot = tot16 or tot32
    if not fatsz or not tot:
        return None
    rds = (rootent * 32 + bps - 1) // bps
    data_sec = tot - (rsv + nfat * fatsz + rds)
    if data_sec <= 0:
        return None
    clusters = data_sec // spc
    ftype = "fat12" if clusters < 4085 else "fat16" if clusters < 65525 else "fat32"
    if fat32 and ftype != "fat32":
        ftype = "fat32"
    lab = b[0x47:0x52] if fat32 else b[0x2B:0x36]
    vid = u32(b, 0x43 if fat32 else 0x27)
    return {"fs": "vfat", "ftype": ftype, "bps": bps, "spc": spc, "rsv": rsv, "nfat": nfat, "fatsz": fatsz,
            "rootent": rootent, "tot": tot, "rds": rds, "clusters": clusters, "rootclus": u32(b, 0x2C) if fat32 else 0,
            "bk": u16(b, 0x32) if fat32 else 0, "media": b[0x15], "label": _txt(lab), "serial": f"{vid:08X}",
            "size": tot * bps}


def _ntfs_bpb(b):
    if len(b) < 512 or b[3:11] != b"NTFS    " or b[510:512] != b"\x55\xaa":
        return None
    bps, spc, tot = u16(b, 0x0B), b[0x0D], u64(b, 0x28)
    if spc > 0x80:
        spc = 1 << (256 - spc)
    if bps not in (512, 1024, 2048, 4096) or not spc or spc & (spc - 1) or not tot:
        return None
    cs = bps * spc
    c = struct.unpack_from("<b", b, 0x40)[0]
    rs = c * cs if c > 0 else 1 << (-c)
    return {"fs": "ntfs", "bps": bps, "spc": spc, "cs": cs, "tot": tot, "mft": u64(b, 0x30), "mirr": u64(b, 0x38),
            "rs": rs, "serial": f"{u64(b, 0x48):016X}", "size": (tot + 1) * bps, "label": ""}


def _exfat_bpb(b):
    if len(b) < 512 or b[3:11] != b"EXFAT   " or b[510:512] != b"\x55\xaa":
        return None
    shift, cshift = b[0x6C], b[0x6D]
    if not 9 <= shift <= 12 or cshift > 25:
        return None
    return {"fs": "exfat", "bps": 1 << shift, "spc": 1 << cshift, "size": u64(b, 0x48) << shift,
            "serial": f"{u32(b, 0x64):08X}", "fat_off": u32(b, 0x50), "heap": u32(b, 0x58),
            "clusters": u32(b, 0x5C), "root": u32(b, 0x60), "label": ""}


def _probe_buf(b):
    """Identify a filesystem whose first byte is b[0]."""
    L = len(b)
    if L < 512:
        return None
    r = _ntfs_bpb(b) or _exfat_bpb(b)
    if r:
        return r
    if b[0x52:0x5A] == b"FAT32   " or b[0x36:0x3E] in (b"FAT12   ", b"FAT16   ", b"FAT     "):
        r = _fat_bpb(b)
        if r:
            return r
    if L >= 2048:
        e = _ext_sb(b[1024:2048])
        if e and e["gnr"] == 0:
            return e
    if b[0:4] == b"XFSB":
        bs, dblocks = be32(b, 4), be64(b, 8)
        if 512 <= bs <= 65536 and bs & (bs - 1) == 0 and dblocks:
            return {"fs": "xfs", "size": bs * dblocks, "label": _txt(b[0x6C:0x78]), "uuid": b[0x20:0x30].hex(),
                    "bs": bs, "agblocks": be32(b, 0x54)}
    if L >= 0x10200 and b[0x10040:0x10048] == b"_BHRfS_M":
        return {"fs": "btrfs", "size": u64(b, 0x10070), "label": _txt(b[0x1012B:0x1022B]),
                "uuid": b[0x10020:0x10030].hex()}
    if b[0:6] == b"LUKS\xba\xbe":
        return {"fs": "crypto_LUKS", "size": None, "label": "", "uuid": _txt(b[0xA8:0xD0]), "version": be16(b, 6)}
    if L >= 4096 and b[4086:4096] == b"SWAPSPACE2":
        last = u32(b, 1028)
        return {"fs": "swap", "size": (last + 1) * 4096 if last else None, "label": _txt(b[1052:1068]),
                "uuid": b[1036:1052].hex()}
    if b[512:520] == b"LABELONE" and b[536:544] == b"LVM2 001":
        return {"fs": "LVM2_member", "size": None, "label": "", "uuid": _txt(b[544:576])}
    if L >= 0x8800 and b[0x8001:0x8006] == b"CD001":
        return {"fs": "iso9660", "size": u32(b, 0x8050) * u16(b, 0x8080), "label": _txt(b[0x8028:0x8048])}
    return None


DEEP_PATS = [(b"NTFS    ", 3, 4096), (b"EXFAT   ", 3, 4096), (b"FAT32   ", 0x52, 4096), (b"FAT16   ", 0x36, 4096),
             (b"FAT12   ", 0x36, 4096), (b"XFSB", 0, 4096), (b"_BHRfS_M", 0x10040, 0x10200),
             (b"LUKS\xba\xbe", 0, 4096), (b"SWAPSPACE2", 4086, 4096), (b"LABELONE", 512, 4096),
             (b"\x01CD001", 0x8000, 0x8800)]


def _disk_table(path):
    """Current partitions on a disk (from lsblk) as [{start,size,name,fstype}]."""
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
    except OSError:
        return []
    if not statmod.S_ISBLK(st.st_mode):
        if which("sfdisk"):
            rc, out, _ = run(["sfdisk", "-J", real], timeout=20)
            try:
                pt = json.loads(out)["partitiontable"]
                ss = pt.get("sectorsize", 512)
                return [{"start": p["start"] * ss, "size": p["size"] * ss, "name": p["node"], "fstype": None}
                        for p in pt.get("partitions", [])]
            except Exception:
                return []
        return []
    try:
        node, _ = _find_node(os.path.basename(real))
    except ApiError:
        return []
    return [{"start": c["start"], "size": c["size"], "name": c["path"], "fstype": c.get("fstype")}
            for c in node.get("children", []) if c.get("type") == "part" and c.get("start") is not None]


def _psearch_job(job, path, mode, start, end):
    dev = RawDev(path)
    try:
        size = dev.size
        end = min(end or size, size)
        start = max(0, start or 0) // 512 * 512
        found = {}
        t0 = time.time()
        job["live"] = {"phase": mode, "pos": start, "total": end - start, "found": []}

        def add(off, info, how):
            if not info:
                return None
            key = (off, info["fs"])
            if key in found:
                return found[key]
            c = dict(info, start=off, how=how)
            found[key] = c
            job["live"]["found"] = _summ(found.values())
            return c

        def prog(pos, label):
            el = max(time.time() - t0, 1e-3)
            done = max(pos - start, 0)
            rate = done / el
            job["progress"] = min(0.99, done / max(end - start, 1))
            job["live"].update(pos=pos, rate=rate, eta=(end - pos) / rate if rate > 0 else None)
            job["message"] = f"{label} {done / 1e9:.2f} / {(end - start) / 1e9:.2f} GB · {len(found)} found"

        if mode == "quick":
            for p in _disk_table(path):
                if start <= p["start"] < end:
                    add(p["start"], _probe_buf(dev.read(p["start"], 0x10200)), "partition-table entry")
            # 1 MiB grid, jumping over filesystems we recognise
            off = (start + MiB - 1) // MiB * MiB
            if start == 0:
                add(0, _probe_buf(dev.read(0, 0x10200)), "start of device")
            while off < end and not job["cancel"]:
                cnt = int(min(128, (end - off) // MiB + 1))
                blob = dev.strided(off, cnt, MiB, 4096)
                jumped = False
                for i in range(cnt):
                    o = off + i * MiB
                    if o >= end:
                        break
                    info = _probe_buf(blob[i * 4096:(i + 1) * 4096])
                    if info is None and blob[i * 4096:(i + 1) * 4096].strip(b"\0") == b"":
                        continue
                    if info is None:
                        continue
                    c = add(o, info, "1 MiB alignment")
                    if c and c.get("size"):
                        nxt = (o + c["size"] + MiB - 1) // MiB * MiB
                        if nxt > o:
                            off = nxt
                            jumped = True
                            break
                if not jumped:
                    off += cnt * MiB
                prog(min(off, end), "Quick search")
            # legacy CHS cylinder boundaries (255 heads x 63 sectors)
            cyl = 16065 * 512
            for base in (32256, 0):
                o = base
                while o < end and not job["cancel"]:
                    cnt = int(min(256, (end - o) // cyl + 1))
                    blob = dev.strided(o, cnt, cyl, 4096)
                    for i in range(cnt):
                        oo = o + i * cyl
                        if start <= oo < end and oo % MiB:
                            add(oo, _probe_buf(blob[i * 4096:(i + 1) * 4096]), "cylinder boundary")
                    o += cnt * cyl
        else:
            CH = 16 * MiB
            OV = 0x10200
            pos = start
            while pos < end and not job["cancel"]:
                buf = dev.read(pos, min(CH + OV, end - pos + OV))
                if not buf:
                    break
                lim = min(CH, len(buf))

                def at(s, need):
                    if s >= pos and s + need <= pos + len(buf):
                        return buf[s - pos:s - pos + need]
                    return dev.read(s, need)
                for pat, rel, need in DEEP_PATS:
                    i = buf.find(pat)
                    while 0 <= i:
                        s = pos + i - rel
                        if i - rel < lim and s >= start and s % 512 == 0:
                            add(s, _probe_buf(at(s, need)), "deep scan")
                        i = buf.find(pat, i + 1)
                # ext: magic 0xEF53 at superblock+0x38 (primary or backup superblocks)
                i = buf.find(b"\x53\xef")
                while 0 <= i:
                    sbp = pos + i - 0x38
                    if i - 0x38 < lim and sbp >= 0 and sbp % 512 == 0:
                        e = _ext_sb(at(sbp, 1024))
                        if e:
                            if e["gnr"] == 0:
                                fs0 = sbp - 1024
                                how = "deep scan"
                            else:
                                fs0 = sbp - (e["gnr"] * e["bpg"] + e["fdb"]) * e["bs"]
                                how = f"backup superblock (group {e['gnr']})"
                            if fs0 >= 0 and not any(k[0] == fs0 and k[1].startswith("ext") for k in found):
                                add(fs0, dict(e, gnr=0), how)
                    i = buf.find(b"\x53\xef", i + 1)
                pos += lim
                prog(pos, "Deep search")
        if job["cancel"]:
            return None
        cands = _classify(dev, list(found.values()), _disk_table(path), size)
        job["message"] = f"Search complete — {len([c for c in cands if not c.get('backup')])} filesystem(s)"
        return {"candidates": cands, "size": size, "table": _disk_table(path), "mode": mode,
                "sector": _sector_size(path) if dev.kind == "block" else 512}
    finally:
        dev.close()


def _summ(cands):
    return [{"start": c["start"], "fs": c["fs"], "size": c.get("size"), "label": c.get("label", "")}
            for c in sorted(cands, key=lambda c: c["start"])][-200:]


def _classify(dev, cands, table, disksize):
    cands.sort(key=lambda c: c["start"])
    out = []
    for c in cands:
        fs = c["fs"]
        # mark backups of an earlier primary
        for p in out:
            if p["fs"] == "ntfs" == fs and p.get("serial") == c.get("serial") and \
                    c["start"] == p["start"] + p["tot"] * p["bps"]:
                c["backup"] = f"backup boot sector of the NTFS at sector {p['start'] // 512:,}"
            if p["fs"] == "vfat" == fs and p.get("serial") == c.get("serial") and p.get("bk") and \
                    c["start"] == p["start"] + p["bk"] * p["bps"]:
                c["backup"] = f"backup boot sector of the FAT32 at sector {p['start'] // 512:,}"
            if p["fs"] == "exfat" == fs and p.get("serial") == c.get("serial") and \
                    c["start"] == p["start"] + 12 * p["bps"]:
                c["backup"] = f"backup boot region of the exFAT at sector {p['start'] // 512:,}"
            if p["fs"] in ("xfs", "btrfs") and p["fs"] == fs and p.get("uuid") == c.get("uuid"):
                c["backup"] = f"secondary superblock of the {fs} at sector {p['start'] // 512:,}"
            if p["fs"].startswith("ext") and fs.startswith("ext") and p.get("uuid") == c.get("uuid") and not p.get("backup"):
                c["backup"] = f"superblock copy (journal/backup) of the {p['fs']} at sector {p['start'] // 512:,}"
        if not c.get("backup") and fs == "ntfs" and not any(
                p["fs"] == "ntfs" and p.get("serial") == c.get("serial") for p in out):
            # lone NTFS boot sector: may be the backup at the end of a volume whose primary is damaged
            pstart = c["start"] - c["tot"] * c["bps"]
            if pstart >= 0 and pstart % 512 == 0 and not _ntfs_bpb(dev.read(pstart, 512)):
                prim = dev.read(pstart, 512)
                if prim and (pstart % MiB == 0 or pstart % (16065 * 512) in (0, 32256) or pstart == 63 * 512):
                    r = dict(c, start=pstart, how="reconstructed from backup boot sector", repair="ntfs_boot_from_backup")
                    out.append(r)
                    c["backup"] = f"backup boot sector — primary at sector {pstart // 512:,} is damaged"
        if not c.get("backup") and fs == "vfat" and c.get("ftype") == "fat32":
            pstart = c["start"] - 6 * c["bps"]
            if pstart >= 0 and not any(p["fs"] == "vfat" and p["start"] == pstart for p in out):
                b0 = dev.read(pstart, 512)
                if b0[510:512] != b"\x55\xaa" and (pstart % MiB == 0 or pstart == 63 * 512 or pstart % (16065 * 512) in (0, 32256)):
                    out.append(dict(c, start=pstart, how="reconstructed from backup boot sector", repair="fat_boot_from_backup"))
                    c["backup"] = f"backup boot sector — primary at sector {pstart // 512:,} is damaged"
        out.append(c)
    # relation to the current partition table
    for c in out:
        if c.get("backup"):
            c["status"] = "backup"
            continue
        same = [p for p in table if p["start"] == c["start"]]
        if same:
            c["status"] = "in-table"
            c["part"] = same[0]["name"]
            continue
        end = c["start"] + (c.get("size") or 512)
        ov = [p for p in table if c["start"] < p["start"] + p["size"] and p["start"] < end]
        c["status"] = "overlap" if ov else "lost"
        if ov:
            c["overlaps"] = ", ".join(p["name"] for p in ov)
    prim = [c for c in out if not c.get("backup")]
    for i, c in enumerate(prim):
        nxt = min([p["start"] for p in prim if p["start"] > c["start"]] + [disksize])
        c["max_size"] = nxt - c["start"]
        if not c.get("size") or c["size"] > disksize - c["start"]:
            c["size_guess"] = True
            c["size"] = c["max_size"] if not c.get("size") else min(c["size"], disksize - c["start"])
    return out


# ---------------------------------------------------------------- write recovered partitions

GPT_TYPES = {"ntfs": "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7", "exfat": "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7",
             "vfat": "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7", "swap": "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F",
             "LVM2_member": "E6D6D379-F507-44C2-A23C-238F2A3DF928"}
DOS_TYPES = {"ntfs": "7", "exfat": "7", "swap": "82", "LVM2_member": "8e"}


def api_rec_write_table(q, b):
    path = os.path.realpath(os.path.expanduser(str(b.get("path", ""))))
    mode = b.get("mode", "append")
    label = b.get("label", "gpt")
    items = b.get("items") or []
    if mode not in ("append", "new") or label not in ("gpt", "dos"):
        raise ApiError("Invalid options")
    if not items:
        raise ApiError("Select at least one partition to write.")
    is_blk = statmod.S_ISBLK(os.stat(path).st_mode)
    if is_blk:
        node, _ = _find_node(os.path.basename(path))
        if node["type"] != "disk":
            raise ApiError("Partition tables are written to a whole disk, not a partition.")
        if _all_mounts(node):
            raise ApiError("Unmount every filesystem on this disk first.")
        if set(_all_mounts(node)) & CRITICAL_MOUNTS:
            raise ApiError("Refused: this is the running system disk.")
        disksize = node["size"]
    else:
        disksize = os.path.getsize(path)
    if b.get("confirm") != path:
        raise ApiError(f"Confirmation text must exactly match {path}.")
    ss = _sector_size(path) if is_blk else 512
    rows = sorted(((int(i["start"]), int(i["size"]), str(i.get("fs", ""))) for i in items), key=lambda x: x[0])
    existing = [] if mode == "new" else _disk_table(path)
    prev_end = 0
    for s, n, fs in rows:
        if s % ss or n <= 0 or s + n > disksize:
            raise ApiError(f"Partition at {s} is not sector-aligned or exceeds the disk.")
        if s < (34 * ss if label == "gpt" else ss):
            raise ApiError("A partition cannot start inside the partition-table area.")
        if s < prev_end:
            raise ApiError("Selected partitions overlap each other.")
        prev_end = s + n
        for p in existing:
            if s < p["start"] + p["size"] and p["start"] < s + n:
                raise ApiError(f"Partition at {s} overlaps existing {p['name']}.")
    if label == "gpt" and rows[-1][0] + rows[-1][1] > disksize - 33 * ss:
        s, n, fs = rows[-1]
        rows[-1] = (s, (disksize - 33 * ss - s) // ss * ss, fs)
    script = (f"label: {label}\n" if mode == "new" else "")
    for s, n, fs in rows:
        t = (GPT_TYPES if label == "gpt" else DOS_TYPES).get(fs, "0FC63DAF-8483-4772-8E79-3D69D8477DE4" if label == "gpt" else "83")
        if label == "dos" and fs == "vfat":
            t = "c"
        script += f"start={s // ss}, size={n // ss}, type={t}\n"
    ensure_data_dir()
    rdir = os.path.join(DATA_DIR, "recovery")
    os.makedirs(rdir, exist_ok=True)
    stampv = time.strftime("%Y%m%d-%H%M%S")
    bk = os.path.join(rdir, f"ptable-before-{os.path.basename(path)}-{stampv}.sfdisk")
    sc = os.path.join(rdir, f"ptable-write-{stampv}.txt")
    with open(sc, "w") as f:
        f.write(script)
    user = f"{os.getuid()}:{os.getgid()}"
    sh = ('set -e; sfdisk --dump "$1" > "$2" 2>/dev/null || echo "(no previous partition table)"; chown "$4" "$2"; '
          f'sfdisk {"--append " if mode == "append" else ""}--wipe never --wipe-partitions never "$1" < "$3"; '
          '(blockdev --rereadpt "$1" 2>/dev/null || partprobe "$1" 2>/dev/null || true); echo "==> Partition table written"')

    def job_fn(job):
        r = stream_job(job, ["sh", "-c", sh, "mysyspilot", path, bk, sc, user], use_priv=is_blk)
        if os.path.exists(bk) and os.path.getsize(bk):
            _bk_add({"ts": time.time(), "type": "ptable", "dev": path, "label": "Table before recovery write",
                     "file": bk, "size": os.path.getsize(bk), "compression": "none", "sha256": None,
                     "source_bytes": os.path.getsize(bk)})
        r["backup"] = bk
        return r
    return {"job": job_start("rec-write", job_fn)}


# ---------------------------------------------------------------- mount found partition read-only

REC_MOUNTS = {}


def api_rec_mount(q, b):
    path = os.path.realpath(os.path.expanduser(str(b.get("path", ""))))
    off, size = int(b.get("offset", 0)), int(b.get("size", 0))
    if off < 0 or size <= 0:
        raise ApiError("Invalid offset/size")
    rc, out, err = priv(["losetup", "-f", "--show", "-r", "-o", str(off), "--sizelimit", str(size), path], timeout=60)
    if rc != 0:
        raise ApiError((err or out).strip() or "losetup failed")
    loop = out.strip()
    mp = None
    if which("udisksctl"):
        rc, o2, e2 = run(["udisksctl", "mount", "-b", loop, "-o", "ro"], timeout=120)
        m = re.search(r" at (.+?)\.?$", o2.strip())
        if rc == 0 and m:
            mp = m.group(1)
    if not mp:
        mp = f"/mnt/mysyspilot-recovery/{os.path.basename(loop)}"
        rc, o3, e3 = priv(["sh", "-c", 'mkdir -p "$2" && mount -o ro "$1" "$2"', "mysyspilot", loop, mp], timeout=120)
        if rc != 0:
            priv(["losetup", "-d", loop], timeout=30)
            raise ApiError("Could not mount the recovered filesystem read-only: " + (e3 or o3).strip()[-400:])
    REC_MOUNTS[loop] = {"loop": loop, "mountpoint": mp, "path": path, "offset": off, "size": size}
    return {"ok": True, "loop": loop, "mountpoint": mp}


def api_rec_unmount(q, b):
    loop = str(b.get("loop", ""))
    m = REC_MOUNTS.get(loop)
    if not m:
        raise ApiError("Unknown recovery mount")
    rc, o, e = run(["udisksctl", "unmount", "-b", loop], timeout=60) if which("udisksctl") else (1, "", "")
    if rc != 0:
        priv(["umount", m["mountpoint"]], timeout=60)
    priv(["losetup", "-d", loop], timeout=30)
    REC_MOUNTS.pop(loop, None)
    return {"ok": True}


def api_rec_mounts(q, b):
    return {"mounts": list(REC_MOUNTS.values())}


# ---------------------------------------------------------------- boot sector / superblock analysis & repair


def _exfat_cksum(region, bps):
    c = 0
    for i in range(11 * bps):
        if i in (106, 107, 112):
            continue
        c = (((c << 31) | (c >> 1)) + region[i]) & 0xFFFFFFFF
    return c


def _exfat_region_ok(region, bps):
    if len(region) < 12 * bps or not _exfat_bpb(region[:512]):
        return False
    c = _exfat_cksum(region, bps)
    s11 = region[11 * bps:12 * bps]
    return s11 == struct.pack("<I", c) * (bps // 4)


def _ntfs_fixup(rec, stride=512):
    if rec[0:4] != b"FILE":
        return None
    usa, cnt = u16(rec, 4), u16(rec, 6)
    if usa + cnt * 2 > len(rec) or cnt < 1:
        return None
    r = bytearray(rec)
    usn = rec[usa:usa + 2]
    for k in range(1, cnt):
        p = k * stride - 2
        if p + 2 > len(r):
            break
        if r[p:p + 2] != usn:
            return None
        r[p:p + 2] = rec[usa + 2 * k:usa + 2 * k + 2]
    return bytes(r)


def _analyze_job(job, path, off, size_hint):
    dev = RawDev(path)
    try:
        job["message"] = "Reading boot area…"
        head = dev.read(off, 0x10200)
        rep = {"path": path, "offset": off, "checks": [], "actions": [], "fs": None}
        chk = lambda name, ok, detail: rep["checks"].append({"name": name, "status": ok, "detail": detail})  # noqa
        act = lambda a, label, danger=True, **kw: rep["actions"].append(dict(action=a, label=label, danger=danger, **kw))  # noqa
        info = _probe_buf(head)
        # ---- NTFS
        nt = _ntfs_bpb(head)
        bk = None
        if not nt and size_hint:
            bk = dev.read(off + size_hint - 512, 512)
            if _ntfs_bpb(bk):
                nt = _ntfs_bpb(bk)
                rep["fs"] = "ntfs"
                chk("Primary boot sector", "bad", "Not a valid NTFS boot sector")
                chk("Backup boot sector", "ok", f"Valid backup found in the last sector ({nt['tot']} sectors)")
                act("ntfs_boot_from_backup", "Restore boot sector from backup")
        if nt and rep["fs"] is None:
            rep["fs"] = "ntfs"
            chk("Primary boot sector", "ok", f"{nt['bps']} B/sector · {nt['cs']} B/cluster · {nt['tot']:,} sectors · serial {nt['serial']}")
            bk = dev.read(off + nt["tot"] * nt["bps"], 512)
            bnt = _ntfs_bpb(bk)
            if not bnt:
                chk("Backup boot sector", "warn", "Missing or invalid at the end of the volume")
                act("ntfs_backup_from_boot", "Rewrite backup boot sector from primary", danger=False)
            elif bk != head[:512]:
                chk("Backup boot sector", "warn", "Valid but differs from the primary")
                act("ntfs_boot_from_backup", "Restore primary from backup")
                act("ntfs_backup_from_boot", "Update backup from primary", danger=False)
            else:
                chk("Backup boot sector", "ok", "Identical to primary")
        if rep["fs"] == "ntfs":
            mft_off = off + nt["mft"] * nt["cs"]
            mir_off = off + nt["mirr"] * nt["cs"]
            n = 4 * nt["rs"]
            job["message"] = "Comparing $MFT with $MFTMirr…"
            a, m = dev.read(mft_off, n), dev.read(mir_off, n)
            okA = all(a[i * nt["rs"]:i * nt["rs"] + 4] == b"FILE" for i in range(4))
            okM = all(m[i * nt["rs"]:i * nt["rs"] + 4] == b"FILE" for i in range(4))
            if a == m and okA:
                chk("$MFT vs $MFTMirr", "ok", f"First 4 MFT records match the mirror (MFT at cluster {nt['mft']}, mirror at {nt['mirr']})")
            else:
                chk("$MFT vs $MFTMirr", "bad" if not okA else "warn",
                    f"MFT records {'valid' if okA else 'DAMAGED'} · mirror {'valid' if okM else 'DAMAGED'} · {'identical' if a == m else 'different'}")
                if okM:
                    act("ntfs_mft_from_mirror", "Repair $MFT from $MFTMirr")
                if okA:
                    act("ntfs_mirror_from_mft", "Update $MFTMirr from $MFT", danger=False)
            rep["info"] = {k: nt[k] for k in ("bps", "cs", "tot", "mft", "mirr", "rs", "serial", "size")}
            return rep
        # ---- exFAT
        ex = _exfat_bpb(head)
        if not ex:
            b2 = dev.read(off + 12 * 512, 512)
            if _exfat_bpb(b2):
                ex = _exfat_bpb(b2)
                rep["fs"] = "exfat"
                chk("Main boot region", "bad", "Not a valid exFAT boot sector")
                act("exfat_boot_from_backup", "Restore main boot region from backup")
        if ex:
            rep["fs"] = "exfat"
            bps = ex["bps"]
            main_r, back_r = dev.read(off, 12 * bps), dev.read(off + 12 * bps, 12 * bps)
            mok, bok = _exfat_region_ok(main_r, bps), _exfat_region_ok(back_r, bps)
            if not rep["checks"]:
                chk("Main boot region", "ok" if mok else "bad", "Checksum valid" if mok else "Checksum mismatch")
            chk("Backup boot region", "ok" if bok else "bad", "Checksum valid" if bok else "Checksum mismatch or missing")
            if mok and bok and main_r != back_r:
                chk("Main vs backup", "warn", "Both valid but different")
            if bok and (not mok or main_r != back_r) and not any(a["action"] == "exfat_boot_from_backup" for a in rep["actions"]):
                act("exfat_boot_from_backup", "Restore main boot region from backup")
            if mok and (not bok or main_r != back_r):
                act("exfat_backup_from_boot", "Update backup boot region from main", danger=False)
            rep["info"] = {"bytes_per_sector": bps, "cluster": bps * ex["spc"], "size": ex["size"], "serial": ex["serial"]}
            return rep
        # ---- FAT
        fb = _fat_bpb(head)
        if not fb:
            b6 = dev.read(off + 6 * 512, 512)
            if _fat_bpb(b6):
                fb = _fat_bpb(b6)
                rep["fs"] = "vfat"
                chk("Boot sector", "bad", "Primary boot sector is invalid")
                chk("Backup boot sector (sector 6)", "ok", "Valid FAT32 backup found")
                act("fat_boot_from_backup", "Restore boot sector from backup")
        if fb:
            rep["fs"] = "vfat"
            bps = fb["bps"]
            if not rep["checks"]:
                chk("Boot sector", "ok", f"{fb['ftype'].upper()} · {bps} B/sector · {bps * fb['spc']} B/cluster · {fb['clusters']:,} clusters · label '{fb['label']}'")
                if fb["ftype"] == "fat32" and fb["bk"]:
                    bb = dev.read(off + fb["bk"] * bps, bps)
                    if not _fat_bpb(bb):
                        chk("Backup boot sector", "warn", f"Sector {fb['bk']} does not hold a valid backup")
                        act("fat_backup_from_boot", "Rewrite backup boot sector from primary", danger=False)
                    elif bb != head[:bps]:
                        chk("Backup boot sector", "warn", "Valid but differs from the primary")
                        act("fat_boot_from_backup", "Restore primary from backup")
                        act("fat_backup_from_boot", "Update backup from primary", danger=False)
                    else:
                        chk("Backup boot sector", "ok", "Identical to primary")
            if fb["nfat"] == 2:
                fbytes = fb["fatsz"] * bps
                f1 = off + fb["rsv"] * bps
                f2 = f1 + fbytes
                diff = 0
                pos = 0
                while pos < fbytes and not job["cancel"]:
                    k = min(4 * MiB, fbytes - pos)
                    a, c = dev.read(f1 + pos, k), dev.read(f2 + pos, k)
                    if a != c:
                        for s in range(0, k, bps):
                            if a[s:s + bps] != c[s:s + bps]:
                                diff += 1
                    pos += k
                    job["progress"] = pos / fbytes
                    job["message"] = f"Comparing FAT copies… {100 * pos / fbytes:.0f}%"
                m1 = dev.read(f1, 1)[:1] == bytes([fb["media"]])
                m2 = dev.read(f2, 1)[:1] == bytes([fb["media"]])
                if diff == 0:
                    chk("FAT1 vs FAT2", "ok", f"Both {fbytes:,}-byte FAT copies are identical")
                else:
                    chk("FAT1 vs FAT2", "warn", f"{diff:,} sector(s) differ · FAT1 media byte {'OK' if m1 else 'BAD'} · FAT2 {'OK' if m2 else 'BAD'}")
                    act("fat_copy_1to2", "Copy FAT1 → FAT2", recommended=m1)
                    act("fat_copy_2to1", "Copy FAT2 → FAT1", recommended=(m2 and not m1))
            rep["info"] = {k: fb[k] for k in ("ftype", "bps", "spc", "rsv", "nfat", "fatsz", "clusters", "label", "serial", "size")}
            return rep
        # ---- ext
        e = _ext_sb(head[1024:2048])
        rep["fs"] = e["fs"] if e else None
        if e:
            st = e["state"]
            chk("Primary superblock", "ok" if st & 1 and not st & 2 else "warn",
                f"{e['fs']} · {e['bs']} B blocks · {e['blocks']:,} blocks · {e['groups']} groups · label '{e['label']}' · state {'clean' if st & 1 else 'not clean'}{' · ERRORS recorded' if st & 2 else ''}")
            groups = [g for g in range(1, e["groups"]) if not e["sparse"] or _sparse_group(g)][:24]
            if e["sparse2"] and e["backup_bgs"]:
                groups = [g for g in e["backup_bgs"] if g]
            good = []
            for g in groups:
                blk = g * e["bpg"] + e["fdb"]
                b2 = _ext_sb(dev.read(off + blk * e["bs"], 1024))
                if b2 and b2["uuid"] == e["uuid"]:
                    good.append(blk)
            chk("Backup superblocks", "ok" if good else "warn",
                f"{len(good)} of {len(groups)} checked are valid: " + ", ".join(map(str, good[:12])))
            rep["backups"] = [{"block": blk, "bs": e["bs"]} for blk in good]
            if good:
                act("ext_fsck_backup", f"Repair with backup superblock {good[0]} (e2fsck -b)", block=good[0], bs=e["bs"])
            act("ext_fsck", "Check & repair with e2fsck -f -y")
            rep["info"] = {k: e[k] for k in ("fs", "bs", "blocks", "groups", "label", "uuid", "size")}
            return rep
        # primary superblock damaged: look for backups at likely positions
        job["message"] = "Primary superblock invalid — searching for backup superblocks…"
        hits = []
        for bs in (4096, 1024, 2048):
            bpg = 8 * bs
            fdb = 1 if bs == 1024 else 0
            for g in (1, 3, 5, 7, 9, 25, 27, 49, 81, 125):
                blk = g * bpg + fdb
                b2 = _ext_sb(dev.read(off + blk * bs, 1024))
                if b2 and b2["gnr"] == g:
                    hits.append({"block": blk, "bs": bs, "fs": b2["fs"], "label": b2["label"]})
            if hits:
                break
        if hits:
            rep["fs"] = hits[0]["fs"]
            chk("Primary superblock", "bad", "Invalid or overwritten")
            chk("Backup superblocks", "ok", "Found at blocks " + ", ".join(str(h["block"]) for h in hits) + f" (block size {hits[0]['bs']})")
            rep["backups"] = hits
            act("ext_fsck_backup", f"Repair with backup superblock {hits[0]['block']} (e2fsck -b)", block=hits[0]["block"], bs=hits[0]["bs"])
            return rep
        if info:
            rep["fs"] = info["fs"]
            chk("Filesystem", "ok", f"{info['fs']} detected · {fmt_bytes(info.get('size'))} · label '{info.get('label', '')}'")
            if info["fs"] == "xfs":
                act("xfs_check", "Check with xfs_repair -n", danger=False)
                act("xfs_repair", "Repair with xfs_repair")
            if info["fs"] == "btrfs":
                act("btrfs_check", "Check with btrfs check --readonly", danger=False)
                act("btrfs_super_recover", "Recover superblock with btrfs rescue super-recover")
            return rep
        chk("Filesystem", "bad", "No known filesystem signature (NTFS, FAT, exFAT, ext2/3/4, XFS, Btrfs) found here.")
        return rep
    finally:
        dev.close()


def fmt_bytes(n):
    if not n:
        return "?"
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PiB"


def _sparse_group(g):
    if g in (0, 1):
        return True
    for p in (3, 5, 7):
        x = p
        while x < g:
            x *= p
        if x == g:
            return True
    return False


def api_rec_analyze(q, b):
    path = str(b.get("path", ""))
    return {"job": job_start("rec-analyze", _analyze_job, path, int(b.get("offset", 0)), int(b.get("size", 0) or 0))}


UNDO_DIR = lambda: os.path.join(DATA_DIR, "recovery", "undo")  # noqa: E731


def _undo_index():
    try:
        return json.loads(read_file(os.path.join(UNDO_DIR(), "index.json"), "[]") or "[]")
    except Exception:
        return []


def _rec_write(dev, off, data, desc, path):
    old = dev.read(off, len(data))
    os.makedirs(UNDO_DIR(), exist_ok=True)
    uid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    with open(os.path.join(UNDO_DIR(), uid + ".bin"), "wb") as f:
        f.write(old)
    idx = _undo_index()
    idx.insert(0, {"id": uid, "ts": time.time(), "path": path, "offset": off, "length": len(data), "desc": desc})
    with open(os.path.join(UNDO_DIR(), "index.json"), "w") as f:
        json.dump(idx[:500], f, indent=1)
    dev.write(off, data)
    if dev.read(off, len(data)) != data:
        raise ApiError("Verification after write failed")
    return uid


def _repair_job(job, path, off, action, params):
    tool_actions = {"ext_fsck", "ext_fsck_backup", "xfs_check", "xfs_repair", "btrfs_check", "btrfs_super_recover"}
    if action in tool_actions:
        size = int(params.get("size") or 0)
        return _tool_on_region(job, path, off, size, action, params)
    dev = RawDev(path, write=True)
    try:
        done = []
        head = dev.read(off, 4096)
        if action.startswith("ntfs_"):
            nt = _ntfs_bpb(head)
            if not nt and params.get("size"):
                nt = _ntfs_bpb(dev.read(off + int(params["size"]) - 512, 512))
            if not nt:
                raise ApiError("No valid NTFS boot sector (primary or backup) found")
            bko = off + nt["tot"] * nt["bps"]
            if action == "ntfs_boot_from_backup":
                bk = dev.read(bko, 512)
                if not _ntfs_bpb(bk):
                    raise ApiError("Backup boot sector is not valid")
                done.append(_rec_write(dev, off, bk, "NTFS boot sector ← backup", path))
            elif action == "ntfs_backup_from_boot":
                if not _ntfs_bpb(head):
                    raise ApiError("Primary boot sector is not valid")
                done.append(_rec_write(dev, bko, head[:512], "NTFS backup boot sector ← primary", path))
            elif action in ("ntfs_mft_from_mirror", "ntfs_mirror_from_mft"):
                n = 4 * nt["rs"]
                mft, mir = off + nt["mft"] * nt["cs"], off + nt["mirr"] * nt["cs"]
                src, dst = (mir, mft) if action == "ntfs_mft_from_mirror" else (mft, mir)
                data = dev.read(src, n)
                if not all(data[i * nt["rs"]:i * nt["rs"] + 4] == b"FILE" for i in range(4)):
                    raise ApiError("Source records are not valid MFT records")
                done.append(_rec_write(dev, dst, data, "$MFT ← $MFTMirr" if dst == mft else "$MFTMirr ← $MFT", path))
        elif action.startswith("exfat_"):
            ex = _exfat_bpb(head) or _exfat_bpb(dev.read(off + 12 * 512, 512))
            if not ex:
                raise ApiError("No valid exFAT boot region found")
            bps = ex["bps"]
            main_r, back_r = dev.read(off, 12 * bps), dev.read(off + 12 * bps, 12 * bps)
            if action == "exfat_boot_from_backup":
                if not _exfat_region_ok(back_r, bps):
                    raise ApiError("Backup boot region checksum is invalid")
                done.append(_rec_write(dev, off, back_r, "exFAT main boot region ← backup", path))
            else:
                if not _exfat_region_ok(main_r, bps):
                    raise ApiError("Main boot region checksum is invalid")
                done.append(_rec_write(dev, off + 12 * bps, main_r, "exFAT backup boot region ← main", path))
        elif action.startswith("fat_"):
            fb = _fat_bpb(head)
            if action == "fat_boot_from_backup":
                bko = (fb["bk"] if fb and fb["bk"] else 6)
                bps = fb["bps"] if fb else 512
                bk = dev.read(off + bko * bps, 3 * bps)
                if not _fat_bpb(bk):
                    raise ApiError("Backup boot sector is not valid")
                done.append(_rec_write(dev, off, bk, "FAT32 boot sectors 0–2 ← backup", path))
            elif action == "fat_backup_from_boot":
                if not fb or fb["ftype"] != "fat32":
                    raise ApiError("Primary FAT32 boot sector is not valid")
                bps = fb["bps"]
                done.append(_rec_write(dev, off + (fb["bk"] or 6) * bps, dev.read(off, 3 * bps), "FAT32 backup boot ← primary", path))
            elif action in ("fat_copy_1to2", "fat_copy_2to1"):
                if not fb or fb["nfat"] != 2:
                    raise ApiError("Volume does not have two FAT copies")
                bps = fb["bps"]
                fbytes = fb["fatsz"] * bps
                f1 = off + fb["rsv"] * bps
                f2 = f1 + fbytes
                src, dst = (f1, f2) if action == "fat_copy_1to2" else (f2, f1)
                pos = 0
                while pos < fbytes:
                    k = min(4 * MiB, fbytes - pos)
                    a, c = dev.read(src + pos, k), dev.read(dst + pos, k)
                    if a != c:
                        done.append(_rec_write(dev, dst + pos, a, f"FAT copy {'1→2' if src == f1 else '2→1'} @{pos}", path))
                    pos += k
                    job["progress"] = pos / fbytes
                    job["message"] = f"Copying FAT… {100 * pos / fbytes:.0f}%"
        else:
            raise ApiError("Unknown repair action")
        job["message"] = "Repair written and verified"
        return {"written": len(done), "undo": done}
    finally:
        dev.close()


def _tool_on_region(job, path, off, size, action, params):
    real = os.path.realpath(path)
    cmds = {"ext_fsck": ["e2fsck", "-f", "-y"],
            "ext_fsck_backup": ["e2fsck", "-f", "-y", "-b", str(int(params.get("block", 0))), "-B", str(int(params.get("bs", 4096)))],
            "xfs_check": ["xfs_repair", "-n"], "xfs_repair": ["xfs_repair"],
            "btrfs_check": ["btrfs", "check", "--readonly"], "btrfs_super_recover": ["btrfs", "rescue", "super-recover", "-y"]}
    cmd = cmds[action]
    if not which(cmd[0]):
        raise ApiError(f"{cmd[0]} is not installed.")
    is_blk = statmod.S_ISBLK(os.stat(real).st_mode)
    if cmd[0] == "e2fsck":
        cmd = cmd + ["-C", "1"]
    hook = _progress_parser(job, cmd[0])
    if off == 0 and (is_blk or not size):
        return stream_job(job, cmd + [real], use_priv=is_blk, ok_codes=tuple(range(0, 8)), parse=hook)
    if not size:
        raise ApiError("Size of the region is required")
    script = ('L=$(losetup -f --show -o "$1" --sizelimit "$2" "$3") || exit 1; echo "==> using $L"; shift 3; '
              '"$@" "$L"; rc=$?; losetup -d "$L"; exit $rc')
    return stream_job(job, ["sh", "-c", script, "mysyspilot", str(off), str(size), real] + [which(cmd[0])] + cmd[1:],
                      use_priv=True, ok_codes=tuple(range(0, 8)), parse=hook)


def api_rec_repair(q, b):
    path = os.path.realpath(os.path.expanduser(str(b.get("path", ""))))
    action = str(b.get("action", ""))
    if b.get("confirm") != path and action not in ("xfs_check", "btrfs_check"):
        raise ApiError(f"Confirmation text must exactly match {path}.")
    if statmod.S_ISBLK(os.stat(path).st_mode):
        node, top = _find_node(os.path.basename(path))
        mounts = _all_mounts(node)
        if set(mounts) & CRITICAL_MOUNTS:
            raise ApiError("Refused: this holds the running system. Boot from a live USB to repair it.")
        if mounts:
            raise ApiError("Unmount the volume (and any partition on this disk) first: " + ", ".join(mounts))
    return {"job": job_start("rec-repair", _repair_job, path, int(b.get("offset", 0)), action, b.get("params") or {})}


def api_rec_undo(q, b):
    out = []
    for e in _undo_index():
        e = dict(e)
        e["exists"] = os.path.exists(os.path.join(UNDO_DIR(), e["id"] + ".bin"))
        out.append(e)
    return {"undo": out}


def _undo_job(job, uid):
    e = next((x for x in _undo_index() if x["id"] == uid), None)
    if not e:
        raise ApiError("Unknown undo entry")
    data = open(os.path.join(UNDO_DIR(), uid + ".bin"), "rb").read()
    dev = RawDev(e["path"], write=True)
    try:
        new = _rec_write(dev, e["offset"], data, f"UNDO of {uid}: {e['desc']}", e["path"])
    finally:
        dev.close()
    return {"ok": True, "undo": new}


def api_rec_undo_revert(q, b):
    uid = str(b.get("id", ""))
    e = next((x for x in _undo_index() if x["id"] == uid), None)
    if not e or b.get("confirm") != e["path"]:
        raise ApiError("Confirmation text must match the device path")
    return {"job": job_start("rec-undo", _undo_job, uid)}


# ---------------------------------------------------------------- undelete (NTFS, FAT12/16/32)

REC_UNDEL = {}


def _runs(b, off):
    runs, lcn = [], 0
    while off < len(b):
        h = b[off]
        if h == 0:
            break
        ls, os_ = h & 0xF, h >> 4
        off += 1
        if ls == 0 or ls > 8 or os_ > 8:
            break
        length = int.from_bytes(b[off:off + ls], "little")
        off += ls
        if os_:
            lcn += int.from_bytes(b[off:off + os_], "little", signed=True)
            off += os_
            runs.append((lcn, length))
        else:
            runs.append((None, length))
    return runs


def _ntfs_attrs(rec):
    """Parse an MFT record (fixed up). Returns dict with names, parent, data, times."""
    out = {"names": [], "data": None, "mtime": 0}
    p = u16(rec, 0x14)
    while p + 16 <= len(rec):
        t = u32(rec, p)
        if t == 0xFFFFFFFF or t == 0:
            break
        ln = u32(rec, p + 4)
        if ln < 16 or p + ln > len(rec):
            break
        nonres = rec[p + 8]
        nlen = rec[p + 9]
        if not nonres:
            clen, coff = u32(rec, p + 0x10), u16(rec, p + 0x14)
            content = rec[p + coff:p + coff + clen]
            if t == 0x10 and len(content) >= 0x20:
                out["mtime"] = u64(content, 0x08)
            elif t == 0x30 and len(content) >= 0x42:
                cn = content[0x40]
                name = content[0x42:0x42 + 2 * cn].decode("utf-16le", errors="replace")
                out["names"].append((content[0x41], name, u64(content, 0) & 0xFFFFFFFFFFFF))
                if not out["mtime"]:
                    out["mtime"] = u64(content, 0x10)
            elif t == 0x80 and nlen == 0 and out["data"] is None:
                out["data"] = {"resident": True, "size": clen, "bytes": bytes(content) if clen <= 4096 else None}
        else:
            if t == 0x80 and nlen == 0 and (out["data"] is None or out["data"].get("vcn", 1) != 0):
                svcn = u64(rec, p + 0x10)
                if svcn == 0:
                    out["data"] = {"resident": False, "size": u64(rec, p + 0x30), "runs": _runs(rec, p + u16(rec, p + 0x20)), "vcn": 0}
        p += ln
    if out["names"]:
        best = sorted(out["names"], key=lambda x: {1: 0, 3: 1, 0: 2, 2: 3}.get(x[0], 4))[0]
        out["name"], out["parent"] = best[1], best[2]
    return out


def _filetime(ft):
    return max(0, (ft - 116444736000000000) / 1e7) if ft else 0


def _ntfs_scan(job, dev, off):
    nt = _ntfs_bpb(dev.read(off, 512))
    if not nt:
        raise ApiError("No valid NTFS boot sector here (repair it first in Boot & Superblock Repair).")
    cs, rs = nt["cs"], nt["rs"]
    rec0 = _ntfs_fixup(dev.read(off + nt["mft"] * cs, rs))
    if not rec0:
        raise ApiError("$MFT record 0 is damaged — try 'Repair $MFT from $MFTMirr' first.")
    a0 = _ntfs_attrs(rec0)
    if not a0.get("data") or a0["data"]["resident"]:
        raise ApiError("Could not read the $MFT data runs.")
    runs, total = a0["data"]["runs"], a0["data"]["size"]
    nrec = total // rs
    dirs, deleted = {}, []
    idx = 0
    for lcn, length in runs:
        if lcn is None:
            idx += length * cs // rs
            continue
        roff, rlen = off + lcn * cs, length * cs
        pos = 0
        while pos < rlen and idx < nrec and not job["cancel"]:
            chunk = dev.read(roff + pos, min(4 * MiB, rlen - pos))
            for k in range(0, len(chunk) - rs + 1, rs):
                if idx >= nrec:
                    break
                raw = chunk[k:k + rs]
                if raw[:4] == b"FILE":
                    rec = _ntfs_fixup(raw)
                    if rec:
                        flags = u16(rec, 0x16)
                        a = _ntfs_attrs(rec)
                        if a.get("name") is not None:
                            if flags & 2:
                                dirs[idx] = (a["name"], a["parent"], bool(flags & 1))
                            elif not flags & 1 and a.get("data") and not a["name"].startswith("$"):
                                d = a["data"]
                                deleted.append({"id": idx, "name": a["name"], "parent": a["parent"], "size": d["size"],
                                                "mtime": _filetime(a["mtime"]), "resident": d["resident"],
                                                "runs": d.get("runs"), "data": d.get("bytes")})
                idx += 1
            pos += len(chunk)
            job["progress"] = min(0.9, idx / max(nrec, 1) * 0.9)
            job["message"] = f"Reading MFT… {idx:,} / {nrec:,} records · {len(deleted):,} deleted files"
            job["live"] = {"records": idx, "total": nrec, "deleted": len(deleted)}
            if not chunk:
                break
    # paths
    cache = {}

    def path_of(pid, depth=0):
        if pid == 5:
            return ""
        if pid in cache:
            return cache[pid]
        if pid not in dirs or depth > 64:
            return f"<lost+found-{pid}>"
        name, par, live = dirs[pid]
        r = path_of(par, depth + 1) + "/" + name + ("" if live else " (deleted)")
        cache[pid] = r
        return r
    # bitmap
    bitmap = None
    try:
        rec6 = _ntfs_fixup(dev.read(off + nt["mft"] * cs + 6 * rs, rs))
        a6 = _ntfs_attrs(rec6) if rec6 else {}
        d6 = a6.get("data")
        if d6 and not d6["resident"] and d6["size"] <= 256 * MiB:
            job["message"] = "Reading cluster allocation bitmap…"
            parts = []
            for lcn, length in d6["runs"]:
                parts.append(dev.read(off + lcn * cs, length * cs) if lcn is not None else b"\0" * (length * cs))
            bitmap = b"".join(parts)[:d6["size"]]
    except Exception:
        bitmap = None
    files = []
    for f in deleted:
        status = "good"
        if not f["resident"]:
            if not f["runs"] or f["size"] == 0:
                status = "empty" if f["size"] == 0 else "no data runs"
            elif bitmap is not None:
                used = tot = 0
                for lcn, length in f["runs"]:
                    if lcn is None:
                        continue
                    for c in range(lcn, lcn + min(length, 200000)):
                        tot += 1
                        if c // 8 < len(bitmap) and bitmap[c // 8] >> (c % 8) & 1:
                            used += 1
                status = "good" if used == 0 else "overwritten" if used == tot else f"partial ({100 * used // max(tot, 1)}% reused)"
            else:
                status = "unknown"
        files.append({"id": f["id"], "name": f["name"], "dir": path_of(f["parent"]) or "/", "size": f["size"],
                      "mtime": f["mtime"], "status": status})
    return {"fs": "ntfs", "geom": nt, "raw": {f["id"]: f for f in deleted}}, files


def _fat_scan(job, dev, off):
    fb = _fat_bpb(dev.read(off, 512))
    if not fb:
        raise ApiError("No valid FAT boot sector here (repair it first in Boot & Superblock Repair).")
    bps, cs = fb["bps"], fb["bps"] * fb["spc"]
    fbytes = fb["fatsz"] * bps
    if fbytes > 512 * MiB:
        raise ApiError("FAT too large to load")
    job["message"] = "Loading FAT…"
    fat = dev.read(off + fb["rsv"] * bps, fbytes)
    data0 = off + (fb["rsv"] + fb["nfat"] * fb["fatsz"] + fb["rds"]) * bps
    ft = fb["ftype"]
    eoc = {"fat12": 0xFF8, "fat16": 0xFFF8, "fat32": 0x0FFFFFF8}[ft]
    maxc = fb["clusters"] + 2

    def nxt(c):
        if ft == "fat32":
            return u32(fat, c * 4) & 0x0FFFFFFF if c * 4 + 4 <= len(fat) else eoc
        if ft == "fat16":
            return u16(fat, c * 2) if c * 2 + 2 <= len(fat) else eoc
        o = c + c // 2
        if o + 2 > len(fat):
            return eoc
        v = u16(fat, o)
        return v >> 4 if c & 1 else v & 0xFFF

    def chain(c, limit=1 << 20):
        out, seen = [], set()
        while 2 <= c < maxc and c not in seen and len(out) < limit:
            out.append(c)
            seen.add(c)
            c = nxt(c)
            if c >= eoc or c == 0:
                break
        return out

    def read_clusters(cl):
        return b"".join(dev.read(data0 + (c - 2) * cs, cs) for c in cl)

    if ft == "fat32":
        root = read_clusters(chain(fb["rootclus"], 4096))
    else:
        root = dev.read(off + (fb["rsv"] + fb["nfat"] * fb["fatsz"]) * bps, fb["rootent"] * 32)
    queue = [(root, "", False)]
    files, raw = [], {}
    visited = set()
    ndirs = 0
    while queue and not job["cancel"] and ndirs < 100000:
        data, path, deld = queue.pop(0)
        ndirs += 1
        lfn = []
        for k in range(0, len(data) - 31, 32):
            e = data[k:k + 32]
            first, attr = e[0], e[11]
            if first == 0:
                break
            if attr == 0x0F:
                part = e[1:11] + e[14:26] + e[28:32]
                lfn.append(part.decode("utf-16le", errors="ignore").split("\x00")[0].replace("￿", ""))
                continue
            name_lfn = "".join(reversed(lfn)) if lfn else None
            lfn = []
            if attr & 0x08:
                continue
            dele = first == 0xE5
            base = e[0:8].decode("cp437", errors="replace").rstrip()
            ext = e[8:11].decode("cp437", errors="replace").rstrip()
            if e[12] & 0x08:
                base = base.lower()
            if e[12] & 0x10:
                ext = ext.lower()
            if dele:
                base = "_" + base[1:]
            sname = base + ("." + ext if ext else "")
            name = name_lfn or sname
            if sname.startswith(".") and sname.strip(".") == "" or name in (".", ".."):
                continue
            clus = (u16(e, 20) << 16 | u16(e, 26)) if ft == "fat32" else u16(e, 26)
            size = u32(e, 28)
            d, t = u16(e, 24), u16(e, 22)
            try:
                mt = time.mktime((1980 + (d >> 9), (d >> 5) & 15 or 1, d & 31 or 1, t >> 11, (t >> 5) & 63, (t & 31) * 2, 0, 0, -1))
            except Exception:
                mt = 0
            if attr & 0x10:
                if 2 <= clus < maxc and clus not in visited:
                    visited.add(clus)
                    if not dele:
                        queue.append((read_clusters(chain(clus, 4096)), path + "/" + name, deld))
                    else:
                        queue.append((read_clusters([clus]), path + "/" + name + " (deleted)", True))
                continue
            if dele or deld:
                dele = True
                n = -(-size // cs) if size else 0
                if clus < 2 or clus >= maxc:
                    status = "empty" if size == 0 else "no start cluster"
                    n = 0
                else:
                    used = sum(1 for c in range(clus, min(clus + n, maxc)) if nxt(c) != 0) if dele else 0
                    status = "good" if used == 0 else "overwritten" if used >= n else f"partial ({100 * used // max(n, 1)}% reused)"
                fid = len(raw)
                raw[fid] = {"clus": clus, "size": size, "n": n, "chain": None if dele else chain(clus)}
                files.append({"id": fid, "name": name, "dir": path or "/", "size": size, "mtime": mt, "status": status})
        job["message"] = f"Scanning directories… {ndirs:,} folders · {len(files):,} deleted files"
        job["progress"] = min(0.95, ndirs / (ndirs + len(queue) + 1))
        job["live"] = {"dirs": ndirs, "deleted": len(files)}
    return {"fs": "vfat", "geom": dict(fb, data0=data0, cs=cs), "raw": raw}, files


def _undel_scan_job(job, path, off):
    dev = RawDev(path)
    try:
        head = dev.read(off, 512)
        if _ntfs_bpb(head):
            meta, files = _ntfs_scan(job, dev, off)
        elif _fat_bpb(head):
            meta, files = _fat_scan(job, dev, off)
        elif _exfat_bpb(head):
            raise ApiError("exFAT undelete by name is not implemented — use File Carving on this partition.")
        elif _ext_sb(dev.read(off + 1024, 1024)):
            raise ApiError("ext3/ext4 erase block pointers when a file is deleted, so undelete by name is not possible. "
                           "Use File Carving on this partition.")
        else:
            raise ApiError("No NTFS or FAT filesystem found at this location.")
    finally:
        dev.close()
    key = secrets.token_hex(6)
    meta.update(path=path, off=off, files={f["id"]: f for f in files})
    REC_UNDEL.clear()
    REC_UNDEL[key] = meta
    job["message"] = f"{len(files):,} deleted file(s) found"
    return {"key": key, "fs": meta["fs"], "files": files[:200000], "truncated": len(files) > 200000}


def api_rec_undel_scan(q, b):
    return {"job": job_start("rec-undel", _undel_scan_job, str(b.get("path", "")), int(b.get("offset", 0)))}


def _same_disk(outdir, path):
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
    except OSError:
        return False
    if not statmod.S_ISBLK(st.st_mode):
        return False
    d = os.stat(outdir).st_dev
    out_k = os.path.basename(os.path.realpath(f"/sys/dev/block/{os.major(d)}:{os.minor(d)}"))
    tgt_k = os.path.basename(real)

    def disk_of(k):
        p = os.path.realpath(f"/sys/class/block/{k}")
        return os.path.basename(os.path.dirname(p)) if os.path.exists(os.path.join(p, "partition")) else k
    return out_k == tgt_k or disk_of(out_k) == disk_of(tgt_k)


def _safe_name(s):
    s = re.sub(r"[\x00/\\]", "_", s).strip() or "unnamed"
    return "_" + s if s in (".", "..") else s[:200]


def _undel_recover_job(job, key, ids, outdir):
    meta = REC_UNDEL.get(key)
    if not meta:
        raise ApiError("Scan results expired — scan again.")
    files = meta["files"]
    dev = RawDev(meta["path"])
    base = os.path.join(outdir, "recovered-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(base, exist_ok=True)
    ok, fail, total = 0, [], 0
    try:
        for n, fid in enumerate(ids):
            if job["cancel"]:
                break
            info = files.get(fid)
            r = meta["raw"].get(fid)
            if not info or not r:
                continue
            rel = "/".join(_safe_name(x) for x in info["dir"].strip("/").split("/") if x)
            d = os.path.join(base, rel)
            os.makedirs(d, exist_ok=True)
            dest = os.path.join(d, _safe_name(info["name"]))
            k = 1
            while os.path.exists(dest):
                k += 1
                dest = os.path.join(d, f"{k}_{_safe_name(info['name'])}")
            try:
                with open(dest, "wb") as out:
                    if meta["fs"] == "ntfs":
                        g = meta["geom"]
                        if r["resident"]:
                            data = r.get("data")
                            if data is None:
                                rec = _ntfs_fixup(dev.read(meta["off"] + g["mft"] * g["cs"] + fid * g["rs"], g["rs"]))
                                data = (_ntfs_attrs(rec).get("data") or {}).get("bytes") or b""
                            out.write(data[:r["size"]])
                        else:
                            left = r["size"]
                            for lcn, length in r["runs"]:
                                n_b = min(length * g["cs"], left)
                                if lcn is None:
                                    out.write(b"\0" * n_b)
                                else:
                                    pos = 0
                                    while pos < n_b:
                                        c = dev.read(meta["off"] + lcn * g["cs"] + pos, min(8 * MiB, n_b - pos))
                                        if not c:
                                            break
                                        out.write(c)
                                        pos += len(c)
                                left -= n_b
                                if left <= 0:
                                    break
                    else:
                        g = meta["geom"]
                        left = r["size"]
                        cl = r["chain"] or list(range(r["clus"], r["clus"] + r["n"]))
                        for c in cl:
                            if left <= 0:
                                break
                            data = dev.read(g["data0"] + (c - 2) * g["cs"], min(g["cs"], left))
                            out.write(data)
                            left -= len(data)
                if info.get("mtime"):
                    os.utime(dest, (info["mtime"], info["mtime"]))
                ok += 1
                total += os.path.getsize(dest)
            except Exception as e:  # noqa
                fail.append(f"{info['name']}: {e}")
            job["progress"] = (n + 1) / len(ids)
            job["message"] = f"Recovered {ok:,} / {len(ids):,} · {total / 1e6:.1f} MB"
    finally:
        dev.close()
    return {"folder": base, "recovered": ok, "failed": fail[:50], "bytes": total}


def api_rec_undel_recover(q, b):
    key = str(b.get("key", ""))
    meta = REC_UNDEL.get(key)
    if not meta:
        raise ApiError("Scan results expired — scan again.")
    outdir = os.path.abspath(os.path.expanduser(str(b.get("outdir", ""))))
    os.makedirs(outdir, exist_ok=True)
    if _same_disk(outdir, meta["path"]):
        raise ApiError("Recover to a different disk — writing to the same disk can overwrite the data you are recovering.")
    ids = [int(x) for x in b.get("ids", [])]
    return {"job": job_start("rec-recover", _undel_recover_job, key, ids, outdir)}


# ---------------------------------------------------------------- file carving (PhotoRec style)

class _BR:
    def __init__(self, dev):
        self.dev, self.bo, self.bb = dev, -1, b""

    def get(self, off, n):
        if self.bo <= off and off + n <= self.bo + len(self.bb):
            return self.bb[off - self.bo:off - self.bo + n]
        if n > 4 * MiB:
            return self.dev.read(off, n)
        self.bo, self.bb = off, self.dev.read(off, 4 * MiB)
        return self.bb[:n]

    def find(self, pat, off, limit):
        end = off + limit
        while off < end:
            blk = self.dev.read(off, min(8 * MiB, end - off))
            if not blk:
                return -1
            i = blk.find(pat)
            if i >= 0:
                return off + i
            if len(blk) < len(pat):
                return -1
            off += len(blk) - len(pat) + 1
        return -1


def _end_jpg(br, s):
    off, mx = s + 2, s + 128 * MiB
    while off < mx:
        h = br.get(off, 4)
        if len(h) < 4 or h[0] != 0xFF:
            return None
        m = h[1]
        if m == 0xFF:
            off += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            off += 2
            continue
        if m == 0xD9:
            return off + 2, "jpg"
        ln = be16(h, 2)
        if ln < 2:
            return None
        if m == 0xDA:
            p = br.find(b"\xff\xd9", off + 2 + ln, mx - off)
            return (p + 2, "jpg") if p > 0 else None
        off += 2 + ln
    return None


def _end_png(br, s):
    off = s + 8
    while off < s + 512 * MiB:
        h = br.get(off, 8)
        if len(h) < 8:
            return None
        ln, typ = be32(h, 0), h[4:8]
        if not typ.isalpha() or ln > 256 * MiB:
            return None
        off += 12 + ln
        if typ == b"IEND":
            return off, "png"
    return None


def _gif_sub(br, off):
    for _ in range(1 << 20):
        b = br.get(off, 1)
        if not b:
            return None
        off += 1 + b[0]
        if b[0] == 0:
            return off
    return None


def _end_gif(br, s):
    h = br.get(s, 13)
    off = s + 13
    if h[10] & 0x80:
        off += 3 * (2 << (h[10] & 7))
    while off and off < s + 64 * MiB:
        b = br.get(off, 1)
        if not b:
            return None
        c = b[0]
        if c == 0x3B:
            return off + 1, "gif"
        if c == 0x21:
            off = _gif_sub(br, off + 2)
        elif c == 0x2C:
            d = br.get(off, 10)
            off += 10
            if d[9] & 0x80:
                off += 3 * (2 << (d[9] & 7))
            off = _gif_sub(br, off + 1)
        else:
            return None
    return None


def _end_pdf(br, s):
    p = br.find(b"%%EOF", s, 256 * MiB)
    if p < 0:
        return None
    end = p + 5
    while True:
        q = br.find(b"%%EOF", end, 4 * MiB)
        if q < 0 or br.find(b"%PDF-", end, q - end) >= 0:
            break
        end = q + 5
    tail = br.get(end, 2)
    end += len(tail) - len(tail.lstrip(b"\r\n"))
    return end, "pdf"


def _end_zip(br, s):
    e = br.find(b"PK\x05\x06", s, 4096 * MiB)
    if e < 0:
        return None
    cl = u16(br.get(e + 20, 2) or b"\0\0", 0)
    head = br.get(s, 8192)
    ext = "zip"
    if b"word/" in head:
        ext = "docx"
    elif b"xl/" in head:
        ext = "xlsx"
    elif b"ppt/" in head:
        ext = "pptx"
    elif b"opendocument.text" in head:
        ext = "odt"
    elif b"opendocument.spreadsheet" in head:
        ext = "ods"
    elif b"opendocument.presentation" in head:
        ext = "odp"
    elif b"application/epub+zip" in head:
        ext = "epub"
    elif b"AndroidManifest.xml" in head:
        ext = "apk"
    elif b"META-INF/" in head:
        ext = "jar"
    return e + 22 + cl, ext


def _end_ole(br, s):
    h = br.get(s, 512)
    ss = 1 << u16(h, 30)
    if ss not in (512, 4096):
        return None
    nfat = u32(h, 0x2C)
    maxsec = 0
    per = ss // 4
    for k in range(min(nfat, 109)):
        sec = u32(h, 0x4C + 4 * k)
        if sec >= 0xFFFFFFFA:
            continue
        maxsec = max(maxsec, sec)
        fs = br.get(s + (sec + 1) * ss, ss)
        if len(fs) < ss:
            return None
        ent = struct.unpack("<%dI" % per, fs)
        for i, v in enumerate(ent):
            if v != 0xFFFFFFFF:
                maxsec = max(maxsec, k * per + i)
    d = br.get(s + (u32(h, 0x30) + 1) * ss, ss)
    names = [d[i * 128:i * 128 + 64].decode("utf-16le", errors="ignore").split("\0")[0] for i in range(ss // 128)]
    ext = "doc" if "WordDocument" in names else "xls" if ("Workbook" in names or "Book" in names) else \
        "ppt" if "PowerPoint Document" in names else "msg" if any(n.startswith("__substg") for n in names) else "ole"
    end = s + (maxsec + 2) * ss
    return (end, ext) if end - s <= 2048 * MiB else None


MP4_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"pdin", b"moof", b"mfra",
             b"styp", b"sidx", b"junk", b"pnot", b"prfl", b"udta"}


def _end_mp4(br, s):
    brand = br.get(s + 8, 4)
    off, seen = s, set()
    while True:
        h = br.get(off, 16)
        if len(h) < 8 or h[4:8] not in MP4_BOXES:
            break
        sz = be32(h, 0)
        if sz == 1:
            sz = be64(h, 8)
        elif sz == 0:
            return None
        if sz < 8:
            break
        seen.add(h[4:8])
        off += sz
        if off - s > 16 * 1024 * MiB:
            return None
    if b"mdat" not in seen and b"moov" not in seen:
        return None
    ext = "mov" if brand == b"qt  " else "m4a" if brand[:3] == b"M4A" else "heic" if brand in (b"heic", b"mif1", b"heix") \
        else "3gp" if brand[:3] == b"3gp" else "mp4"
    return off, ext


def _end_riff(br, s):
    h = br.get(s, 12)
    sz = u32(h, 4) + 8
    return s + sz, {b"WAVE": "wav", b"AVI ": "avi", b"WEBP": "webp"}[h[8:12]]


def _end_sqlite(br, s):
    h = br.get(s, 32)
    ps = be16(h, 16)
    ps = 65536 if ps == 1 else ps
    pc = be32(h, 28)
    if not pc or ps < 512 or ps & (ps - 1):
        return None
    return s + ps * pc, "sqlite"


def _end_7z(br, s):
    h = br.get(s, 32)
    end = s + 32 + u64(h, 12) + u64(h, 20)
    return (end, "7z") if end - s < 64 * 1024 * MiB else None


MP3_BR1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
MP3_BR2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]


def _end_mp3(br, s):
    h = br.get(s, 10)
    sz = (h[6] & 127) << 21 | (h[7] & 127) << 14 | (h[8] & 127) << 7 | (h[9] & 127)
    off = s + 10 + sz + (10 if h[5] & 0x10 else 0)
    frames = 0
    while off - s < 512 * MiB:
        f = br.get(off, 4)
        if len(f) < 4 or f[0] != 0xFF or (f[1] & 0xE0) != 0xE0:
            break
        ver, layer, bri, sri, pad = (f[1] >> 3) & 3, (f[1] >> 1) & 3, f[2] >> 4, (f[2] >> 2) & 3, (f[2] >> 1) & 1
        if layer != 1 or bri in (0, 15) or sri == 3 or ver == 1:
            break
        if ver == 3:
            fl = 144000 * MP3_BR1[bri] // [44100, 48000, 32000][sri] + pad
        else:
            sr = [22050, 24000, 16000][sri] // (2 if ver == 0 else 1)
            fl = 72000 * MP3_BR2[bri] // sr + pad
        if fl < 24:
            break
        off += fl
        frames += 1
    if frames < 10:
        return None
    if br.get(off, 3) == b"TAG":
        off += 128
    return off, "mp3"


def _end_ogg(br, s):
    off, pages = s, 0
    while off - s < 2048 * MiB:
        h = br.get(off, 27)
        if len(h) < 27 or h[:4] != b"OggS":
            break
        seg = br.get(off + 27, h[26])
        off += 27 + h[26] + sum(seg)
        pages += 1
        if h[5] & 4:
            return off, "ogg"
    return (off, "ogg") if pages > 2 else None


def _end_bmp(br, s):
    h = br.get(s, 18)
    sz, doff, dib = u32(h, 2), u32(h, 10), u32(h, 14)
    if dib not in (12, 40, 52, 56, 108, 124) or not 54 <= sz <= 256 * MiB or doff >= sz:
        return None
    return s + sz, "bmp"


CARVERS = {"jpg": _end_jpg, "png": _end_png, "gif": _end_gif, "pdf": _end_pdf, "zip": _end_zip, "ole": _end_ole,
           "mp4": _end_mp4, "riff": _end_riff, "sqlite": _end_sqlite, "7z": _end_7z, "mp3": _end_mp3,
           "ogg": _end_ogg, "bmp": _end_bmp}
CARVE_MIN = {"jpg": 512, "png": 67, "gif": 35, "bmp": 64, "pdf": 256, "zip": 64, "ole": 1536, "mp4": 64,
             "riff": 44, "sqlite": 512, "7z": 32, "mp3": 1024, "ogg": 128}


def _match_header(h):
    if h[:3] == b"\xff\xd8\xff":
        return "jpg"
    if h[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if h[:5] == b"%PDF-":
        return "pdf"
    if h[:4] == b"PK\x03\x04":
        return "zip"
    if h[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"
    if h[4:8] == b"ftyp":
        return "mp4"
    if h[:4] == b"RIFF" and h[8:12] in (b"WAVE", b"AVI ", b"WEBP"):
        return "riff"
    if h[:16] == b"SQLite format 3\x00":
        return "sqlite"
    if h[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if h[:3] == b"ID3":
        return "mp3"
    if h[:4] == b"OggS" and h[5] & 2:
        return "ogg"
    if h[:2] == b"BM" and h[6:10] == b"\0\0\0\0":
        return "bmp"
    return None


def _carve_job(job, path, start, end, types, outdir, align):
    dev = RawDev(path)
    br = _BR(dev)
    base = os.path.join(outdir, "recup-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(base, exist_ok=True)
    counts, recent = {}, []
    nbytes = nfiles = 0
    t0 = time.time()
    try:
        end = min(end or dev.size, dev.size)
        pos = start // align * align
        CH = 16 * MiB
        while pos < end and not job["cancel"]:
            buf = dev.read(pos, min(CH, end - pos))
            if not buf:
                break
            i = 0
            nxt = None
            L = len(buf)
            while i + 16 <= L:
                t = _match_header(buf[i:i + 16])
                if t and t in types:
                    s = pos + i
                    try:
                        r = CARVERS[t](br, s)
                    except Exception:
                        r = None
                    if r and r[0] - s >= CARVE_MIN.get(t, 1) and r[0] <= dev.size:
                        e, ext = r
                        d = os.path.join(base, ext)
                        os.makedirs(d, exist_ok=True)
                        fn = os.path.join(d, f"f{s // 512:010d}.{ext}")
                        with open(fn, "wb") as out:
                            p = s
                            while p < e:
                                c = dev.read(p, min(8 * MiB, e - p))
                                if not c:
                                    break
                                out.write(c)
                                p += len(c)
                        sz = e - s
                        counts[ext] = counts.get(ext, 0) + 1
                        nfiles += 1
                        nbytes += sz
                        recent.append({"name": os.path.relpath(fn, base), "size": sz, "offset": s})
                        recent = recent[-60:]
                        np_ = (e + align - 1) // align * align
                        if np_ >= pos + L:
                            nxt = np_
                            break
                        i = np_ - pos
                        continue
                i += align
            pos = nxt if nxt else pos + L
            el = max(time.time() - t0, 1e-3)
            done = pos - start
            rate = done / el
            job["progress"] = min(0.999, done / max(end - start, 1))
            job["message"] = f"Scanned {done / 1e9:.2f} / {(end - start) / 1e9:.2f} GB · {nfiles:,} files recovered"
            job["live"] = {"pos": pos, "total": end - start, "done": done, "rate": rate,
                           "eta": (end - pos) / rate if rate > 0 else None, "counts": dict(counts),
                           "files": nfiles, "bytes": nbytes, "recent": list(recent)}
    finally:
        dev.close()
    return {"folder": base, "files": nfiles, "bytes": nbytes, "counts": counts, "recent": recent}


def api_rec_carve(q, b):
    path = str(b.get("path", ""))
    outdir = os.path.abspath(os.path.expanduser(str(b.get("outdir", ""))))
    os.makedirs(outdir, exist_ok=True)
    if _same_disk(outdir, path):
        raise ApiError("Save recovered files to a different disk than the one being scanned.")
    types = [t for t in (b.get("types") or list(CARVERS)) if t in CARVERS]
    if not types:
        raise ApiError("Select at least one file type")
    align = 512 if int(b.get("align", 512)) == 512 else 4096
    return {"job": job_start("rec-carve", _carve_job, path, int(b.get("start", 0)), int(b.get("end", 0) or 0),
                             set(types), outdir, align)}


def api_rec_search(q, b):
    mode = b.get("mode", "quick")
    if mode not in ("quick", "deep"):
        raise ApiError("Invalid mode")
    return {"job": job_start("rec-search", _psearch_job, str(b.get("path", "")), mode,
                             int(b.get("start", 0) or 0), int(b.get("end", 0) or 0))}


def api_rec_targets(q, b):
    disks = [_norm(d) for d in _lsblk()]
    out = []
    for d in disks:
        if d["size"] == 0 or d["type"] == "rom":
            continue
        if d["type"] == "loop" and any(m.startswith("/snap/") for m in _all_mounts(d)):
            continue
        out.append({"path": d["path"], "type": d["type"], "size": d["size"], "model": d.get("model") or "",
                    "fstype": d.get("fstype"), "label": d.get("label"), "mounted": _all_mounts(d),
                    "critical": bool(set(_all_mounts(d)) & CRITICAL_MOUNTS), "pttype": d.get("pttype"),
                    "parts": [{"path": c["path"], "size": c["size"], "start": c.get("start"), "fstype": c.get("fstype"),
                               "label": c.get("label"), "mounted": _all_mounts(c)}
                              for c in d.get("children", []) if c.get("type") == "part"]})
    return {"targets": out, "testdisk": bool(which("testdisk")), "photorec": bool(which("photorec")),
            "helper": os.path.exists(HELPER_INSTALLED)}


def api_rec_external(q, b):
    tool = b.get("tool")
    if tool not in ("testdisk", "photorec"):
        raise ApiError("Unknown tool")
    exe = which(tool)
    if not exe:
        raise ApiError(f"{tool} is not installed (sudo apt install testdisk).")
    path = os.path.realpath(str(b.get("path", "")))
    if not os.path.exists(path):
        raise ApiError("Target not found")
    pk = which("pkexec") or "sudo"
    for term, args in (("gnome-terminal", ["--"]), ("konsole", ["-e"]), ("xfce4-terminal", ["-x"]),
                       ("mate-terminal", ["-x"]), ("x-terminal-emulator", ["-e"]), ("xterm", ["-e"])):
        t = shutil.which(term)
        if t:
            subprocess.Popen([t] + args + [pk, exe, path], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "message": f"Opened {tool} in {term}"}
    raise ApiError("No terminal emulator found")


# --------------------------------------------------------------------------- jobs API + quit


def api_job(q, b):
    j = JOBS.get(q.get("id", ""))
    if not j:
        raise ApiError("Unknown job")
    out = {k: v for k, v in list(j.items()) if not k.startswith("_")}
    live = dict(out.get("live") or {})
    if isinstance(live.get("log"), list):
        live["log"] = live["log"][-400:]
    out["live"] = live
    return out


def api_job_cancel(q, b):
    j = JOBS.get(str(b.get("id", "")))
    if j:
        j["cancel"] = True
    return {"ok": True}


# ---- single instance (v1.3.1) -------------------------------------------
# instance.json (0600, in the 0700 data dir) holds pid/port/token of the running server so a
# second launch reopens the existing window instead of starting another server on a random port.
INSTANCE_FILE = lambda: os.path.join(DATA_DIR, "instance.json")  # noqa: E731


def _proc_is_msp(pid):
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            return False
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        return any(re.search(rb"(?i)mysyspilot[^/]*\.py$", c) for c in cmd)
    except Exception:
        return False


def _proc_ports(pid):
    """127.0.0.1 LISTEN ports owned by pid (from /proc/net/tcp + fd inodes)."""
    inodes = set()
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                l = os.readlink(f"/proc/{pid}/fd/{fd}")
                if l.startswith("socket:["):
                    inodes.add(l[8:-1])
            except OSError:
                pass
    except OSError:
        return []
    ports = []
    try:
        for line in open("/proc/net/tcp").read().splitlines()[1:]:
            f = line.split()
            if f[3] == "0A" and f[9] in inodes:
                ports.append(int(f[1].split(":")[1], 16))
    except Exception:
        pass
    return sorted(ports)


def _other_instances():
    me = os.getpid()
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit() and int(d) != me and _proc_is_msp(int(d)):
            try:
                st = os.stat(f"/proc/{d}")
                with open(f"/proc/{d}/stat") as f:
                    start_ticks = int(f.read().rsplit(")", 1)[1].split()[19])
                boot = float(open("/proc/stat").read().split("btime ")[1].split()[0])
                started = boot + start_ticks / os.sysconf("SC_CLK_TCK")
            except Exception:
                started = None
            out.append({"pid": int(d), "ports": _proc_ports(int(d)), "started": started})
    return out


def _read_instance():
    try:
        with open(INSTANCE_FILE()) as f:
            d = json.load(f)
        if _proc_is_msp(int(d["pid"])):
            c = http.client.HTTPConnection("127.0.0.1", int(d["port"]), timeout=2)
            c.request("GET", "/api/meta", headers={"X-MSP-Token": d["token"], "Host": f"127.0.0.1:{d['port']}"})
            r = c.getresponse()
            if r.status == 200:
                d["meta"] = json.loads(r.read() or b"{}")
                return d
    except Exception:
        pass
    return None


def _write_instance():
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    fd = os.open(INSTANCE_FILE() + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"pid": os.getpid(), "port": PORT, "token": TOKEN, "version": APP_VERSION}, f)
    os.replace(INSTANCE_FILE() + ".tmp", INSTANCE_FILE())


def _clear_instance():
    try:
        with open(INSTANCE_FILE()) as f:
            if json.load(f).get("pid") == os.getpid():
                os.unlink(INSTANCE_FILE())
    except Exception:
        pass


def api_instances(q, b):
    return {"self": {"pid": os.getpid(), "port": PORT, "version": APP_VERSION}, "others": _other_instances()}


def api_instance_stop(q, b):
    pid = int(b.get("pid") or 0)
    if not pid or pid == os.getpid() or not _proc_is_msp(pid):
        raise ApiError("Not a MySysPilot process owned by you")
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not os.path.exists(f"/proc/{pid}"):
            return {"ok": True}
        time.sleep(0.1)
    return {"ok": True, "note": "SIGTERM sent; process still exiting"}


def api_quit(q, b):
    threading.Timer(0.4, lambda: (_clear_instance(), os._exit(0))).start()
    return {"ok": True}


ROUTES_GET = {
    "/api/meta": api_meta, "/api/instances": api_instances, "/api/stats": api_stats, "/api/history": api_history, "/api/sysinfo": api_sysinfo,
    "/api/processes": api_processes, "/api/services": api_services, "/api/startup": api_startup,
    "/api/packages": api_packages, "/api/repos": api_repos, "/api/disks": api_disks,
    "/api/disk/smart": api_disk_smart, "/api/disk/fstab": api_fstab, "/api/speed/servers": api_speed_servers,
    "/api/speed/history": api_speed_history, "/api/job": api_job,
    "/api/backups": api_backups, "/api/disks/stamp": api_disks_stamp, "/api/docker/status": api_docker_status, "/api/docker/list": api_docker_list,
    "/api/docker/logs": api_docker_logs, "/api/docker/inspect": api_docker_inspect,
    "/api/rec/targets": api_rec_targets, "/api/rec/undo": api_rec_undo, "/api/rec/mounts": api_rec_mounts,
}
ROUTES_POST = {
    "/api/process/signal": api_process_signal, "/api/process/renice": api_process_renice,
    "/api/service": api_service_action, "/api/startup/toggle": api_startup_toggle,
    "/api/startup/add": api_startup_add, "/api/startup/delete": api_startup_delete,
    "/api/cleaner/scan": api_cleaner_scan, "/api/cleaner/clean": api_cleaner_clean,
    "/api/packages/remove": api_package_remove, "/api/packages/autoremove": api_package_autoremove,
    "/api/repos/toggle": api_repo_toggle, "/api/disk/action": api_disk_action2,
    "/api/backups/verify": api_backup_verify, "/api/backups/forget": api_backup_forget,
    "/api/dupes/of": api_dupes_of, "/api/search/start": api_search_start, "/api/search/delete": api_search_delete,
    "/api/docker/action": api_docker_action, "/api/docker/template_save": api_docker_template_save,
    "/api/rec/search": api_rec_search, "/api/rec/write_table": api_rec_write_table, "/api/rec/mount": api_rec_mount,
    "/api/rec/unmount": api_rec_unmount, "/api/rec/analyze": api_rec_analyze, "/api/rec/repair": api_rec_repair,
    "/api/rec/undo/revert": api_rec_undo_revert, "/api/rec/undelete/scan": api_rec_undel_scan,
    "/api/rec/undelete/recover": api_rec_undel_recover, "/api/rec/carve": api_rec_carve, "/api/rec/external": api_rec_external,
    "/api/dupes/start": api_dupes_start, "/api/dupes/delete": api_dupes_delete, "/api/open": api_open,
    "/api/speed/start": api_speed_start, "/api/speed/pingall": api_speed_pingall,
    "/api/speed/history/clear": api_speed_history_clear, "/api/job/cancel": api_job_cancel, "/api/quit": api_quit, "/api/instance/stop": api_instance_stop,
}


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{APP_VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _guard(self):
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            self._send(403, {"error": "Forbidden host"})
            return False
        return True

    def _authed(self):
        if not secrets.compare_digest(self.headers.get("X-MSP-Token", ""), TOKEN):
            self._send(401, {"error": "Missing or invalid session token. Relaunch MySysPilot."})
            return False
        return True

    def _dispatch(self, routes, body):
        u = urllib.parse.urlsplit(self.path)
        fn = routes.get(u.path)
        if not fn:
            return self._send(404, {"error": "Not found"})
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            self._send(200, fn(q, body))
        except ApiError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa
            self._send(500, {"error": f"{e.__class__.__name__}: {e}"})

    def do_GET(self):
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                   "img-src data:; connect-src 'self'; font-src data:; base-uri 'none'; form-action 'none'; "
                   "frame-ancestors 'none'")
            return self._send(200, UI_HTML.encode(), "text/html; charset=utf-8", {"Content-Security-Policy": csp})
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if path.startswith("/api/"):
            if not self._authed():
                return
            return self._dispatch(ROUTES_GET, {})
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._guard() or not self._authed():
            return
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > 4 * 1024 * 1024:
            return self._send(413, {"error": "Request too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return self._send(400, {"error": "Invalid JSON"})
        self._dispatch(ROUTES_POST, body)


# --------------------------------------------------------------------------- launcher

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
<defs>
<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#3b82f6"/>
<stop offset="1" stop-color="#22d3ee"/>
</linearGradient>
</defs>
<rect width="128" height="128" rx="28" fill="#0b1426"/>
<circle cx="64" cy="64" r="40" fill="none" stroke="#1e2a44" stroke-width="10"/>
<path d="M64 24a40 40 0 1 1-34.6 60" fill="none" stroke="url(#g)" stroke-width="10" stroke-linecap="round"/>
<path d="M64 64 86 46" stroke="#22e39a" stroke-width="7" stroke-linecap="round"/>
<circle cx="64" cy="64" r="7" fill="#22e39a"/>
</svg>"""


def install_desktop():
    script = os.path.abspath(__file__)
    ensure_data_dir()
    icon = os.path.join(DATA_DIR, "mysyspilot.svg")
    with open(icon, "w") as f:
        f.write(ICON_SVG)
    appdir = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local", "share")), "applications")
    os.makedirs(appdir, exist_ok=True)
    path = os.path.join(appdir, "mysyspilot.desktop")
    with open(path, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=MySysPilot\n"
                "Comment=System optimizer, monitor, disk utility and speed test\n"
                f"Exec={sys.executable} \"{script}\"\nIcon={icon}\nTerminal=false\n"
                "Categories=System;Monitor;Utility;\nStartupNotify=true\n")
    os.chmod(path, 0o755)
    print(f"Launcher installed: {path}")



def open_app_window(url):
    for b in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser",
              "microsoft-edge", "vivaldi"):
        exe = shutil.which(b)
        if exe:
            try:
                subprocess.Popen([exe, f"--app={url}", "--new-window", "--window-size=1500,960"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                return b
            except Exception:
                continue
    webbrowser.open(url)
    return "default browser"


def main():
    global PORT
    ap = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--install-desktop", action="store_true")
    ap.add_argument("--new-instance", action="store_true",
                    help="start a separate server even if one is already running")
    a = ap.parse_args()
    if a.install_desktop:
        install_desktop()
        return
    if os.geteuid() == 0:
        print("Warning: running as root is not recommended; MySysPilot elevates per action via polkit.",
              file=sys.stderr)
    if not a.new_instance:
        cur = _read_instance()
        if cur:
            url = f"http://127.0.0.1:{cur['port']}/#t={cur['token']}"
            ver = (cur.get("meta") or {}).get("version", "?")
            if ver == APP_VERSION:
                print(f"{APP_NAME} v{ver} is already running (pid {cur['pid']}, port {cur['port']}) — reopening it.")
                if not a.no_browser:
                    open_app_window(url)
                else:
                    print(f"Open: {url}")
                return
            print(f"Replacing running {APP_NAME} v{ver} (pid {cur['pid']}) with v{APP_VERSION}.")
            try:
                os.kill(int(cur["pid"]), signal.SIGTERM)
                for _ in range(30):
                    if not os.path.exists(f"/proc/{cur['pid']}"):
                        break
                    time.sleep(0.1)
            except OSError:
                pass
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    PORT = httpd.server_address[1]
    ALLOWED_HOSTS.update({f"127.0.0.1:{PORT}", f"localhost:{PORT}"})
    SAMPLER.start()
    try:
        if not a.new_instance:
            _write_instance()
    except Exception as e:
        print(f"Note: could not write instance file: {e}", file=sys.stderr)
    import atexit
    atexit.register(_clear_instance)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if PORT != a.port:
        print(f"Port {a.port} is in use; using {PORT}.", file=sys.stderr)
    url = f"http://127.0.0.1:{PORT}/#t={TOKEN}"
    print(f"{APP_NAME} v{APP_VERSION} running at http://127.0.0.1:{PORT}/  (Ctrl+C to stop)")
    if a.no_browser:
        print(f"Open: {url}")
    else:
        how = open_app_window(url)
        print(f"Opened in {how}. If no window appeared, open: {url}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
