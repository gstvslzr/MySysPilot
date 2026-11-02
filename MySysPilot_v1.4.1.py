#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Gustavo Salazar
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

UI_HTML = r'''<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MySysPilot v1.4.1</title>
<style>
:root{
  --bg:#060b17;--bg2:#0a1324;--card:rgba(13,22,41,.78);--card2:rgba(18,30,54,.85);--inset:rgba(4,10,22,.55);
  --line:rgba(86,140,220,.16);--line2:rgba(56,189,248,.38);--text:#e7f0ff;--muted:#8ea3c4;--dim:#54688a;
  --track:rgba(120,155,210,.13);--track2:rgba(120,155,210,.22);
  --cyan:#22d3ee;--blue:#3b82f6;--green:#1ee39a;--violet:#a78bfa;--amber:#fbbf24;--red:#fb4f6e;--pink:#f472b6;
  --shadow:0 18px 40px rgba(0,0,0,.45);--glow:1;--radius:16px;
  --font:"Inter","Segoe UI","Ubuntu","Cantarell",system-ui,-apple-system,sans-serif;
  --mono:"JetBrains Mono","Ubuntu Mono","DejaVu Sans Mono",ui-monospace,monospace;
}
:root[data-theme="light"]{
  --bg:#eaf0f8;--bg2:#dde6f3;--card:rgba(255,255,255,.88);--card2:#ffffff;--inset:rgba(226,234,246,.8);
  --line:rgba(28,72,140,.14);--line2:rgba(14,116,190,.35);--text:#0c1a33;--muted:#4c5f7d;--dim:#8a9ab3;
  --track:rgba(25,60,120,.10);--track2:rgba(25,60,120,.18);
  --cyan:#0aa5c2;--blue:#2563eb;--green:#04a86c;--violet:#7c3aed;--amber:#d48806;--red:#e11d48;--pink:#db2777;
  --shadow:0 10px 28px rgba(20,40,80,.12);--glow:.35;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{font-family:var(--font);color:var(--text);background:var(--bg);font-size:14px;overflow:hidden;
  background-image:radial-gradient(1100px 600px at 12% -10%,rgba(59,130,246,.16),transparent 60%),
  radial-gradient(900px 600px at 100% 110%,rgba(34,211,238,.10),transparent 60%),
  radial-gradient(700px 500px at 70% 10%,rgba(167,139,250,.08),transparent 60%);}
:root[data-theme="light"] body{background-image:radial-gradient(1100px 600px at 12% -10%,rgba(59,130,246,.10),transparent 60%),radial-gradient(900px 600px at 100% 110%,rgba(34,211,238,.10),transparent 60%)}
button,input,select,textarea{font:inherit;color:inherit}
.app{display:grid;grid-template-columns:236px 1fr;height:100vh}
/* sidebar */
.side{border-right:1px solid var(--line);background:linear-gradient(180deg,rgba(10,19,38,.9),rgba(6,11,23,.9));display:flex;flex-direction:column;min-height:0}
:root[data-theme="light"] .side{background:linear-gradient(180deg,#f6f9fd,#eaf0f8)}
.brand{display:flex;gap:10px;align-items:center;padding:18px 16px 14px}
.brand svg{width:34px;height:34px;filter:drop-shadow(0 0 8px rgba(34,211,238,calc(.5*var(--glow))))}
.brand b{font-size:16px;letter-spacing:.3px}.brand small{display:block;color:var(--muted);font-size:9.5px;letter-spacing:1.2px;white-space:nowrap}
.nav{flex:1;overflow:auto;padding:6px 10px 10px}
.nav .grp{color:var(--dim);font-size:10.5px;letter-spacing:1.6px;text-transform:uppercase;margin:14px 8px 6px}
.nav a{display:flex;align-items:center;gap:11px;padding:9px 10px;border-radius:10px;color:var(--muted);text-decoration:none;cursor:pointer;border:1px solid transparent;margin:2px 0;transition:.15s}
.nav a svg{width:18px;height:18px;flex:none}
.nav a:hover{background:var(--track);color:var(--text)}
.nav a.on{color:var(--text);background:linear-gradient(90deg,rgba(59,130,246,.22),rgba(34,211,238,.06));border-color:var(--line2);box-shadow:inset 3px 0 0 var(--cyan)}
.side-foot{padding:12px 14px;border-top:1px solid var(--line);display:flex;gap:8px;align-items:center;color:var(--muted);font-size:12px}
/* main */
.main{display:flex;flex-direction:column;min-width:0;min-height:0}
.top{display:flex;align-items:center;gap:14px;padding:14px 24px;border-bottom:1px solid var(--line)}
.top h1{font-size:19px;margin:0;font-weight:650;letter-spacing:.2px}
.top .sub{color:var(--muted);font-size:12.5px}
.top .sp{flex:1}
.pill{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--muted);font-size:12px;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green)}
.dot.red{background:var(--red);box-shadow:0 0 10px var(--red)}.dot.amber{background:var(--amber);box-shadow:0 0 10px var(--amber)}
.content{flex:1;overflow:auto;padding:22px 24px 40px}
.page{display:none;animation:fade .25s ease}.page.on{display:block}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
/* cards */
.grid{display:grid;gap:18px}
.g12{grid-template-columns:repeat(12,minmax(0,1fr))}
.c3{grid-column:span 3}.c4{grid-column:span 4}.c5{grid-column:span 5}.c6{grid-column:span 6}.c7{grid-column:span 7}.c8{grid-column:span 8}.c12{grid-column:span 12}
@media(max-width:1380px){.c3{grid-column:span 6}.c4{grid-column:span 6}.c5,.c7{grid-column:span 12}.c8{grid-column:span 12}}
@media(max-width:900px){.app{grid-template-columns:1fr}.side{display:none}.c3,.c4,.c6{grid-column:span 12}}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:18px;box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.04);position:relative;backdrop-filter:blur(8px)}
.card.glow{border-color:var(--line2);box-shadow:var(--shadow),0 0 0 1px rgba(34,211,238,calc(.06*var(--glow))),0 0 30px rgba(34,211,238,calc(.08*var(--glow))) inset}
.card h3{margin:0 0 12px;font-size:12px;letter-spacing:1.6px;text-transform:uppercase;color:var(--muted);font-weight:600;display:flex;align-items:center;gap:8px}
.card h3 svg{width:16px;height:16px;color:var(--cyan)}
.card h3 .r{margin-left:auto;text-transform:none;letter-spacing:0;font-weight:500}
.center{display:flex;align-items:center;justify-content:center}
.muted{color:var(--muted)}.dim{color:var(--dim)}.mono{font-family:var(--mono)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.row.sb{justify-content:space-between}
.spacer{flex:1}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:7px 16px;font-size:13px}
.kv div:nth-child(odd){color:var(--muted)}
.kv div:nth-child(even){overflow-wrap:anywhere}
/* gauges */
.gauge{width:100%;max-width:230px;height:auto;display:block;margin:auto;overflow:visible}
.g-label{fill:var(--muted);font-size:11px;letter-spacing:2px;text-anchor:middle;font-weight:600}
.g-value{fill:var(--text);font-size:34px;font-weight:600;text-anchor:middle;font-family:var(--mono);letter-spacing:-.5px}
.g-sub{fill:var(--muted);font-size:10.5px;letter-spacing:1.6px;text-anchor:middle;font-weight:600}
.g-unit{font-size:20px}
.arcv{transition:stroke-dasharray .6s cubic-bezier(.2,.8,.2,1)}
.seg{transition:fill .35s}
/* digit panel */
.dp{display:flex;align-items:flex-start;justify-content:center;gap:6px;background:var(--inset);border:1px solid var(--line);border-radius:12px;padding:14px 10px 10px}
.dp-g{display:flex;flex-direction:column;align-items:center;min-width:58px}
.dp-d{font-family:var(--mono);font-size:42px;font-weight:600;line-height:1;color:var(--c);text-shadow:0 0 calc(18px*var(--glow)) var(--c)}
.dp-l{font-size:10.5px;color:var(--c);letter-spacing:1.6px;margin-top:8px;font-weight:600;opacity:.9}
.dp-s{font-family:var(--mono);font-size:36px;color:var(--muted);line-height:1.05}
/* linear bar */
.bar{height:10px;border-radius:999px;background:var(--track);overflow:hidden;position:relative}
.bar>i{position:absolute;inset:0 auto 0 0;border-radius:999px;background:linear-gradient(90deg,var(--blue),var(--cyan),var(--green));box-shadow:0 0 calc(14px*var(--glow)) rgba(34,211,238,.6);transition:width .6s cubic-bezier(.2,.8,.2,1)}
.bar.sm{height:6px}.bar.warn>i{background:linear-gradient(90deg,var(--amber),var(--red))}.bar.vio>i{background:linear-gradient(90deg,var(--violet),var(--pink))}
.lb{display:flex;align-items:center;gap:12px}
.lb .ic{width:36px;height:36px;border-radius:10px;display:grid;place-items:center;background:rgba(59,130,246,.12);color:var(--cyan);border:1px solid var(--line)}
.lb .ic svg{width:20px;height:20px}
.lb .t{font-size:16px;font-weight:600}.lb .p{margin-left:auto;font-family:var(--mono);font-size:15px}
/* HUD */
.hud{position:relative;padding:18px 24px;min-height:112px}
.hud>svg.frame{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.hud{--hf:1}:root[data-theme="light"] .hud svg.frame polygon:first-of-type{fill-opacity:.35}.hud .in{position:relative;display:flex;align-items:center;gap:18px}
.hud .reticle{width:52px;height:52px;color:var(--cyan);filter:drop-shadow(0 0 calc(8px*var(--glow)) var(--cyan));flex:none}
.hud .lbl{font-size:11px;letter-spacing:2px;color:var(--muted);font-weight:600}
.hud .val{font-family:var(--mono);font-size:30px;font-weight:600;letter-spacing:.5px}
.hud .val small{font-size:14px;color:var(--muted);margin-left:4px}
.slash{display:flex;gap:5px;margin-left:auto}
.slash i{width:12px;height:22px;transform:skewX(-28deg);background:var(--track2);border-radius:2px;transition:.3s}
.slash i.on{background:var(--c,var(--cyan));box-shadow:0 0 calc(10px*var(--glow)) var(--c,var(--cyan))}
/* status card */
.status .hd{display:flex;align-items:center;gap:8px;color:var(--green);font-weight:700;letter-spacing:1.6px;font-size:12px;margin-bottom:10px}
.status .ln{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px dashed var(--line);font-size:13px}
.status .ln svg{width:15px;height:15px;color:var(--muted)}
.status .ln b{margin-left:auto;font-family:var(--mono);font-weight:500;max-width:60%;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* mini rings */
.minis{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:14px}
.mini{display:flex;flex-direction:column;align-items:center}
.mini>svg{width:92px;height:92px}
/* buttons/inputs */
.btn{display:inline-flex;align-items:center;gap:7px;padding:8px 14px;border-radius:10px;border:1px solid var(--line2);background:linear-gradient(180deg,rgba(59,130,246,.22),rgba(59,130,246,.10));color:var(--text);cursor:pointer;font-weight:550;white-space:nowrap;transition:.15s}
.btn:hover{box-shadow:0 0 0 1px var(--line2),0 0 18px rgba(34,211,238,calc(.25*var(--glow)))}
.btn:disabled{opacity:.45;cursor:not-allowed;box-shadow:none}
.btn svg{width:16px;height:16px}
.btn.pri{background:linear-gradient(90deg,var(--blue),var(--cyan));border-color:transparent;color:#fff}
.btn.ok{background:linear-gradient(90deg,#0d9e6c,var(--green));border-color:transparent;color:#032015}
.btn.danger{background:linear-gradient(180deg,rgba(251,79,110,.25),rgba(251,79,110,.10));border-color:rgba(251,79,110,.55)}
.btn.ghost{background:transparent;border-color:var(--line)}
.btn.sm{padding:5px 10px;font-size:12.5px;border-radius:8px}
.btn.ico{padding:6px;border-radius:8px}
input[type=text],input[type=number],input[type=password],select,textarea{background:var(--inset);border:1px solid var(--line);border-radius:10px;padding:8px 11px;outline:none;min-width:0}
input:focus,select:focus,textarea:focus{border-color:var(--line2);box-shadow:0 0 0 3px rgba(34,211,238,.12)}
select option{background:var(--bg2);color:var(--text)}
textarea{width:100%;resize:vertical;font-family:var(--mono);font-size:12.5px}
label.chk{display:inline-flex;gap:7px;align-items:center;cursor:pointer;color:var(--muted)}
input[type=checkbox]{accent-color:var(--cyan);width:15px;height:15px}
.sw{position:relative;width:40px;height:22px;flex:none;display:inline-block}
.sw input{opacity:0;width:0;height:0}
.sw span{position:absolute;inset:0;border-radius:999px;background:var(--track2);transition:.2s;cursor:pointer;border:1px solid var(--line)}
.sw span:after{content:"";position:absolute;width:16px;height:16px;left:2px;top:2px;border-radius:50%;background:#fff;transition:.2s}
.sw input:checked+span{background:linear-gradient(90deg,var(--blue),var(--cyan));box-shadow:0 0 12px rgba(34,211,238,calc(.4*var(--glow)))}
.sw input:checked+span:after{transform:translateX(18px)}
.search{position:relative}.search input{padding-left:32px;width:260px}.search svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);width:15px;height:15px;color:var(--dim)}
.tabs{display:inline-flex;background:var(--inset);border:1px solid var(--line);border-radius:11px;padding:3px}
.tabs button{border:0;background:none;padding:6px 14px;border-radius:8px;cursor:pointer;color:var(--muted);font-weight:550}
.tabs button.on{background:linear-gradient(90deg,rgba(59,130,246,.35),rgba(34,211,238,.2));color:var(--text)}
/* tables */
.tbl-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px;max-height:calc(100vh - 270px)}
table.t{width:100%;border-collapse:collapse;font-size:13px}
table.t th{position:sticky;top:0;background:var(--card2);text-align:left;font-weight:600;color:var(--muted);font-size:11.5px;letter-spacing:1px;text-transform:uppercase;padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;z-index:1}
:root[data-theme="dark"] table.t th{background:#0e1830}
table.t td{padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
table.t tr:hover td{background:rgba(59,130,246,.06)}
table.t tr.sel td{background:rgba(34,211,238,.12)}
table.t td.num{font-family:var(--mono);text-align:right;white-space:nowrap}
table.t td.trunc{max-width:420px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:600;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.badge.g{color:var(--green);border-color:rgba(30,227,154,.4);background:rgba(30,227,154,.08)}
.badge.r{color:var(--red);border-color:rgba(251,79,110,.45);background:rgba(251,79,110,.08)}
.badge.a{color:var(--amber);border-color:rgba(251,191,36,.45);background:rgba(251,191,36,.08)}
.badge.b{color:var(--cyan);border-color:var(--line2);background:rgba(34,211,238,.08)}
.badge.v{color:var(--violet);border-color:rgba(167,139,250,.45);background:rgba(167,139,250,.08)}
/* charts */
.chart{width:100%;height:210px;display:block}
.legend{display:flex;gap:14px;font-size:12px;color:var(--muted);flex-wrap:wrap}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
/* cleaner */
.cats{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}
.cat{border:1px solid var(--line);border-radius:14px;padding:14px;background:var(--inset);cursor:pointer;transition:.15s;position:relative}
.cat.on{border-color:var(--line2);box-shadow:0 0 0 1px var(--line2),0 0 20px rgba(34,211,238,calc(.12*var(--glow)))}
.cat .nm{display:flex;gap:9px;align-items:center;font-weight:600}
.cat .nm svg{width:18px;height:18px;color:var(--cyan)}
.cat .sz{font-family:var(--mono);font-size:22px;margin-top:10px}
.cat .ct{color:var(--muted);font-size:12px}
.cat input{position:absolute;right:12px;top:14px}
.items{max-height:340px;overflow:auto;border:1px solid var(--line);border-radius:12px}
.item{display:flex;gap:10px;align-items:center;padding:7px 12px;border-bottom:1px solid var(--line);font-size:12.5px}
.item .p{flex:1;font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item .s{font-family:var(--mono);color:var(--muted)}
/* startup list */
.sl{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
.si{display:flex;gap:12px;align-items:center;padding:13px 14px;border:1px solid var(--line);border-radius:13px;background:var(--inset)}
.si .av{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;font-weight:700;color:#fff;background:linear-gradient(135deg,var(--blue),var(--cyan));flex:none}
.si .tx{flex:1;min-width:0}.si .tx b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.si .tx small{color:var(--muted);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono);font-size:11.5px}
/* disks */
.dlist{display:flex;flex-direction:column;gap:10px}
.dcard{display:flex;gap:12px;align-items:center;padding:12px;border:1px solid var(--line);border-radius:13px;background:var(--inset);cursor:pointer;transition:.15s}
.dcard:hover{border-color:var(--line2)}
.dcard.on{border-color:var(--line2);box-shadow:0 0 0 1px var(--line2),0 0 22px rgba(34,211,238,calc(.15*var(--glow)));background:linear-gradient(90deg,rgba(59,130,246,.16),transparent)}
.dcard svg.di{width:34px;height:34px;color:var(--cyan);flex:none}
.dcard .tx{flex:1;min-width:0}.dcard b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dcard small{color:var(--muted)}
.layout{display:flex;height:92px;border-radius:12px;overflow:hidden;border:1px solid var(--line);background:var(--inset);gap:2px;padding:2px}
.vol{position:relative;min-width:26px;border-radius:9px;cursor:pointer;padding:9px 10px;overflow:hidden;font-size:12px;transition:.15s;border:1px solid transparent;display:flex;flex-direction:column;justify-content:space-between}
.vol b{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12.5px}
.vol small{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:.85}
.vol .use{position:absolute;left:0;bottom:0;height:4px;background:rgba(255,255,255,.75)}
.vol.sel{border-color:#fff;box-shadow:0 0 0 2px var(--cyan),0 0 18px var(--cyan)}
.vol.free{background:repeating-linear-gradient(135deg,var(--track),var(--track) 8px,transparent 8px,transparent 16px);color:var(--muted)}
.toolbar{display:flex;flex-wrap:wrap;gap:8px}
.health{display:flex;gap:16px;align-items:center}
/* speed */
.speedo{position:relative;max-width:460px;margin:auto}
.speedo svg{width:100%;height:auto;display:block;overflow:visible}
.speedo .read{position:absolute;left:0;right:0;top:66%;text-align:center}
.speedo .read .n{font-family:var(--mono);font-size:46px;font-weight:600;line-height:1;text-shadow:0 0 calc(24px*var(--glow)) var(--sc,var(--cyan))}
.speedo .read .u{color:var(--muted);letter-spacing:2px;font-size:12px;margin-top:6px;font-weight:600}
.speedo .read .ph{display:inline-flex;gap:6px;align-items:center;margin-top:10px;font-size:12px;letter-spacing:1.5px;color:var(--sc,var(--cyan));font-weight:700}
.needle{transform-box:view-box;transform-origin:200px 200px;transition:transform .35s cubic-bezier(.2,.8,.2,1)}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.metric{border:1px solid var(--line);border-radius:13px;padding:12px 14px;background:var(--inset)}
.metric .k{font-size:11px;color:var(--muted);letter-spacing:1.5px;font-weight:600;display:flex;gap:6px;align-items:center}
.metric .k svg{width:14px;height:14px}
.metric .v{font-family:var(--mono);font-size:24px;margin-top:6px}.metric .v small{font-size:12px;color:var(--muted);margin-left:4px}
.srvlist{max-height:430px;overflow:auto;border:1px solid var(--line);border-radius:12px}
.srv{display:flex;gap:10px;align-items:center;padding:9px 12px;border-bottom:1px solid var(--line);cursor:pointer;font-size:13px}
.srv:hover{background:rgba(59,130,246,.07)}
.srv.on{background:linear-gradient(90deg,rgba(34,211,238,.18),transparent);box-shadow:inset 3px 0 0 var(--cyan)}
.srv>svg{width:18px;height:18px;color:var(--cyan);flex:none}.srv .nm{flex:1;min-width:0}.srv .nm b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:550}
.srv .nm small{color:var(--muted)}
.srv .pg{font-family:var(--mono);font-size:12.5px;min-width:64px;text-align:right}
/* modal + toast */
.modal-bg{position:fixed;inset:0;background:rgba(2,6,15,.62);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;z-index:50;padding:20px}
.modal-bg.on{display:flex}
.modal{width:min(760px,100%);max-height:90vh;overflow:auto;background:var(--card2);border:1px solid var(--line2);border-radius:18px;box-shadow:0 30px 80px rgba(0,0,0,.55),0 0 40px rgba(34,211,238,calc(.12*var(--glow)));padding:22px}
:root[data-theme="dark"] .modal{background:#0c1629}
.modal h2{margin:0 0 14px;font-size:17px;display:flex;gap:10px;align-items:center}
.modal .acts{display:flex;justify-content:flex-end;gap:10px;margin-top:18px}
.form{display:grid;grid-template-columns:150px 1fr;gap:10px 14px;align-items:center}
.form label{color:var(--muted);font-size:13px}
.warnbox{overflow-wrap:anywhere;border:1px solid rgba(251,79,110,.5);background:rgba(251,79,110,.08);border-radius:12px;padding:11px 13px;font-size:13px;margin:10px 0}
.infobox{border:1px solid var(--line2);background:rgba(34,211,238,.06);border-radius:12px;padding:11px 13px;font-size:13px;margin:10px 0}
.toasts{position:fixed;right:20px;bottom:20px;display:flex;flex-direction:column;gap:10px;z-index:60}
.toast{min-width:280px;max-width:440px;padding:12px 15px;border-radius:12px;background:var(--card2);border:1px solid var(--line2);box-shadow:var(--shadow);font-size:13px;animation:fade .2s}
:root[data-theme="dark"] .toast{background:#0f1b33}
.toast.err{border-color:rgba(251,79,110,.6)}.toast.ok{border-color:rgba(30,227,154,.55)}
pre.out{background:var(--inset);border:1px solid var(--line);border-radius:10px;padding:10px;max-height:240px;overflow:auto;font-size:12px;white-space:pre-wrap}
.help h2{font-size:16px;margin:22px 0 8px;color:var(--cyan)}.help h2:first-child{margin-top:0}
.help p,.help li{color:var(--muted);line-height:1.6}.help code{font-family:var(--mono);background:var(--inset);padding:1px 6px;border-radius:6px;color:var(--text)}
.help li b{color:var(--text)}
.empty{padding:30px;text-align:center;color:var(--muted)}
.spin{width:16px;height:16px;border:2px solid var(--track2);border-top-color:var(--cyan);border-radius:50%;animation:sp 0.8s linear infinite;display:inline-block}
@keyframes sp{to{transform:rotate(360deg)}}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:var(--track2);border-radius:10px;border:2px solid transparent;background-clip:padding-box}
.grp-card{border:1px solid var(--line);border-radius:12px;margin-bottom:10px;overflow:hidden}
.grp-hd{display:flex;gap:10px;align-items:center;padding:9px 12px;background:var(--inset);cursor:pointer;font-size:13px}
.grp-bd .item:last-child{border-bottom:0}.grp-hd>svg{width:16px;height:16px;color:var(--cyan);flex:none}.status .hd>svg,.toolbar>span>svg{width:15px;height:15px}
.agrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}.agrp{border:1px solid var(--line);border-radius:12px;padding:10px 12px;background:var(--inset)}.agt{font-size:10.5px;letter-spacing:1.6px;text-transform:uppercase;color:var(--muted);font-weight:700;margin-bottom:8px}.btn:disabled{filter:grayscale(.6)}mark{background:rgba(250,204,21,.35);color:inherit;border-radius:3px;padding:0 1px}.dkp .tbl-wrap{max-height:calc(100vh - 470px);min-height:160px}.rrow{display:grid;grid-template-columns:110px 1fr;gap:10px;align-items:center;margin:6px 0;font-size:12px;color:var(--muted)}
.rtrack{position:relative;height:30px;border-radius:8px;background:var(--inset);border:1px solid var(--line);overflow:hidden}
.rseg{position:absolute;top:2px;bottom:2px;border-radius:6px;font-size:11px;color:#fff;padding:6px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:4px}
.rseg.t{background:linear-gradient(160deg,#2563eb,#1e40af)}.rseg.lost{background:linear-gradient(160deg,#e11d48,#9f1239);box-shadow:0 0 12px rgba(251,79,110,calc(.5*var(--glow)))}.rseg.in-table{background:linear-gradient(160deg,#059669,#065f46)}.rseg.overlap{background:linear-gradient(160deg,#d97706,#92400e)}
tr.dimrow td{opacity:.55}
.ctype{display:flex;gap:9px;align-items:center;padding:9px 11px;border:1px solid var(--line);border-radius:11px;background:var(--inset);cursor:pointer;font-size:13px}
.ctype svg{width:16px;height:16px;color:var(--cyan)}.ctype b{margin-left:auto;color:var(--green)}
.ctypes{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px}
.checks{display:grid;gap:8px}.chk-row{display:flex;gap:12px;align-items:flex-start;padding:11px 13px;border-radius:12px;border:1px solid var(--line);background:var(--inset)}
.chk-row>svg{width:20px;height:20px;flex:none;margin-top:2px}.chk-row.g>svg{color:var(--green)}.chk-row.a>svg{color:var(--amber)}.chk-row.r>svg{color:var(--red)}.chk-row.r{border-color:rgba(251,79,110,.45)}
.bigbar{height:14px;border-radius:999px;background:var(--track);overflow:hidden;margin-top:12px}.bigbar>i{display:block;height:100%;width:0;border-radius:999px;background:linear-gradient(90deg,var(--blue),var(--cyan),var(--green));box-shadow:0 0 calc(16px*var(--glow)) rgba(34,211,238,.7);transition:width .4s;background-size:200% 100%;animation:barflow 2s linear infinite}
@keyframes barflow{to{background-position:-200% 0}}
.dbadges{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}
.badge svg{width:11px;height:11px;flex:none}
button.badge{cursor:pointer;font:inherit;font-size:11.5px;font-weight:700;background:rgba(251,79,110,.12)}button.badge:hover{box-shadow:0 0 0 1px var(--red),0 0 12px rgba(251,79,110,.5)}
.dcard{align-items:flex-start}.dcard svg.di{margin-top:3px}
.opcard{display:flex;gap:14px;align-items:center;padding:11px 13px;border:1px solid var(--line);border-radius:13px;background:var(--inset);margin-bottom:9px}
.opcard.running{border-color:rgba(34,211,238,.45);box-shadow:0 0 18px rgba(34,211,238,calc(.12*var(--glow)))}.opcard.error{border-color:rgba(251,79,110,.5)}.opcard.done{opacity:.9}
.opcard.big{padding:16px;margin:0}.opmain{flex:1;min-width:0}
.opstats{font-size:12px;margin-top:2px;font-variant-numeric:tabular-nums}.opmsg{font-size:12.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1}
.opbar{height:8px;border-radius:999px;background:var(--track);overflow:hidden;margin:8px 0 6px;position:relative}
.opbar>i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--blue),var(--cyan),var(--green));transition:width .35s;box-shadow:0 0 calc(10px*var(--glow)) rgba(34,211,238,.6)}
.opbar.indet>i{background:repeating-linear-gradient(115deg,rgba(34,211,238,.85) 0 14px,rgba(59,130,246,.35) 14px 28px);background-size:60px 100%;animation:indet 0.9s linear infinite}
.rcload{display:flex;gap:12px;align-items:flex-start;padding:10px 12px;border:1px solid var(--line,rgba(34,211,238,.25));border-radius:12px;background:rgba(34,211,238,.05)}.rcload .opbar{margin:6px 0 4px}.rcload.err{border-color:rgba(239,68,68,.5);background:rgba(239,68,68,.06)}.rcload.err>svg{color:var(--red);flex:none;margin-top:2px;width:20px;height:20px}.rcok a{color:var(--cyan)}.rcok svg{width:14px;height:14px;vertical-align:-2px;color:var(--green)}
.modal.wide{width:min(1180px,100%)}.modal h2>svg{width:22px;height:22px;vertical-align:-4px;margin-right:6px;color:var(--cyan)}
.dkxgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:14px}
.dkxcard{border:1px solid var(--line);border-radius:14px;padding:14px;background:var(--inset)}
.dkxhd{display:flex;gap:12px;align-items:flex-start;margin-bottom:10px}
.dkxl{flex:none;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:16px;color:#04121f;background:linear-gradient(135deg,var(--cyan),var(--blue));box-shadow:0 0 calc(12px*var(--glow)) rgba(34,211,238,.45)}
.dkxcmd{display:flex;align-items:center;gap:6px;margin-top:8px;background:var(--card2);border:1px solid var(--line);border-radius:8px;padding:2px 2px 2px 10px}
.dkxcmd code{flex:1;font-family:var(--mono,ui-monospace,monospace);font-size:12.5px;white-space:pre-wrap;word-break:break-all;color:var(--cyan)}
.dkxcmd .btn.ico{width:28px;height:28px}.dkxcmd svg,.dkxurl svg{width:15px;height:15px}
.dkxexp{font-size:12px;color:var(--muted);margin:3px 2px 0}
.dkxsplit{display:grid;grid-template-columns:270px 1fr;gap:16px;margin-top:12px}
.dkxlist{display:flex;flex-direction:column;gap:6px}
.dkxlist button{text-align:left;background:var(--inset);border:1px solid var(--line);border-radius:10px;padding:9px 11px;color:var(--text);cursor:pointer;display:flex;flex-direction:column;gap:2px}
.dkxlist button span{font-size:11.5px;color:var(--muted)}.dkxlist button.on{border-color:var(--cyan);box-shadow:0 0 0 1px var(--cyan) inset}
.dkxft{display:flex;gap:4px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:6px}
.dkxft>button:not(.btn){background:none;border:1px solid transparent;border-radius:8px;padding:5px 9px;color:var(--muted);cursor:pointer;font-family:var(--mono,ui-monospace,monospace);font-size:12px}
.dkxft>button.on{color:var(--cyan);border-color:var(--line2);background:var(--inset)}
.dkxpre{background:var(--inset);border:1px solid var(--line);border-radius:10px;padding:12px;margin-top:8px;max-height:330px;overflow:auto;font-size:12.5px;white-space:pre;font-family:var(--mono,ui-monospace,monospace)}
.dkxform{display:grid;grid-template-columns:auto 1fr;gap:8px 12px;align-items:center;margin-top:12px}.dkxform label{margin:0}
.dkxg{margin:18px 0 8px;font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.dkxlinks{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:10px}
.dkxlink{border:1px solid var(--line);border-radius:12px;padding:11px 12px;background:var(--inset)}
.dkxlink a{color:var(--cyan);text-decoration:none;margin-right:8px}.dkxlink a:hover{text-decoration:underline}
.dkxurl{display:flex;align-items:center;gap:6px;margin-top:6px;font-size:11.5px;color:var(--muted)}.dkxurl .mono{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.dkxurl .btn.ico{width:26px;height:26px}
@media(max-width:900px){.dkxsplit{grid-template-columns:1fr}.dkxgrid{grid-template-columns:1fr}}
@keyframes indet{to{background-position:60px 0}}
.opbar.error>i{background:var(--red);box-shadow:none}.opbar.done>i{background:linear-gradient(90deg,#0d9e6c,var(--green))}
.opsteps{display:flex;flex-wrap:wrap;align-items:center;gap:4px;margin-top:7px;font-size:11.5px}
.opstep{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
.opstep.ok{color:var(--green);border-color:rgba(30,227,154,.4)}.opstep.cur{color:var(--cyan);border-color:var(--line2);background:rgba(34,211,238,.08)}.opstep.bad{color:var(--red);border-color:rgba(251,79,110,.5)}.opstep.sk{text-decoration:line-through}
.oparrow{color:var(--dim)}.opspin{animation:sp 1s linear infinite}.opspark{flex:none}
.opspill{color:var(--cyan);border-color:var(--line2);box-shadow:0 0 14px rgba(34,211,238,calc(.25*var(--glow)))}
.vol.volbad{box-shadow:inset 0 0 0 2px var(--red)}.vol.volbusy:after{content:"";position:absolute;inset:0;background:repeating-linear-gradient(115deg,rgba(255,255,255,.18) 0 10px,transparent 10px 20px);background-size:40px 100%;animation:indet .9s linear infinite;pointer-events:none}
.kbd{font-family:var(--mono);font-size:11px;border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:1px 5px}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">
      <svg viewBox="0 0 128 128"><defs><linearGradient id="bg1" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3b82f6"/><stop offset="1" stop-color="#22d3ee"/></linearGradient></defs><rect width="128" height="128" rx="28" fill="#0b1426"/><circle cx="64" cy="64" r="40" fill="none" stroke="#1e2a44" stroke-width="10"/><path d="M64 24a40 40 0 1 1-34.6 60" fill="none" stroke="url(#bg1)" stroke-width="10" stroke-linecap="round"/><path d="M64 64 86 46" stroke="#22e39a" stroke-width="7" stroke-linecap="round"/><circle cx="64" cy="64" r="7" fill="#22e39a"/></svg>
      <div><b>MySysPilot</b><small>LINUX COMMAND CENTER</small></div>
    </div>
    <nav class="nav" id="nav"></nav>
    <div class="side-foot"><span class="dot" id="connDot"></span><span id="connTxt">Connecting…</span></div>
  </aside>
  <main class="main">
    <header class="top">
      <div><h1 id="pgTitle">Dashboard</h1><div class="sub" id="pgSub"></div></div>
      <div class="sp"></div>
      <span class="pill opspill" id="opsPill" style="display:none;cursor:pointer" title="Show operations"></span><span class="pill" id="hostPill">—</span>
      <span class="pill" id="clockPill">--:--</span>
      <button class="btn ghost ico" id="themeBtn" title="Toggle dark / light (T)"></button>
    </header>
    <div class="content" id="content">

<!-- DASHBOARD -->
<section class="page" id="p-dashboard">
  <div class="grid g12">
    <div class="card glow c4"><h3><span data-ic="cpu"></span>CPU Load<span class="r muted" id="cpuModelS"></span></h3><div id="gCpu"></div></div>
    <div class="card glow c4"><h3><span data-ic="clock"></span>System Uptime</h3>
      <div id="dpUptime" style="margin-top:6px"></div>
      <div style="margin-top:18px" class="row sb"><span class="muted">Load average (1m vs cores)</span><span class="mono" id="loadTxt">—</span></div>
      <div class="bar" style="margin-top:8px"><i id="loadBar" style="width:0"></i></div>
      <div class="row sb" style="margin-top:14px"><span class="muted">Processes</span><span class="mono" id="procTxt">—</span></div>
      <div class="row sb" style="margin-top:8px"><span class="muted">CPU temperature</span><span class="mono" id="tempTxt">—</span></div>
    </div>
    <div class="card glow c4"><h3><span data-ic="mem"></span>Memory</h3><div id="gMem"></div></div>

    <div class="card c6">
      <div class="lb"><div class="ic" data-ic="disk"></div><div><div class="t">Root filesystem</div><div class="muted" id="rootSub" style="font-size:12.5px">—</div></div><div class="p" id="rootPct">—</div></div>
      <div class="bar" style="margin-top:14px"><i id="rootBar" style="width:0"></i></div>
      <div class="lb" style="margin-top:20px"><div class="ic" data-ic="swap"></div><div><div class="t">Swap</div><div class="muted" id="swapSub" style="font-size:12.5px">—</div></div><div class="p" id="swapPct">—</div></div>
      <div class="bar vio" style="margin-top:14px"><i id="swapBar" style="width:0"></i></div>
      <div class="lb" style="margin-top:20px"><div class="ic" data-ic="mem"></div><div><div class="t">Page cache &amp; buffers</div><div class="muted" id="cacheSub" style="font-size:12.5px">—</div></div><div class="p" id="cachePct">—</div></div>
      <div class="bar" style="margin-top:14px"><i id="cacheBar" style="width:0;background:linear-gradient(90deg,#0d9e6c,var(--green))"></i></div>
    </div>
    <div class="c6 grid" style="gap:18px">
      <div class="hud" id="hudDown"></div>
      <div class="hud" id="hudUp"></div>
    </div>

    <div class="card c5"><h3><span data-ic="cpu"></span>Cores<span class="r muted" id="coreCount"></span></h3><div class="minis" id="minis"></div></div>
    <div class="card glow c3"><h3><span data-ic="io"></span>Disk I/O</h3><div id="gIo"></div></div>
    <div class="card c4 status" id="statusCard">
      <div class="hd"><span class="dot"></span>RUNNING</div>
      <div class="ln"><span data-ic="host"></span>Host<b id="sHost">—</b></div>
      <div class="ln"><span data-ic="linux"></span>OS<b id="sOs">—</b></div>
      <div class="ln"><span data-ic="chip"></span>Kernel<b id="sKernel">—</b></div>
      <div class="ln"><span data-ic="desk"></span>Desktop<b id="sDesk">—</b></div>
      <div class="ln"><span data-ic="bolt"></span>Frequency<b id="sFreq">—</b></div>
      <div class="ln" style="border:0"><span data-ic="net"></span>Data since boot<b id="sNetTot">—</b></div>
      <div class="bar sm" style="margin-top:8px"><i id="sBar" style="width:0;background:linear-gradient(90deg,#0d9e6c,var(--green))"></i></div>
    </div>
  </div>
</section>

<!-- RESOURCES -->
<section class="page" id="p-resources">
  <div class="grid g12">
    <div class="card c12"><h3><span data-ic="cpu"></span>CPU history<span class="r legend" id="lgCpu"></span></h3><canvas class="chart" id="chCpu"></canvas></div>
    <div class="card c6"><h3><span data-ic="mem"></span>Memory &amp; swap<span class="r legend"><span><i style="background:var(--green)"></i>RAM %</span><span><i style="background:var(--violet)"></i>Swap %</span></span></h3><canvas class="chart" id="chMem"></canvas></div>
    <div class="card c6"><h3><span data-ic="net"></span>Network<span class="r legend"><span><i style="background:var(--cyan)"></i>Download</span><span><i style="background:var(--violet)"></i>Upload</span></span></h3><canvas class="chart" id="chNet"></canvas></div>
    <div class="card c6"><h3><span data-ic="io"></span>Disk I/O<span class="r legend"><span><i style="background:var(--blue)"></i>Read</span><span><i style="background:var(--amber)"></i>Write</span></span></h3><canvas class="chart" id="chIo"></canvas></div>
    <div class="card c6"><h3><span data-ic="temp"></span>Sensors</h3><div id="sensors" class="minis" style="grid-template-columns:repeat(auto-fill,minmax(150px,1fr))"></div></div>
  </div>
</section>

<!-- PROCESSES -->
<section class="page" id="p-processes">
  <div class="row" style="margin-bottom:14px">
    <div class="search"><span data-ic="search"></span><input type="text" id="prSearch" placeholder="Filter by name, user, PID, command"></div>
    <label class="chk"><input type="checkbox" id="prMine"> Only my processes</label>
    <span class="spacer"></span>
    <span class="muted" id="prCount"></span>
    <button class="btn sm" data-sig="TERM" disabled>End (TERM)</button>
    <button class="btn sm danger" data-sig="KILL" disabled>Kill</button>
    <button class="btn sm ghost" data-sig="STOP" disabled>Pause</button>
    <button class="btn sm ghost" data-sig="CONT" disabled>Resume</button>
    <button class="btn sm ghost" id="prNice" disabled>Priority…</button>
  </div>
  <div class="tbl-wrap"><table class="t" id="prTbl"><thead><tr>
    <th data-k="pid">PID</th><th data-k="name">Name</th><th data-k="user">User</th><th data-k="state">State</th><th data-k="cpu">CPU %</th><th data-k="rss">Memory</th><th data-k="threads">Thr</th><th data-k="nice">Nice</th><th data-k="cmd">Command</th>
  </tr></thead><tbody></tbody></table></div>
</section>

<!-- SERVICES -->
<section class="page" id="p-services">
  <div class="row" style="margin-bottom:14px">
    <div class="search"><span data-ic="search"></span><input type="text" id="svSearch" placeholder="Filter services"></div>
    <div class="tabs" id="svTabs"><button data-f="running" class="on">Running</button><button data-f="enabled">Enabled</button><button data-f="failed">Failed</button><button data-f="all">All</button></div>
    <span class="spacer"></span><span class="muted" id="svCount"></span>
    <button class="btn sm ghost" id="svReload"><span data-ic="refresh"></span>Refresh</button>
  </div>
  <div class="tbl-wrap"><table class="t" id="svTbl"><thead><tr><th>Service</th><th>Description</th><th>State</th><th>Boot</th><th style="text-align:right">Actions</th></tr></thead><tbody></tbody></table></div>
</section>

<!-- STARTUP -->
<section class="page" id="p-startup">
  <div class="row" style="margin-bottom:14px">
    <span class="muted">Applications launched when you log in (XDG autostart). Changes are written to your user autostart folder only.</span>
    <span class="spacer"></span>
    <label class="chk"><input type="checkbox" id="suAll"> Show entries for other desktops</label>
    <button class="btn pri sm" id="suAdd"><span data-ic="plus"></span>Add application</button>
  </div>
  <div class="sl" id="suList"></div>
</section>

<!-- CLEANER -->
<section class="page" id="p-cleaner">
  <div class="grid g12">
    <div class="card glow c4 center" style="flex-direction:column"><div id="gClean" style="width:100%"></div>
      <div class="row" style="margin-top:12px;justify-content:center"><button class="btn pri" id="clScan"><span data-ic="search"></span>Scan system</button><button class="btn ok" id="clClean" disabled><span data-ic="broom"></span>Clean selected</button></div></div>
    <div class="card c8"><h3><span data-ic="broom"></span>Categories<span class="r muted" id="clSel"></span></h3><div class="cats" id="clCats"></div>
      <div class="infobox" style="margin-top:14px">Application caches are rebuilt automatically but apps may start slower once. System items (package cache, rotated logs, journal, old snap revisions) are removed in one polkit-authorized step.</div></div>
    <div class="card c12"><h3><span data-ic="list"></span>Items<span class="r"><label class="chk"><input type="checkbox" id="clAllItems"> select all in view</label></span></h3><div class="items" id="clItems"><div class="empty">Run a scan to see cleanable items.</div></div></div>
  </div>
</section>

<!-- UNINSTALLER -->
<section class="page" id="p-packages">
  <div class="row" style="margin-bottom:14px">
    <div class="tabs" id="pkTabs"><button data-k="apt" class="on">APT / deb</button><button data-k="snap">Snap</button><button data-k="flatpak">Flatpak</button></div>
    <div class="search"><span data-ic="search"></span><input type="text" id="pkSearch" placeholder="Search packages"></div>
    <label class="chk" id="pkManualL"><input type="checkbox" id="pkManual" checked> Manually installed only</label>
    <span class="spacer"></span><span class="muted" id="pkCount"></span>
    <button class="btn sm ghost" id="pkAuto">Autoremove unused</button>
    <button class="btn sm danger" id="pkRemove" disabled><span data-ic="trash"></span>Remove selected</button>
  </div>
  <div class="tbl-wrap"><table class="t" id="pkTbl"><thead><tr><th style="width:34px"><input type="checkbox" id="pkAll"></th><th data-k="name">Package</th><th data-k="version">Version</th><th data-k="size">Size</th><th data-k="summary">Description</th></tr></thead><tbody></tbody></table></div>
</section>

<!-- REPOS -->
<section class="page" id="p-repos">
  <div class="row" style="margin-bottom:14px"><span class="muted">APT sources from <code class="mono">/etc/apt/sources.list</code> and <code class="mono">sources.list.d</code> (.list and deb822 .sources). Toggling makes a timestamped backup in <code class="mono">/var/backups/mysyspilot/</code>. Run <code class="mono">sudo apt update</code> afterwards.</span><span class="spacer"></span><button class="btn sm ghost" id="rpReload"><span data-ic="refresh"></span>Refresh</button></div>
  <div class="tbl-wrap"><table class="t" id="rpTbl"><thead><tr><th>On</th><th>Type</th><th>URI</th><th>Suite</th><th>Components</th><th>File</th></tr></thead><tbody></tbody></table></div>
</section>

<!-- DISKS -->
<section class="page" id="p-disks">
  <div class="grid g12">
    <div class="c4">
      <div class="row" style="margin-bottom:12px"><button class="btn sm ghost" id="dkReload"><span data-ic="refresh"></span>Refresh</button><span id="dkFresh" class="muted" style="font-size:11.5px;display:inline-flex;gap:6px;align-items:center"></span><button class="btn sm ghost" id="dkAttach"><span data-ic="image"></span>Attach image…</button><label class="chk" style="margin-left:auto"><input type="checkbox" id="dkSnap"> snap loops</label></div>
      <div class="dlist" id="dkList"><div class="empty"><span class="spin"></span></div></div>
    </div>
    <div class="c8 grid" style="gap:18px;align-content:start"><div class="card glow" id="dkOps"></div><div class="grid" style="gap:18px" id="dkDetail"><div class="card empty">Select a drive.</div></div></div>
    <div class="card c12"><h3><span data-ic="save"></span>Backups made with MySysPilot<span class="r"><button class="btn sm ghost" id="bkReload"><span data-ic="refresh"></span>Refresh</button></span></h3>
      <div class="tbl-wrap" style="max-height:340px"><table class="t" id="bkTbl"><thead><tr><th>Date</th><th>Type</th><th>Source</th><th>File</th><th>Size</th><th>Compression</th><th>Checksum</th><th style="text-align:right">Actions</th></tr></thead><tbody></tbody></table></div></div>
  </div>
</section>

<!-- DUPLICATES / SEARCH -->
<section class="page" id="p-dupes">
  <div class="grid g12">
    <div class="card c8">
      <div class="row sb" style="margin-bottom:14px"><div class="tabs" id="duTabs"><button data-m="dup" class="on">Duplicate files</button><button data-m="of">Copies of a file</button><button data-m="search">Search files &amp; text</button></div></div>
      <div class="form">
        <label data-mode="of">Reference file</label><input data-mode="of" type="text" id="duRef" placeholder="~/Pictures/IMG_0001.jpg — find every identical copy of this file">
        <label data-mode="search">File name</label><div data-mode="search" class="row"><input type="text" id="srName" placeholder="report  ·  *.pdf, *.docx  ·  ^IMG_\d+" style="flex:1"><select id="srNameMode"><option value="contains">contains</option><option value="glob">wildcard (*.ext)</option><option value="regex">regex</option></select></div>
        <label data-mode="search">Containing text</label><div data-mode="search" class="row"><input type="text" id="srText" placeholder="text inside files (optional)" style="flex:1"><label class="chk"><input type="checkbox" id="srCase"> Match case</label><label class="chk"><input type="checkbox" id="srRegex"> Regex</label></div>
        <label>Search in folders</label><textarea id="duRoots" rows="2" placeholder="one folder per line"></textarea>
        <label data-mode="dup">Only names matching</label><input data-mode="dup" type="text" id="duInc" placeholder="optional, e.g. *.jpg, *.png, *.mp4">
        <label data-mode="dup">File size</label><div data-mode="dup" class="row"><span class="muted">min</span><input type="number" id="duMin" value="64" min="1" style="width:100px"><select id="duMinU"><option value="1">bytes</option><option value="1024" selected>KiB</option><option value="1048576">MiB</option></select><span class="muted">max</span><input type="number" id="duMax" value="0" min="0" style="width:100px"><span class="muted">MiB (0 = none)</span></div>
        <label data-mode="search">Filters</label><div data-mode="search" class="row"><span class="muted">size ≥</span><input type="number" id="srMin" value="0" min="0" style="width:90px"><span class="muted">KiB ≤</span><input type="number" id="srMax" value="0" min="0" style="width:90px"><span class="muted">MiB · modified in last</span><input type="number" id="srDays" value="0" min="0" style="width:80px"><span class="muted">days (0 = any)</span></div>
        <label data-mode="search">Limits</label><div data-mode="search" class="row"><span class="muted">max results</span><input type="number" id="srMaxRes" value="2000" min="1" style="width:100px"><span class="muted">skip text search in files over</span><input type="number" id="srMaxFile" value="50" min="1" style="width:80px"><span class="muted">MiB</span></div>
        <label>Exclude</label><input type="text" id="duEx" value="node_modules, .git, __pycache__, *.tmp, .cache">
        <label>Options</label><div class="row"><label class="chk"><input type="checkbox" id="duHidden"> Include hidden files/folders</label></div>
      </div>
      <div class="row" style="margin-top:16px"><button class="btn pri" id="duStart"><span data-ic="search"></span>Find duplicates</button><button class="btn ghost" id="duCancel" disabled>Cancel</button><span class="muted" id="duHint" style="font-size:12.5px"></span></div>
    </div>
    <div class="card glow c4"><div id="gDupes"></div><div class="row sb" style="margin-top:8px"><span class="muted">Files scanned</span><span class="mono" id="duFiles">0</span></div><div class="row sb" style="margin-top:6px"><span class="muted" id="duL2">Duplicate groups</span><span class="mono" id="duGroups">0</span></div><div class="row sb" style="margin-top:6px"><span class="muted" id="duL3">Reclaimable</span><span class="mono" id="duWaste">0 B</span></div></div>
    <div class="card c12"><h3><span data-ic="list"></span>Results<span class="r row" style="gap:8px">
      <div class="search"><span data-ic="search"></span><input type="text" id="duFilter" placeholder="Filter results" style="width:200px"></div>
      <select id="duAuto"><option value="">Auto-select…</option><option value="newest">Keep newest in each group</option><option value="oldest">Keep oldest in each group</option><option value="shortest">Keep shortest path</option><option value="none">Clear selection</option></select>
      <select id="duMode"><option value="trash">Move to Trash</option><option value="delete">Delete permanently</option></select>
      <button class="btn sm danger" id="duDelete" disabled><span data-ic="trash"></span>Remove selected</button></span></h3>
      <div id="duRes"><div class="empty">No scan yet.</div></div></div>
  </div>
</section>

<!-- DATA RECOVERY -->
<section class="page" id="p-recovery">
  <div class="grid g12" style="margin-bottom:18px">
    <div class="card c8"><h3><span data-ic="disk"></span>Device or image<span class="r"><button class="btn sm ghost" id="rcReload"><span data-ic="refresh"></span>Refresh</button></span></h3>
      <div class="row"><select id="rcDev" style="flex:1;min-width:280px"></select></div>
      <div class="row" id="rcImgRow" style="display:none;margin-top:8px"><input type="text" id="rcImg" placeholder="~/Backups/failing-disk.img" style="flex:1"><button class="btn sm pri" id="rcImgGo">Use image</button></div>
      <div id="rcInfo" style="margin-top:10px;font-size:13px"></div>
      <div class="warnbox" style="margin-top:12px;font-size:12.5px"><b>Recovery rules:</b> stop writing to the affected drive · save recovered files to a <i>different</i> drive · if the drive is failing (clicking, read errors, SMART warnings), make an image first with Disk Utility → Back up disk, then select the image here.</div></div>
    <div class="card glow c4"><div class="row sb"><b id="rcTitle" class="muted" style="font-size:12px;letter-spacing:1.5px;text-transform:uppercase">Idle</b><button class="btn sm ghost" id="rcCancel" disabled>Cancel</button></div>
      <div id="gRec" style="max-width:190px;margin:4px auto 0"></div>
      <div class="bigbar"><i id="rcBar"></i></div>
      <div style="font-size:12.5px;margin-top:8px" id="rcS1">Ready</div><div class="row sb muted mono" style="font-size:12px;margin-top:4px"><span id="rcS2"></span><span id="rcS3"></span></div></div>
  </div>
  <div class="row" style="margin-bottom:14px"><div class="tabs" id="rcTabs"><button data-t="part" class="on">Partition recovery</button><button data-t="boot">Boot &amp; superblock repair</button><button data-t="undel">Undelete files</button><button data-t="carve">File carving (PhotoRec)</button><button data-t="undo">Undo log</button><button data-t="cli">TestDisk / PhotoRec</button></div></div>
  <div class="rcp card" data-t="part">
    <div class="row"><button class="btn pri" id="rcQuick"><span data-ic="search"></span>Quick Search</button><button class="btn" id="rcDeep"><span data-ic="search"></span>Deep Search</button><span class="muted" style="font-size:12.5px">Quick checks 1 MiB and legacy cylinder boundaries (seconds). Deep reads every sector and also finds filesystems through backup boot sectors and backup superblocks.</span></div>
    <div id="rcMap" style="margin-top:14px"></div>
    <div id="rcFound" style="margin-top:12px"><div class="empty">Select a device, then run a search.</div></div>
    <div class="agrp" style="margin-top:14px"><div class="agt">Write recovered partitions</div><div class="row"><select id="rcWMode"><option value="new">Write a new partition table (replace the damaged one)</option><option value="append">Add to the existing partition table</option></select><select id="rcWLabel"><option value="gpt">GPT</option><option value="dos">MBR (DOS)</option></select><button class="btn danger" id="rcWrite" disabled>Write…</button><span class="dim" id="rcWriteNote" style="font-size:12px"></span></div>
      <div class="dim" style="font-size:12px;margin-top:6px">Tick the partitions to keep. Only table entries are written (sfdisk, no signature wiping); the current table is backed up first.</div></div>
    <div id="rcMounts"></div>
  </div>
  <div class="rcp card" data-t="boot" style="display:none">
    <div class="row"><select id="rbTarget" style="flex:1"></select><button class="btn pri" id="rbGo"><span data-ic="shield"></span>Analyze</button></div>
    <div class="dim" style="font-size:12px;margin:8px 0 14px">NTFS: boot sector vs backup, $MFT vs $MFTMirr · FAT12/16/32: boot sector vs backup, FAT1 vs FAT2 · exFAT: main vs backup boot region (checksums) · ext2/3/4: primary and backup superblocks · XFS / Btrfs: native tools.</div>
    <div id="rbOut"></div>
  </div>
  <div class="rcp card" data-t="undel" style="display:none">
    <div class="row"><select id="ruTarget" style="flex:1"></select><button class="btn pri" id="ruGo"><span data-ic="search"></span>Scan for deleted files</button></div>
    <div class="dim" style="font-size:12px;margin:8px 0">Supported: NTFS (MFT records) and FAT12/16/32 (directory entries, long names). ext3/ext4 clear block pointers on delete — use File carving for those, and for exFAT.</div>
    <div class="row" style="margin:10px 0"><span id="ruSum"></span><span class="spacer"></span><div class="search"><span data-ic="search"></span><input type="text" id="ruFilter" placeholder="Filter by name or folder"></div><select id="ruStatus"><option value="all">All states</option><option value="good">Recoverable</option><option value="partial">Partially overwritten</option><option value="bad">Overwritten / empty</option></select></div>
    <div id="ruTbl"></div>
    <div class="row" style="margin-top:12px"><span class="muted">Save to</span><input type="text" id="ruOut" style="flex:1"><button class="btn pri" id="ruRecover" disabled>Recover selected</button></div>
  </div>
  <div class="rcp card" data-t="carve" style="display:none">
    <div class="row"><select id="rcTarget" style="flex:1"></select><select id="rcAlign" title="Scan granularity"><option value="512">Every 512-byte sector (thorough)</option><option value="4096">Every 4 KiB block (faster)</option></select></div>
    <div class="row sb" style="margin:14px 0 8px"><b class="muted" style="font-size:12px;letter-spacing:1.4px">FILE TYPES</b><button class="btn sm ghost" id="rcTypesAll">Toggle all</button></div>
    <div class="ctypes" id="rcTypes"></div>
    <div class="row" style="margin-top:14px"><span class="muted">Save to</span><input type="text" id="rcOut" style="flex:1"><button class="btn pri" id="rcCarve"><span data-ic="play"></span>Start carving</button></div>
    <div class="row sb" style="margin-top:14px"><b class="muted" style="font-size:12px;letter-spacing:1.4px">RECENTLY RECOVERED</b><span class="mono" id="rcFiles"></span></div>
    <div class="items" id="rcRecent" style="max-height:260px;margin-top:8px"></div>
  </div>
  <div class="rcp card" data-t="undo" style="display:none"><div class="muted" style="margin-bottom:10px">Every sector MySysPilot overwrites during a repair is saved here first, so the change can be reverted.</div><div id="rcUndo"></div></div>
  <div class="rcp card" data-t="cli" style="display:none"><div id="rcCli"></div></div>
</section>

<!-- DOCKER -->
<section class="page" id="p-docker">
  <div id="dkrBanner"></div>
  <div class="grid g12" style="margin-bottom:18px">
    <div class="card glow c3"><div id="gDkC"></div></div>
    <div class="card glow c3"><div id="gDkI"></div></div>
    <div class="card c3 status" id="dkrEngine"><div class="empty"><span class="spin"></span></div></div>
    <div class="card c3"><h3><span data-ic="info"></span>Host</h3><div id="dkrSys"></div></div>
  </div>
  <div id="dkrMain">
    <div class="row" style="margin-bottom:14px">
      <div class="tabs" id="dkrTabs"><button data-t="containers" class="on">Containers</button><button data-t="images">Images</button><button data-t="volumes">Volumes</button><button data-t="networks">Networks</button><button data-t="compose">Compose</button><button data-t="system">System</button><button data-t="setup">Setup</button></div>
      <span id="dkrTools" class="row"><div class="search"><span data-ic="search"></span><input type="text" id="dkrSearch" placeholder="Filter" style="width:180px"></div><label class="chk" id="dkrAllL"><input type="checkbox" id="dkrAll" checked> Show stopped</label></span>
      <span class="spacer"></span><span id="dkrBtns" class="row"></span><button class="btn sm" id="dkrLearn"><span data-ic="help"></span>Examples &amp; Templates</button><button class="btn sm ghost" id="dkrRefresh"><span data-ic="refresh"></span>Refresh</button>
    </div>
    <div id="dkrPanels">
      <div class="dkp" data-t="containers"><div class="dkbody"><div class="empty"><span class="spin"></span></div></div></div>
      <div class="dkp" data-t="images" style="display:none"><div class="dkbody"></div></div>
      <div class="dkp" data-t="volumes" style="display:none"><div class="dkbody"></div></div>
      <div class="dkp" data-t="networks" style="display:none"><div class="dkbody"></div></div>
      <div class="dkp" data-t="compose" style="display:none"><div class="dkbody"></div></div>
      <div class="dkp" data-t="system" style="display:none"><div class="dkbody"></div></div>
      <div class="dkp" data-t="setup" style="display:none"><div class="dkbody"></div></div>
    </div>
  </div>
</section>

<!-- SPEED TEST -->
<section class="page" id="p-speed">
  <div class="grid g12">
    <div class="card glow c7">
      <div class="row sb"><h3 style="margin:0"><span data-ic="gauge"></span>Internet speed</h3><span class="pill" id="spSrvPill">Server: —</span></div>
      <div class="speedo" id="speedo"></div>
      <div class="row" style="justify-content:center;margin:8px 0 16px"><button class="btn pri" id="spGo" style="padding:11px 34px;font-size:15px"><span data-ic="play"></span>Start test</button><button class="btn ghost" id="spStop" disabled>Stop</button></div>
      <div class="metrics">
        <div class="metric"><div class="k"><span data-ic="ping"></span>PING</div><div class="v" id="mPing">—<small>ms</small></div></div>
        <div class="metric"><div class="k"><span data-ic="wave"></span>JITTER</div><div class="v" id="mJit">—<small>ms</small></div></div>
        <div class="metric"><div class="k" style="color:var(--cyan)"><span data-ic="down"></span>DOWNLOAD</div><div class="v" id="mDl">—<small>Mbps</small></div></div>
        <div class="metric"><div class="k" style="color:var(--violet)"><span data-ic="up"></span>UPLOAD</div><div class="v" id="mUl">—<small>Mbps</small></div></div>
      </div>
      <canvas class="chart" id="chSpeed" style="height:130px;margin-top:14px"></canvas>
    </div>
    <div class="card c5"><h3><span data-ic="globe"></span>Test server<span class="r"><button class="btn sm ghost" id="spPing">Ping all</button></span></h3>
      <div class="row" style="margin-bottom:10px"><div class="search" style="flex:1"><span data-ic="search"></span><input type="text" id="spSearch" placeholder="City, country or sponsor" style="width:100%"></div>
      <select id="spStreams" title="Parallel streams"><option value="1">1 stream</option><option value="2">2 streams</option><option value="4" selected>4 streams</option><option value="6">6 streams</option><option value="8">8 streams</option></select>
      <select id="spDur" title="Duration per direction"><option value="6">6 s</option><option value="10" selected>10 s</option><option value="15">15 s</option></select></div>
      <div class="srvlist" id="spList"><div class="empty"><span class="spin"></span></div></div>
      <div class="muted" style="font-size:12px;margin-top:10px" id="spNote">Latency is HTTP round-trip time (not ICMP). Test traffic goes only to the server you select.</div>
    </div>
    <div class="card c12"><h3><span data-ic="list"></span>History (stored locally)<span class="r"><button class="btn sm ghost" id="spClear">Clear</button></span></h3>
      <div class="tbl-wrap" style="max-height:300px"><table class="t" id="spHist"><thead><tr><th>Date</th><th>Server</th><th>Ping</th><th>Jitter</th><th>Download</th><th>Upload</th><th>Streams</th></tr></thead><tbody></tbody></table></div></div>
  </div>
</section>

<!-- SETTINGS -->
<section class="page" id="p-settings">
  <div class="grid g12"><div class="card c8"><h3><span data-ic="gear"></span>Preferences</h3>
    <div class="form">
      <label>Theme</label><div class="tabs" id="stTheme"><button data-v="dark">Dark</button><button data-v="light">Light</button><button data-v="system">System</button></div>
      <label>Refresh interval</label><select id="stRefresh"><option value="1000">1 second</option><option value="2000">2 seconds</option><option value="5000">5 seconds</option></select>
      <label>Network units</label><select id="stUnits"><option value="bits">bits/s (Mbps)</option><option value="bytes">bytes/s (MB/s)</option></select>
      <label>Glow intensity</label><input type="range" id="stGlow" min="0" max="100" value="100">
    </div>
    <div class="infobox" style="margin-top:16px">Preferences are saved in this browser window only (localStorage). Nothing is synced.</div>
  </div>
  <div class="card c4"><h3><span data-ic="shield"></span>Components</h3><div id="stTools" class="kv"></div></div></div>
</section>

<!-- HELP -->
<section class="page help" id="p-help"><div class="card">
<h2>Getting started</h2>
<p>MySysPilot is a local web app served by a Python process on <code>127.0.0.1</code>. Start it from the app menu, with <code>mysyspilot</code> (installed .deb) or <code>python3 MySysPilot_v1.4.1.py</code> (add <code>--install-desktop</code> once to create an app-menu launcher). Do not run it with sudo — any action that needs root opens the standard polkit password dialog.</p>
<h2>Sections</h2>
<ul>
<li><b>Dashboard</b> — live CPU, memory, uptime, load, root filesystem, swap, network throughput, per-core load, disk I/O and system identity. Sampled once per second from <code>/proc</code> and <code>/sys</code>.</li>
<li><b>Resources</b> — 3-minute rolling charts for CPU (total + per core), memory/swap, network and disk I/O, plus all hwmon temperature sensors.</li>
<li><b>Processes</b> — sortable table. CPU % is a share of the whole machine (100% = all cores busy). Select a row to send TERM/KILL/STOP/CONT or change priority (nice). Other users' processes require authorization.</li>
<li><b>Services</b> — systemd services with start/stop/restart and enable/disable at boot.</li>
<li><b>Startup Apps</b> — XDG autostart entries. Disabling a system entry writes an override into <code>~/.config/autostart</code>; the system file is never modified.</li>
<li><b>System Cleaner</b> — APT package cache, crash reports, rotated logs, systemd journal (vacuum to 7 days), application caches, thumbnails, Trash and disabled snap revisions. Review items before cleaning.</li>
<li><b>Uninstaller</b> — APT (deb), Snap and Flatpak applications, plus <code>apt autoremove</code>.</li>
<li><b>APT Repositories</b> — enable/disable entries in .list and deb822 .sources files.</li>
<li><b>Disk Utility</b> — a GNOME Disks–style tool built on the same UDisks2 service: drive list, partition map, mount/unmount, format disk (GPT/MBR) or partition (ext4, XFS, Btrfs, FAT, exFAT, NTFS, F2FS, swap, optional LUKS2 encryption), create/delete/resize partitions, edit label and partition name, take ownership, unlock/lock/change passphrase, SMART health with self-tests, read benchmark, attach image files, standby/wake, power off and eject. <b>Mount / Unmount</b> per volume or <i>Mount all / Unmount all</i> per drive. <b>Check &amp; Repair</b> uses UDisks2 and falls back to the native fsck tool (e2fsck, fsck.vfat, fsck.exfat, ntfsfix, xfs_repair) with full output; mounted volumes are unmounted and remounted automatically. <b>Backup &amp; Restore</b>: block-level images or file archives (tar) with zstd/gzip compression and SHA-256 checksums, verified before restore; partition-table backup/restore (sfdisk) and GPT header repair (sgdisk). All backups are listed in the Backups table.</li>
<li><b>Find &amp; Duplicates</b> — three modes: <i>Duplicate files</i> (identical content, optional name filter such as <code>*.jpg</code>), <i>Copies of a file</i> (enter one file, find every identical copy), and <i>Search files &amp; text</i> (by name — contains, wildcard or regex — and/or text inside files, with size and date filters). Results can be filtered, opened in the file manager, and moved to the Trash. One copy of every duplicate group is always kept.</li>
<li><b>Data Recovery</b> — TestDisk-style functions: <i>Quick/Deep Search</i> for lost or deleted partitions (NTFS, FAT, exFAT, ext2/3/4, XFS, Btrfs, LUKS, swap, LVM, ISO9660 — including filesystems found only through backup boot sectors or backup superblocks), write the recovered partition table (GPT or MBR), and browse a found partition read-only to copy files out. <i>Boot &amp; superblock repair</i>: NTFS boot sector ⇄ backup, $MFT ⇄ $MFTMirr, FAT32 boot sector ⇄ backup, FAT1 ⇄ FAT2, exFAT main ⇄ backup boot region (checksum-verified), ext2/3/4 backup superblock location and <code>e2fsck -b</code>, xfs_repair and btrfs rescue. <i>Undelete</i> for NTFS and FAT12/16/32 with original names, folders, dates and an overwritten/recoverable status. <i>File carving</i> (PhotoRec-style) for JPEG, PNG, GIF, BMP, PDF, Office (docx/xlsx/pptx/odt and doc/xls/ppt), ZIP, 7-Zip, MP4/MOV/HEIC, AVI/WAV/WebP, MP3, Ogg and SQLite. Every sector write is verified and saved to an Undo log. Works on disks and on disk-image files.</li>
<li><b>Operations &amp; drive status</b> — every disk action (mount, unmount, label, check, repair, format, partition, resize, backup, restore, benchmark, recovery) appears in Disk Utility → Operations and in the header while it runs, with a progress ring, step chips, a progress bar (striped when the tool cannot report a percentage), throughput graph, elapsed time and ETA. Real percentages come from UDisks2 job progress, e2fsck per-pass progress, xfs_repair phases and byte counters for backup/restore. Drive cards show OK / FAILING (SMART), MOUNTED / NOT MOUNTED, NEEDS REPAIR, I/O ERRORS, SELF-TEST % and WORKING. <i>Needs repair</i> is raised only by evidence: kernel messages in this boot (FAT/exFAT "not properly unmounted", NTFS dirty, ext4/XFS/Btrfs errors), ext4's own error counter, or a MySysPilot check that found problems — and is cleared by a successful check or repair. Click the badge to open Repair.</li>
<li><b>Docker</b> — install Docker Engine from Docker's official apt repository (or your distribution's docker.io), start/stop/enable the daemon, manage docker-group access, edit <code>/etc/docker/daemon.json</code>, and administer containers (start, stop, restart, pause, logs, run a command, inspect, restart policy, remove, run new), images (pull, tag, remove, prune), volumes, networks, Compose projects (up, restart, pull, stop, down) and disk usage / system prune.</li>
<li><b>Speed Test</b> — choose Cloudflare (nearest edge) or any public LibreSpeed server; "Ping all" ranks servers by latency. Measures ping, jitter, download and upload with parallel HTTPS streams; the first ~2 s of each direction are excluded as warm-up.</li>
</ul>
<h2>Safety rules built in</h2>
<ul>
<li>Destructive disk actions require typing the exact device path and are refused for any device that holds <code>/</code>, <code>/boot</code>, <code>/boot/efi</code>, <code>/home</code>, <code>/usr</code>, <code>/var</code> or active swap.</li>
<li>Backups are written only to a new file and never onto the volume being backed up; restores verify the checksum first and require all target filesystems unmounted.</li><li>Data Recovery opens disks read-only for searching, undelete and carving; repairs require the volume to be unmounted, typed confirmation, and save the overwritten bytes for Undo. Recovered files are refused if the output folder is on the disk being recovered.</li><li>Running system volumes cannot be unmounted, repaired, formatted or restored from inside the running system — use a live USB for those.</li>
<li>The Duplicate Finder refuses a delete that would remove every copy in a group.</li>
<li>APT source edits are backed up to <code>/var/backups/mysyspilot/</code> before replacement.</li>
</ul>
<h2>Privacy / OPSEC</h2>
<ul>
<li>The server listens only on loopback, requires a random per-launch token, and rejects non-loopback Host headers. The UI loads no external fonts, scripts or images.</li>
<li>Docker install (only when you click Install) downloads from <code>download.docker.com</code> or your distribution mirrors. Membership of the docker group is root-equivalent; it is opt-in.</li><li>The Speed Test contacts <code>librespeed.org</code> (server list) and the server you choose. Your public IP is visible to that server. Results are kept only in <code>~/.local/share/mysyspilot/speed_history.json</code>.</li>
<li>LUKS passphrases are passed to UDisks2 over the system D-Bus and are never written to disk or command lines.</li>
</ul>
<h2>Requirements</h2>
<ul>
<li>Python 3.8+ (standard library only). Linux with systemd and <code>/proc</code>.</li>
<li>Disk Utility write actions, SMART and benchmark: <code>python3-gi</code> and <code>udisks2</code> (default on Ubuntu Desktop). Detailed SMART tables: <code>sudo apt install smartmontools</code>. Compressed backups: <code>zstd</code>. GPT repair: <code>gdisk</code>.</li>
<li>Privileged actions: <code>pkexec</code> with a running polkit agent (default in GNOME/KDE/XFCE sessions).</li>
</ul>
<h2>Known limits (v1.0)</h2>
<ul>
<li>Multi-gigabit results can be limited by CPU (TLS + Python) rather than your line; use more streams on fast links.</li>
<li>fstab/crypttab mount options are shown read-only; edit them with GNOME Disks or a text editor.</li>
<li>Benchmark is read-only (no write test) to avoid touching data.</li><li>Btrfs repair is not automated (<code>btrfs check --repair</code> can destroy data); Check runs read-only.</li><li>Docker exec runs single non-interactive commands (no TTY shell).</li><li>Data Recovery does not rebuild a FAT or NTFS boot sector from scratch when both copies are lost, and has no undelete for exFAT or ext3/ext4 (use carving). TestDisk/PhotoRec can be launched from the TestDisk / PhotoRec tab for those cases.</li><li>Undelete assumes deleted FAT files were stored contiguously (as TestDisk does); fragmented files may recover with wrong content.</li>
<li>Repository toggles and package removals use APT on Debian/Ubuntu-family systems only.</li>
</ul>
<h2>Keyboard</h2>
<p><span class="kbd">T</span> toggle theme · <span class="kbd">1</span>–<span class="kbd">9</span> jump to the first nine sections · <span class="kbd">Esc</span> close dialogs · <span class="kbd">/</span> focus search on the current page</p>
</div></section>

<!-- ABOUT -->
<section class="page" id="p-about"><div class="grid g12">
  <div class="card glow c7"><div class="row" style="gap:18px;align-items:center">
    <svg viewBox="0 0 128 128" style="width:84px;height:84px"><rect width="128" height="128" rx="28" fill="#0b1426"/><circle cx="64" cy="64" r="40" fill="none" stroke="#1e2a44" stroke-width="10"/><path d="M64 24a40 40 0 1 1-34.6 60" fill="none" stroke="#22d3ee" stroke-width="10" stroke-linecap="round"/><path d="M64 64 86 46" stroke="#22e39a" stroke-width="7" stroke-linecap="round"/><circle cx="64" cy="64" r="7" fill="#22e39a"/></svg>
    <div><div style="font-size:24px;font-weight:700">MySysPilot <span class="muted" style="font-size:16px" id="abVer">v1.4.1</span></div><div class="muted">Linux system optimizer · monitor · disk utility &amp; backup · data recovery · Docker · file search · speed test</div></div></div>
    <div class="kv" style="margin-top:18px" id="abKv"></div>
    <div class="infobox" style="margin-top:16px">Feature set inspired by <b>Stacer</b> (Oğuzhan İnan, GPL-3.0) <b>GNOME Disks</b> (gnome-disk-utility / UDisks2) and <b>TestDisk / PhotoRec</b> (Christophe Grenier, CGSecurity, GPL). MySysPilot is an independent implementation; no code from these projects is included. Speed testing uses Cloudflare's public speed endpoints and LibreSpeed community servers.</div>
    <div class="row" style="margin-top:14px"><button class="btn danger" id="abQuit"><span data-ic="power"></span>Quit MySysPilot server</button></div>
  </div>
  <div class="card c5"><h3><span data-ic="info"></span>Build</h3><div class="kv" id="abBuild"></div><div class="muted" style="margin-top:14px;font-size:12.5px">© 2026 Gus Salazar. Provided as-is, without warranty. Back up data before partitioning, formatting or restoring images.</div></div>
</div></section>

    </div>
  </main>
</div>
<div class="modal-bg" id="modalBg"><div class="modal" id="modal"></div></div>
<div class="toasts" id="toasts"></div>

<script>
"use strict";
/* ============================================================ icons */
const IC={
 dash:'<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
 chart:'<path d="M3 3v18h18"/><path d="m7 15 4-5 4 3 5-7"/>',
 cpu:'<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
 mem:'<rect x="2" y="7" width="20" height="10" rx="2"/><path d="M6 7v10M10 7v10M14 7v10M18 7v10M5 17v3M19 17v3"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 disk:'<ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
 swap:'<path d="M7 4 3 8l4 4"/><path d="M3 8h14"/><path d="m17 20 4-4-4-4"/><path d="M21 16H7"/>',
 io:'<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><rect x="3" y="17" width="18" height="4" rx="1"/>',
 host:'<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
 linux:'<path d="M12 3c-2 0-3 2-3 4.5 0 2-1.5 3-2.5 5S5 17 6.5 19 10 20 12 20s4 .5 5.5-1 .5-4.5-.5-6.5S15 9.5 15 7.5 14 3 12 3z"/><circle cx="10.5" cy="8" r=".6"/><circle cx="13.5" cy="8" r=".6"/>',
 chip:'<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 9h6v6H9z"/>',
 desk:'<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M2 13h20M9 21h6"/>',
 bolt:'<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
 net:'<path d="M5 12.5a10 10 0 0 1 14 0"/><path d="M8.5 16a5 5 0 0 1 7 0"/><path d="M2 9a15 15 0 0 1 20 0"/><circle cx="12" cy="19.5" r="1"/>',
 list:'<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/>',
 gear:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
 rocket:'<path d="M4.5 16.5c-1.5 1.3-2 5-2 5s3.7-.5 5-2c.7-.8.7-2.1-.1-2.9a2.2 2.2 0 0 0-2.9-.1z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.9A12.9 12.9 0 0 1 22 2c0 2.7-.8 7.5-6 11a22.4 22.4 0 0 1-4 2z"/><path d="M9 12H4s.6-3 2-4c1.6-1.1 5 0 5 0M12 15v5s3-.6 4-2c1.1-1.6 0-5 0-5"/>',
 power:'<path d="M18.4 6.6a9 9 0 1 1-12.8 0"/><path d="M12 2v10"/>',
 startup:'<path d="M5 3l14 9-14 9V3z"/>',
 broom:'<path d="m19.5 3.5-8 8"/><path d="M11.5 11.5c-2-1-5 0-6.5 2.5L3 21l7-2c2.5-1.5 3.5-4.5 2.5-6.5z"/><path d="m6.5 14.5 3 3"/>',
 box:'<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
 repo:'<path d="M4 4h12a4 4 0 0 1 4 4v12H8a4 4 0 0 1-4-4z"/><path d="M8 4v12a4 4 0 0 0 0 0"/><path d="M12 9h4M12 13h4"/>',
 copy:'<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
 gauge:'<path d="M12 14 16 9"/><path d="M3.3 17a9 9 0 1 1 17.4 0"/><circle cx="12" cy="14" r="1.5"/>',
 help:'<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.6 2.6 0 0 1 5 1c0 2-2.5 2-2.5 4"/><circle cx="12" cy="17.5" r=".6"/>',
 info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><circle cx="12" cy="7.5" r=".6"/>',
 search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
 refresh:'<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
 plus:'<path d="M12 5v14M5 12h14"/>',
 trash:'<path d="M3 6h18M8 6V4h8v2M6 6l1 15h10l1-15"/>',
 sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
 moon:'<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
 temp:'<path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/>',
 image:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>',
 play:'<path d="M6 4l14 8-14 8z"/>',
 ping:'<path d="M2 12h4l3-8 4 16 3-8h6"/>',
 wave:'<path d="M2 12c2-4 4-4 6 0s4 4 6 0 4-4 6 0"/>',
 down:'<path d="M12 4v14"/><path d="m6 12 6 6 6-6"/>',
 up:'<path d="M12 20V6"/><path d="m6 12 6-6 6 6"/>',
 globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
 shield:'<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/>',
 usb:'<path d="M12 2v14"/><circle cx="12" cy="19" r="2.5"/><path d="M7 8v3l5 3 5-3V7"/><rect x="15.5" y="5" width="3" height="2.5"/><circle cx="7" cy="7" r="1.3"/>',
 hdd:'<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="12" cy="11" r="4"/><circle cx="12" cy="11" r=".8"/><path d="M6 17.5h3"/>',
 ssd:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 9h6M7 12h10M7 15h4"/>',
 lock:'<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
 x:'<path d="M18 6 6 18M6 6l12 12"/>',
 heart:'<path d="M3 12h4l2-5 3 10 2-6 2 3h5"/>',
 docker:'<path d="M2 12.5h17.5c.5-1.6 1.6-2.5 2.5-2.7-.6-.9-1.6-1.1-2.4-.9-.3-1.2-1.1-2-1.1-2-.9.9-1 2.4-.6 3.3H2z"/><path d="M2 12.5c.4 4 3.4 6.5 8 6.5 4.3 0 7.3-2.2 8.6-6.5"/><rect x="5" y="9.5" width="2.5" height="2.5"/><rect x="8" y="9.5" width="2.5" height="2.5"/><rect x="11" y="9.5" width="2.5" height="2.5"/><rect x="8" y="6.5" width="2.5" height="2.5"/><rect x="11" y="6.5" width="2.5" height="2.5"/>',
 life:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="m5.6 5.6 3.6 3.6M14.8 14.8l3.6 3.6M18.4 5.6l-3.6 3.6M9.2 14.8l-3.6 3.6"/>',
 folder:'<path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
 save:'<path d="M5 3h11l5 5v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M7 3v6h9V3M7 21v-7h10v7"/>',
 stop:'<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
 pause:'<path d="M8 5v14M16 5v14"/>',
 term:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>',
 eye:'<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
};
function icon(n,cls){return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"${cls?` class="${cls}"`:''}>${IC[n]||''}</svg>`}
function paintIcons(root){(root||document).querySelectorAll('[data-ic]').forEach(e=>{if(!e.dataset.icDone){e.insertAdjacentHTML('afterbegin',icon(e.dataset.ic));e.dataset.icDone=1}})}

/* ============================================================ utils */
const $=(s,r)=>(r||document).querySelector(s), $$=(s,r)=>[...(r||document).querySelectorAll(s)];
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmtB(b,d=1){if(b==null||isNaN(b))return '—';const u=['B','KiB','MiB','GiB','TiB','PiB'];let i=0;b=+b;while(Math.abs(b)>=1024&&i<u.length-1){b/=1024;i++}return (i?b.toFixed(d):b.toFixed(0))+' '+u[i]}
function fmtSI(b,d=1){if(b==null)return '—';const u=['B','kB','MB','GB','TB'];let i=0;while(b>=1000&&i<u.length-1){b/=1000;i++}return (i?b.toFixed(d):Math.round(b))+' '+u[i]}
function fmtBits(bps){const unit=SET.units;if(unit==='bytes'){return fmtSI(bps/8)+'/s'}if(bps<1e3)return bps.toFixed(0)+' b/s';if(bps<1e6)return (bps/1e3).toFixed(1)+' Kb/s';if(bps<1e9)return (bps/1e6).toFixed(2)+' Mb/s';return (bps/1e9).toFixed(2)+' Gb/s'}
function splitRate(bps){if(SET.units==='bytes'){const B=bps/8;if(B<1e6)return[(B/1e3).toFixed(1),'kB/s'];return[(B/1e6).toFixed(2),'MB/s']}if(bps<1e6)return[(bps/1e3).toFixed(1),'Kbps'];if(bps<1e9)return[(bps/1e6).toFixed(2),'Mbps'];return[(bps/1e9).toFixed(2),'Gbps']}
const pad=n=>String(n).padStart(2,'0');
function fmtDate(t){const d=new Date(t*1000);return d.toLocaleString()}
function lsGet(k,def){try{const v=localStorage.getItem('msp_'+k);return v==null?def:JSON.parse(v)}catch(e){return def}}
function lsSet(k,v){try{localStorage.setItem('msp_'+k,JSON.stringify(v))}catch(e){}}
function hexMix(a,b,t){const pa=parseInt(a.slice(1),16),pb=parseInt(b.slice(1),16);const r=Math.round(((pa>>16)&255)*(1-t)+((pb>>16)&255)*t),g=Math.round(((pa>>8)&255)*(1-t)+((pb>>8)&255)*t),bl=Math.round((pa&255)*(1-t)+(pb&255)*t);return '#'+((1<<24)|(r<<16)|(g<<8)|bl).toString(16).slice(1)}
function grad3(c,t){return t<.5?hexMix(c[0],c[1],t*2):hexMix(c[1],c[2],(t-.5)*2)}
const PAL={blue:['#2563eb','#3b82f6','#22d3ee'],green:['#0f8f63','#10c98a','#34f5b0'],violet:['#6d28d9','#8b5cf6','#c084fc'],cyan:['#0e7490','#06b6d4','#67e8f9'],amber:['#b45309','#f59e0b','#fde047'],speed:['#2563eb','#22d3ee','#22e39a'],up:['#7c3aed','#a78bfa','#f472b6']};
function cssv(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim()}

/* ============================================================ api */
let TOKEN=null;
(function(){const m=location.hash.match(/t=([A-Za-z0-9_\-]+)/);if(m){TOKEN=m[1];try{sessionStorage.setItem('msp_t',TOKEN)}catch(e){}history.replaceState(null,'',location.pathname)}else{try{TOKEN=sessionStorage.getItem('msp_t')}catch(e){}}})();
async function api(path,body){
  const o={headers:{'X-MSP-Token':TOKEN||''}};
  if(body!==undefined){o.method='POST';o.headers['Content-Type']='application/json';o.body=JSON.stringify(body)}
  let r;try{r=await fetch(path,o)}catch(e){setConn(false);throw new Error('MySysPilot server is not reachable')}
  let j;try{j=await r.json()}catch(e){throw new Error('HTTP '+r.status)}
  if(!r.ok||j.error)throw new Error(j.error||('HTTP '+r.status));
  return j;
}
async function waitJob(id,onTick){
  for(;;){const j=await api('/api/job?id='+encodeURIComponent(id));if(onTick)onTick(j);if(j.state!=='running'){if(j.state==='error')throw new Error(j.error);return j}await new Promise(r=>setTimeout(r,350))}
}
function toast(msg,kind){const t=document.createElement('div');t.className='toast '+(kind||'');t.innerHTML=msg;$('#toasts').appendChild(t);setTimeout(()=>{t.style.opacity=0;t.style.transition='.4s'},kind==='err'?7000:3800);setTimeout(()=>t.remove(),kind==='err'?7500:4300)}
function terr(e){toast(esc(e.message||e),'err')}
let connOk=null;function setConn(ok){if(ok===connOk)return;connOk=ok;$('#connDot').className='dot'+(ok?'':' red');$('#connTxt').textContent=ok?'Live · 127.0.0.1':'Disconnected'}

/* ============================================================ modal */
let modalClose=null,MODAL_GEN=0;
function openModal(title,html,acts,onClose){MODAL_GEN++;
  const m=$('#modal');m.className='modal';m.innerHTML=`<h2>${title}</h2><div class="mb">${html}</div><div class="acts"></div>`;paintIcons(m);
  const a=$('.acts',m);(acts||[{label:'Close'}]).forEach(x=>{const b=document.createElement('button');b.className='btn '+(x.cls||'ghost');b.innerHTML=x.label;b.onclick=async()=>{const g=MODAL_GEN;if(x.onClick){const r=await x.onClick(b);if(r===false)return}if(MODAL_GEN===g)closeModal()};a.appendChild(b)});
  $('#modalBg').classList.add('on');modalClose=onClose||null;setTimeout(()=>{const f=$('input,select,textarea',m);if(f)f.focus()},50);
}
function closeModal(){$('#modalBg').classList.remove('on');if(modalClose){const f=modalClose;modalClose=null;f()}}
$('#modalBg').addEventListener('mousedown',e=>{if(e.target.id==='modalBg')closeModal()});
function confirmBox(title,msg,okLabel,danger,typeText){
  return new Promise(res=>{
    const extra=typeText?`<div class="warnbox">This cannot be undone. Type <b class="mono">${esc(typeText)}</b> to confirm.</div><input type="text" id="cfType" style="width:100%" autocomplete="off">`:'';
    let done=false;
    openModal(title,`<div>${msg}</div>${extra}`,[{label:'Cancel'},{label:okLabel||'OK',cls:danger?'danger':'pri',onClick:()=>{if(typeText&&$('#cfType').value!==typeText){toast('Confirmation text does not match','err');return false}done=true;res(typeText?$('#cfType').value:true)}}],()=>{if(!done)res(false)});
  });
}
function promptBox(title,fields,okLabel){
  return new Promise(res=>{
    const html='<div class="form">'+fields.map(f=>`<label>${f.label}</label>`+(f.type==='select'?`<select id="pf_${f.id}">${f.options.map(o=>`<option value="${esc(o[0])}"${o[0]==f.value?' selected':''}>${esc(o[1])}</option>`).join('')}</select>`:f.type==='check'?`<label class="chk"><input type="checkbox" id="pf_${f.id}"${f.value?' checked':''}> ${f.text||''}</label>`:`<input type="${f.type||'text'}" id="pf_${f.id}" value="${esc(f.value==null?'':f.value)}" placeholder="${esc(f.ph||'')}" autocomplete="off">`)).join('')+'</div>'+(fields.note||'');
    let done=false;
    openModal(title,html,[{label:'Cancel'},{label:okLabel||'OK',cls:'pri',onClick:()=>{const o={};fields.forEach(f=>{const e=$('#pf_'+f.id);o[f.id]=f.type==='check'?e.checked:e.value});done=true;res(o)}}],()=>{if(!done)res(null)});
  });
}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('#modalBg').classList.contains('on'))closeModal()});

/* ============================================================ SVG gauges */
const NS='http://www.w3.org/2000/svg';
let GID=0;
function arcPath(cx,cy,r1,r2,a0,a1){ // degrees, 0 = top, clockwise
  const p=(r,a)=>{const t=(a-90)*Math.PI/180;return[cx+r*Math.cos(t),cy+r*Math.sin(t)]};
  const [x0,y0]=p(r2,a0),[x1,y1]=p(r2,a1),[x2,y2]=p(r1,a1),[x3,y3]=p(r1,a0);const lg=(a1-a0)>180?1:0;
  return `M${x0.toFixed(2)} ${y0.toFixed(2)}A${r2} ${r2} 0 ${lg} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}L${x2.toFixed(2)} ${y2.toFixed(2)}A${r1} ${r1} 0 ${lg} 0 ${x3.toFixed(2)} ${y3.toFixed(2)}Z`;
}
/* Smooth ring with lit tick bezel (the "TIME LEFT" style) */
function RingGauge(host,o){
  const id='rg'+(++GID),S=240,C=120,R=86,SW=13,circ=2*Math.PI*R,pal=PAL[o.pal||'blue'],N=72;
  let ticks='';for(let i=0;i<N;i++){const a=i*360/N;ticks+=`<path class="tk" d="${arcPath(C,C,R+15,R+21,a-.9,a+.9)}" fill="var(--track2)"/>`}
  host.innerHTML=`<svg viewBox="0 0 ${S} ${S}" class="gauge"><defs>
   <linearGradient id="${id}l" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="${pal[0]}"/><stop offset=".55" stop-color="${pal[1]}"/><stop offset="1" stop-color="${pal[2]}"/></linearGradient>
   <filter id="${id}f" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
   <circle cx="${C}" cy="${C}" r="${R+28}" fill="none" stroke="var(--track)" stroke-width="1"/>
   <g class="tks">${ticks}</g>
   <circle cx="${C}" cy="${C}" r="${R}" fill="none" stroke="var(--track)" stroke-width="${SW}"/>
   <circle class="arcv" cx="${C}" cy="${C}" r="${R}" fill="none" stroke="url(#${id}l)" stroke-width="${SW}" stroke-linecap="round" stroke-dasharray="0 ${circ}" transform="rotate(-90 ${C} ${C})" filter="url(#${id}f)"/>
   <circle cx="${C}" cy="${C}" r="${R-20}" fill="none" stroke="var(--track)" stroke-width="1" stroke-dasharray="2 5"/>
   <text class="g-label" x="${C}" y="${C-34}">${esc(o.label)}</text>
   <text class="g-value" x="${C}" y="${C+12}">—</text>
   <text class="g-sub" x="${C}" y="${C+40}"></text>
   <g transform="translate(${C-9} ${C+52})" stroke="var(--muted)" fill="none" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${o.icon?`<svg width="18" height="18" viewBox="0 0 24 24">${IC[o.icon]}</svg>`:''}</g></svg>`;
  const arc=host.querySelector('.arcv'),val=host.querySelector('.g-value'),sub=host.querySelector('.g-sub'),tks=[...host.querySelectorAll('.tk')];
  return{set(pct,text,subtext){pct=Math.max(0,Math.min(100,pct||0));arc.setAttribute('stroke-dasharray',`${circ*pct/100} ${circ}`);val.innerHTML=text;sub.textContent=subtext||'';const lit=Math.round(N*pct/100);tks.forEach((t,i)=>{t.setAttribute('fill',i<lit?grad3(pal,i/N):'var(--track2)');t.style.opacity=i<lit?1:.7})}};
}
/* Segmented ring (the "PROGRESS 68%" style) */
function SegGauge(host,o){
  const id='sg'+(++GID),S=240,C=120,R=88,N=o.n||52,pal=PAL[o.pal||'green'],gap=360/N*.32;
  let segs='';for(let i=0;i<N;i++){const a0=i*360/N+gap/2,a1=(i+1)*360/N-gap/2;segs+=`<path class="seg" d="${arcPath(C,C,R-10,R+10,a0,a1)}" fill="var(--track)"/>`}
  host.innerHTML=`<svg viewBox="0 0 ${S} ${S}" class="gauge"><defs><filter id="${id}f" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="3.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
   <radialGradient id="${id}r"><stop offset="0" stop-color="${pal[1]}" stop-opacity=".16"/><stop offset="1" stop-color="${pal[1]}" stop-opacity="0"/></radialGradient></defs>
   <circle cx="${C}" cy="${C}" r="${R+18}" fill="none" stroke="var(--track)" stroke-width="1.2"/>
   <circle cx="${C}" cy="${C}" r="${R-18}" fill="url(#${id}r)" stroke="var(--track)" stroke-width="1"/>
   <g filter="url(#${id}f)">${segs}</g>
   <text class="g-label" x="${C}" y="${C-30}">${esc(o.label)}</text>
   <text class="g-value" x="${C}" y="${C+12}" style="fill:${pal[1]}">—</text>
   <text class="g-sub" x="${C}" y="${C+34}"></text>
   <g transform="translate(${C-12} ${C+42})" stroke="${pal[1]}" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><svg width="24" height="20" viewBox="0 0 24 24">${IC.heart}</svg></g></svg>`;
  const segEls=[...host.querySelectorAll('.seg')],val=host.querySelector('.g-value'),sub=host.querySelector('.g-sub');
  const cols=segEls.map((_,i)=>grad3(pal,i/(N-1)));
  return{set(pct,text,subtext){pct=Math.max(0,Math.min(100,pct||0));const lit=Math.round(N*pct/100);segEls.forEach((s,i)=>s.setAttribute('fill',i<lit?cols[i]:'var(--track)'));val.innerHTML=text;sub.textContent=subtext||''}};
}
/* Mini ring with icon (the "12 HRS" style) */
function MiniRing(host,o){
  const S=100,C=50,R=40,circ=2*Math.PI*R,pal=PAL[o.pal||'blue'],id='mr'+(++GID);
  host.innerHTML=`<svg viewBox="0 0 ${S} ${S}"><defs><linearGradient id="${id}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${pal[0]}"/><stop offset="1" stop-color="${pal[2]}"/></linearGradient><filter id="${id}f" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="2.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
  <circle cx="${C}" cy="${C}" r="${R}" fill="none" stroke="var(--track)" stroke-width="5"/>
  <circle class="arcv" cx="${C}" cy="${C}" r="${R}" fill="none" stroke="url(#${id})" stroke-width="5" stroke-linecap="round" stroke-dasharray="0 ${circ}" transform="rotate(-90 ${C} ${C})" filter="url(#${id}f)"/>
  <g transform="translate(${C-7} 20)" stroke="${pal[1]}" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><svg width="14" height="14" viewBox="0 0 24 24">${IC[o.icon||'cpu']}</svg></g>
  <text x="${C}" y="${C+9}" text-anchor="middle" style="font:600 20px var(--mono);fill:var(--text)" class="v">0</text>
  <text x="${C}" y="${C+24}" text-anchor="middle" style="font:600 8.5px var(--font);letter-spacing:1.3px;fill:var(--muted)">${esc(o.label)}</text></svg>`;
  const arc=host.querySelector('.arcv'),v=host.querySelector('.v');
  return{set(pct,text){pct=Math.max(0,Math.min(100,pct||0));arc.setAttribute('stroke-dasharray',`${circ*pct/100} ${circ}`);v.textContent=text}};
}
/* Split-digit countdown panel */
function DigitPanel(host,groups){
  host.innerHTML='<div class="dp">'+groups.map((g,i)=>(i?'<div class="dp-s">:</div>':'')+`<div class="dp-g" style="--c:${g.c}"><div class="dp-d">00</div><div class="dp-l">${g.l}</div></div>`).join('')+'</div>';
  const ds=[...host.querySelectorAll('.dp-d')];return{set(vals){vals.forEach((v,i)=>ds[i].textContent=v)}};
}
/* HUD panel with chamfered frame + slashed bar */
function Hud(host,o){
  const id='hd'+(++GID);
  host.innerHTML=`<svg class="frame" viewBox="0 0 400 112" preserveAspectRatio="none"><defs><filter id="${id}" x="-10%" y="-30%" width="120%" height="160%"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter><linearGradient id="${id}g" x1="0" x2="1"><stop offset="0" stop-color="${o.c}" stop-opacity=".14"/><stop offset="1" stop-color="${o.c}" stop-opacity=".02"/></linearGradient></defs>
  <polygon points="16,1 384,1 399,22 399,90 384,111 16,111 1,90 1,22" fill="url(#${id}g)" stroke="${o.c}" stroke-opacity=".75" stroke-width="1.4" vector-effect="non-scaling-stroke" filter="url(#${id})"/>
  <polyline points="60,1 64,6 160,6 164,1" fill="none" stroke="${o.c}" stroke-width="2" vector-effect="non-scaling-stroke"/><polyline points="240,111 244,106 340,106 344,111" fill="none" stroke="${o.c}" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
  <div class="in"><svg class="reticle" viewBox="0 0 48 48" fill="none" stroke="${o.c}" stroke-width="2" style="color:${o.c}"><circle cx="24" cy="24" r="15"/><circle cx="24" cy="24" r="6"/><path d="M24 3v8M24 37v8M3 24h8M37 24h8"/></svg>
  <div><div class="lbl">${o.label}</div><div class="val"><span class="vv">—</span><small class="uu"></small></div><div class="muted" style="font-size:12px"><span class="tot"></span></div></div>
  <div class="slash" style="--c:${o.c}">${'<i></i>'.repeat(12)}</div></div>`;
  const vv=host.querySelector('.vv'),uu=host.querySelector('.uu'),tot=host.querySelector('.tot'),sl=[...host.querySelectorAll('.slash i')];
  return{set(v,u,frac,t){vv.textContent=v;uu.textContent=u;tot.textContent=t||'';const n=Math.round(12*Math.max(0,Math.min(1,frac)));sl.forEach((s,i)=>s.classList.toggle('on',i<n))}};
}
/* Speedometer */
const SPEED_MARKS=[0,1,5,10,20,30,50,75,100,250,500,1000];
function Speedo(host){
  const id='sp'+(++GID),C=200,R=150,N=64,A0=-135,A1=135;
  let marks=SPEED_MARKS.slice();
  const ang=t=>A0+(A1-A0)*t;
  function build(){
    let segs='',lbl='';
    for(let i=0;i<N;i++){const a0=ang(i/N)+.8,a1=ang((i+1)/N)-.8;segs+=`<path class="sseg" d="${arcPath(C,C,R-14,R+8,a0,a1)}" fill="var(--track)"/>`}
    marks.forEach((m,i)=>{const t=i/(marks.length-1),a=(ang(t)-90)*Math.PI/180,rr=R-36;lbl+=`<text x="${(C+rr*Math.cos(a)).toFixed(1)}" y="${(C+rr*Math.sin(a)+4).toFixed(1)}" text-anchor="middle" style="font:600 12px var(--mono);fill:var(--muted)">${m>=1000?(m/1000)+'G':m}</text>`;
      const a2=(ang(t)-90)*Math.PI/180;lbl+=`<line x1="${(C+(R+14)*Math.cos(a2)).toFixed(1)}" y1="${(C+(R+14)*Math.sin(a2)).toFixed(1)}" x2="${(C+(R+22)*Math.cos(a2)).toFixed(1)}" y2="${(C+(R+22)*Math.sin(a2)).toFixed(1)}" stroke="var(--dim)" stroke-width="2"/>`});
    host.innerHTML=`<svg viewBox="0 0 400 330"><defs><filter id="${id}f" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
      <radialGradient id="${id}h"><stop offset="0" stop-color="#22d3ee" stop-opacity=".16"/><stop offset="1" stop-color="#22d3ee" stop-opacity="0"/></radialGradient>
      <linearGradient id="${id}n" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="var(--text)" stop-opacity=".1"/><stop offset="1" stop-color="var(--text)"/></linearGradient></defs>
      <circle cx="${C}" cy="${C}" r="${R-48}" fill="url(#${id}h)"/>
      <path d="${arcPath(C,C,R+26,R+27.5,A0,A1)}" fill="var(--track2)"/>
      <g filter="url(#${id}f)">${segs}</g>${lbl}
      <g class="needle" style="transform:rotate(${A0}deg)"><path d="M${C-4} ${C} L${C} ${C-R+18} L${C+4} ${C} Z" fill="url(#${id}n)"/></g>
      <circle cx="${C}" cy="${C}" r="11" fill="var(--card2)" stroke="var(--line2)" stroke-width="2"/><circle cx="${C}" cy="${C}" r="4" fill="var(--cyan)"/></svg>
      <div class="read"><div class="n" id="spNum">0.00</div><div class="u" id="spUnit">MBPS</div><div class="ph" id="spPh">READY</div></div>`;
    segEls=[...host.querySelectorAll('.sseg')];needle=host.querySelector('.needle');
  }
  let segEls,needle,pal=PAL.speed;build();
  function tOf(v){if(v<=0)return 0;const m=marks;if(v>=m[m.length-1])return 1;for(let i=0;i<m.length-1;i++){if(v<m[i+1])return (i+(v-m[i])/(m[i+1]-m[i]))/(m.length-1)}return 1}
  return{set(v,phase,kind){if(v>marks[marks.length-1]*0.95&&marks[marks.length-1]<10000){marks=marks.concat([2500,5000,10000].filter(x=>x>marks[marks.length-1]));build()}
      pal=kind==='up'?PAL.up:PAL.speed;const t=tOf(v),lit=Math.round(N*t);
      segEls.forEach((s,i)=>s.setAttribute('fill',i<lit?grad3(pal,i/(N-1)):'var(--track)'));
      needle.style.transform=`rotate(${A0+(A1-A0)*t}deg)`;
      $('#spNum').textContent=v>=1000?v.toFixed(0):v>=100?v.toFixed(1):v.toFixed(2);
      if(phase!=null){$('#spPh').innerHTML=phase}host.style.setProperty('--sc',kind==='up'?'var(--violet)':'var(--cyan)')},
    reset(){marks=SPEED_MARKS.slice();build()}};
}

/* ============================================================ canvas chart */
function Chart(cv,opts){
  const ctx=cv.getContext('2d');let data=null;
  function draw(){
    if(!data)return;const dpr=window.devicePixelRatio||1,w=cv.clientWidth,h=cv.clientHeight;if(!w||!h)return;
    if(cv.width!==Math.round(w*dpr)||cv.height!==Math.round(h*dpr)){cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr)}
    ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
    const L=56,Rr=10,T=10,B=22,pw=w-L-Rr,ph=h-T-B;
    let max=opts.max||0;if(!opts.max){data.series.forEach(s=>s.values.forEach(v=>{if(v>max)max=v}));max=max<=0?1:niceMax(max)}
    ctx.font='11px '+cssv('--mono');ctx.fillStyle=cssv('--dim');ctx.strokeStyle=cssv('--line');ctx.lineWidth=1;
    for(let i=0;i<=4;i++){const y=T+ph*i/4;ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(L,y);ctx.lineTo(L+pw,y);ctx.stroke();ctx.setLineDash([]);ctx.textAlign='right';ctx.fillText(opts.fmt(max*(1-i/4)),L-8,y+4)}
    const n=data.n||Math.max(...data.series.map(s=>s.values.length),2);
    if(opts.xlabels){ctx.textAlign='center';opts.xlabels(n).forEach(([i,t])=>ctx.fillText(t,L+pw*i/(n-1),h-6))}
    data.series.forEach(s=>{const v=s.values;if(!v.length)return;const col=s.color;
      const X=i=>L+pw*(s.x?s.x[i]/100:(n-v.length+i)/(n-1)),Y=x=>T+ph*(1-Math.min(x,max)/max);
      if(s.dots){ctx.fillStyle=col;v.forEach((x,i)=>{ctx.beginPath();ctx.arc(X(i),Y(x),2,0,7);ctx.fill()});return}
      ctx.beginPath();v.forEach((x,i)=>i?ctx.lineTo(X(i),Y(x)):ctx.moveTo(X(i),Y(x)));
      if(s.fill){const g=ctx.createLinearGradient(0,T,0,T+ph);g.addColorStop(0,col+'55');g.addColorStop(1,col+'00');ctx.save();ctx.lineTo(X(v.length-1),T+ph);ctx.lineTo(X(0),T+ph);ctx.closePath();ctx.fillStyle=g;ctx.fill();ctx.restore();ctx.beginPath();v.forEach((x,i)=>i?ctx.lineTo(X(i),Y(x)):ctx.moveTo(X(i),Y(x)))}
      ctx.strokeStyle=col;ctx.lineWidth=s.width||2;ctx.shadowColor=col;ctx.shadowBlur=s.thin?0:8*parseFloat(cssv('--glow')||1);ctx.globalAlpha=s.alpha||1;ctx.stroke();ctx.shadowBlur=0;ctx.globalAlpha=1});
  }
  new ResizeObserver(draw).observe(cv);
  return{set(d){data=d;draw()},draw};
}
function niceMax(v){const p=Math.pow(10,Math.floor(Math.log10(v)));const f=v/p;return (f<=1?1:f<=2?2:f<=2.5?2.5:f<=5?5:10)*p}
const timeLabels=n=>[[0,`-${n}s`],[Math.round((n-1)/2),`-${Math.round(n/2)}s`],[n-1,'now']];

/* ============================================================ settings */
const SET={theme:lsGet('theme','dark'),refresh:lsGet('refresh',1000),units:lsGet('units','bits'),glow:lsGet('glow',100)};
function applyTheme(){let t=SET.theme;if(t==='system')t=matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';document.documentElement.dataset.theme=t;$('#themeBtn').innerHTML=icon(t==='dark'?'sun':'moon');document.documentElement.style.setProperty('--glow',(t==='dark'?1:.35)*SET.glow/100);$$('#stTheme button').forEach(b=>b.classList.toggle('on',b.dataset.v===SET.theme));Object.values(CHARTS).forEach(c=>c.draw())}
const CHARTS={};
$('#themeBtn').onclick=()=>{SET.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';lsSet('theme',SET.theme);applyTheme()};
matchMedia('(prefers-color-scheme: light)').addEventListener('change',()=>{if(SET.theme==='system')applyTheme()});

/* ============================================================ navigation */
const PAGES=[
 {id:'dashboard',t:'Dashboard',ic:'dash',sub:'Live system overview',grp:'Monitor'},
 {id:'resources',t:'Resources',ic:'chart',sub:'Rolling 3-minute history'},
 {id:'processes',t:'Processes',ic:'cpu',sub:'Running processes'},
 {id:'services',t:'Services',ic:'gear',sub:'systemd services'},
 {id:'startup',t:'Startup Apps',ic:'rocket',sub:'Applications launched at login',grp:'Optimize'},
 {id:'cleaner',t:'System Cleaner',ic:'broom',sub:'Reclaim disk space'},
 {id:'packages',t:'Uninstaller',ic:'box',sub:'APT, Snap and Flatpak applications'},
 {id:'repos',t:'APT Repositories',ic:'repo',sub:'Software sources'},
 {id:'disks',t:'Disk Utility',ic:'disk',sub:'Drives, partitions, SMART, images and benchmark',grp:'Tools'},
 {id:'recovery',t:'Data Recovery',ic:'life',sub:'TestDisk & PhotoRec functions — lost partitions, boot repair, undelete, file carving'},
 {id:'docker',t:'Docker',ic:'docker',sub:'Install, configure and manage containers'},
 {id:'dupes',t:'Find & Duplicates',ic:'copy',sub:'Duplicate files, copies of a file, and file/text search'},
 {id:'speed',t:'Speed Test',ic:'gauge',sub:'Internet bandwidth and latency'},
 {id:'settings',t:'Settings',ic:'gear',sub:'Preferences',grp:'App'},
 {id:'help',t:'Help',ic:'help',sub:'How each section works'},
 {id:'about',t:'About',ic:'info',sub:'Version and credits'},
];
$('#nav').innerHTML=PAGES.map(p=>(p.grp?`<div class="grp">${p.grp}</div>`:'')+`<a data-p="${p.id}">${icon(p.ic)}<span>${p.t}</span></a>`).join('');
let CUR=null;const INIT={},SHOW={},HIDE={};
function go(id){
  if(CUR&&HIDE[CUR])HIDE[CUR]();CUR=id;const p=PAGES.find(x=>x.id===id);
  $$('.nav a').forEach(a=>a.classList.toggle('on',a.dataset.p===id));$$('.page').forEach(s=>s.classList.toggle('on',s.id==='p-'+id));
  $('#pgTitle').textContent=p.t;$('#pgSub').textContent=p.sub;lsSet('page',id);
  if(INIT[id]){INIT[id]();delete INIT[id]}if(SHOW[id])SHOW[id]();
}
$('#nav').addEventListener('click',e=>{const a=e.target.closest('a');if(a)go(a.dataset.p)});
document.addEventListener('keydown',e=>{if(e.target.matches('input,textarea,select')||$('#modalBg').classList.contains('on'))return;if(e.key==='t'||e.key==='T'){$('#themeBtn').click()}else if(/^[1-9]$/.test(e.key)){go(PAGES[+e.key-1].id)}else if(e.key==='/'){const s=$('.page.on input[type=text]');if(s){e.preventDefault();s.focus()}}});
setInterval(()=>{const d=new Date();$('#clockPill').textContent=d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})},1000);

/* ============================================================ dashboard */
let G={},SYS=null,LAST=null;
function initDashboard(){
  G.cpu=RingGauge($('#gCpu'),{label:'CPU LOAD',pal:'blue',icon:'cpu'});
  G.mem=SegGauge($('#gMem'),{label:'MEMORY',pal:'green'});
  G.io=SegGauge($('#gIo'),{label:'DISK I/O',pal:'violet',n:44});
  G.up=DigitPanel($('#dpUptime'),[{l:'DAYS',c:'var(--cyan)'},{l:'HOURS',c:'var(--cyan)'},{l:'MINUTES',c:'var(--green)'},{l:'SECONDS',c:'var(--violet)'}]);
  G.down=Hud($('#hudDown'),{label:'NETWORK ↓ DOWNLOAD',c:'#22d3ee'});G.upl=Hud($('#hudUp'),{label:'NETWORK ↑ UPLOAD',c:'#a78bfa'});
  G.peak={rx:1e6,tx:1e6,io:1e7};
}
let MINIS=[];
function renderDash(s){
  if(!s||!s.mem)return;if(!G.cpu)initDashboard();
  const fr=s.freq_mhz?(s.freq_mhz/1000).toFixed(2)+' GHZ':'';
  G.cpu.set(s.cpu,`${s.cpu.toFixed(0)}<tspan class="g-unit">%</tspan>`,[fr,s.cpu_temp!=null?s.cpu_temp.toFixed(0)+'°C':''].filter(Boolean).join(' · '));
  G.mem.set(s.mem.pct,`${s.mem.pct.toFixed(0)}<tspan class="g-unit">%</tspan>`,`${fmtB(s.mem.used)} / ${fmtB(s.mem.total)}`);
  const io=s.disk_io.read_Bps+s.disk_io.write_Bps;G.peak.io=Math.max(G.peak.io*0.995,io,1e7);
  {const f=fmtSI(io,io>=1e3&&io/Math.pow(1000,Math.floor(Math.log10(io)/3))>=100?0:1).split(' ');G.io.set(100*io/G.peak.io,`${f[0]}<tspan class="g-unit"> ${f[1]}/s</tspan>`,`R ${fmtSI(s.disk_io.read_Bps)}/s · W ${fmtSI(s.disk_io.write_Bps)}/s`)}
  const up=Math.floor(s.uptime);G.up.set([pad(Math.floor(up/86400)),pad(Math.floor(up%86400/3600)),pad(Math.floor(up%3600/60)),pad(up%60)]);
  const nc=s.cores.length||1;$('#loadTxt').textContent=`${s.load.map(x=>x.toFixed(2)).join('  ')}`;$('#loadBar').style.width=Math.min(100,100*s.load[0]/nc)+'%';
  $('#procTxt').textContent=`${s.procs} total · ${s.running} running`;$('#tempTxt').textContent=s.cpu_temp!=null?s.cpu_temp.toFixed(1)+' °C':'n/a';
  $('#rootPct').textContent=s.root.pct.toFixed(1)+'%';$('#rootBar').style.width=s.root.pct+'%';$('#rootBar').parentElement.classList.toggle('warn',s.root.pct>90);$('#rootSub').textContent=`${fmtB(s.root.used)} used of ${fmtB(s.root.total)} · ${fmtB(s.root.free)} free`;
  $('#swapPct').textContent=s.swap.total?s.swap.pct.toFixed(1)+'%':'off';$('#swapBar').style.width=s.swap.pct+'%';$('#swapSub').textContent=s.swap.total?`${fmtB(s.swap.used)} used of ${fmtB(s.swap.total)}`:'No swap configured';
  {const c=s.mem.cached+s.mem.buffers,p=s.mem.total?100*c/s.mem.total:0;$('#cachePct').textContent=p.toFixed(1)+'%';$('#cacheBar').style.width=p+'%';$('#cacheSub').textContent=`${fmtB(c)} reclaimable · ${fmtB(s.mem.avail)} available`}
  G.peak.rx=Math.max(G.peak.rx*0.995,s.net.rx_bps);G.peak.tx=Math.max(G.peak.tx*0.995,s.net.tx_bps);
  const d=splitRate(s.net.rx_bps),u=splitRate(s.net.tx_bps);
  G.down.set(d[0],d[1],s.net.rx_bps/G.peak.rx,`${fmtSI(s.net.rx_total)} received since boot`);G.upl.set(u[0],u[1],s.net.tx_bps/G.peak.tx,`${fmtSI(s.net.tx_total)} sent since boot`);
  if(MINIS.length!==s.cores.length){const h=$('#minis');h.innerHTML='';MINIS=s.cores.map((_,i)=>{const e=document.createElement('div');e.className='mini';h.appendChild(e);return MiniRing(e,{label:'CORE '+i,pal:['blue','green','violet','cyan'][i%4],icon:'cpu'})});$('#coreCount').textContent=s.cores.length+' logical'}
  s.cores.forEach((c,i)=>MINIS[i].set(c,c.toFixed(0)));
  $('#sFreq').textContent=s.freq_mhz?(s.freq_mhz/1000).toFixed(2)+' GHz':'n/a';$('#sNetTot').textContent=`↓ ${fmtSI(s.net.rx_total)} · ↑ ${fmtSI(s.net.tx_total)}`;
  $('#sBar').style.width=Math.min(100,100-s.cpu)+'%';
}
async function checkInstances(){try{const r=await api('/api/instances');if(!r.others.length)return;
  const rows=r.others.map(o=>`<li class="mono">pid ${o.pid} · port ${o.ports.join(', ')||'?'}${o.started?` · started ${new Date(o.started*1000).toLocaleString()}`:''}</li>`).join('');
  if(await confirmBox('Other MySysPilot servers running',`${r.others.length} other MySysPilot server process(es) are running besides this one (pid ${r.self.pid}, port ${r.self.port}, v${esc(r.self.version)}):<ul>${rows}</ul><div class="infobox">They are usually left over from earlier launches (when port 8787 is busy each launch picks a random port). Each keeps its own job list, so an operation started in another window does not show here. Closing them stops any operation still running in them.</div>`,'Close the others',true)){
    for(const o of r.others){try{await api('/api/instance/stop',{pid:o.pid})}catch(e){terr(e)}}toast(`Closed ${r.others.length} other server(s)`,'ok')}}catch(e){}}
async function loadSys(){try{SYS=await api('/api/sysinfo');$('#hostPill').innerHTML=`${icon('host')}&nbsp;${esc(SYS.hostname)}`;$('#hostPill svg').style.cssText='width:14px;height:14px';
  $('#sHost').textContent=SYS.hostname;$('#sOs').textContent=SYS.os;$('#sKernel').textContent=SYS.kernel;$('#sDesk').textContent=(SYS.desktop||'—')+(SYS.session?' · '+SYS.session:'');$('#cpuModelS').textContent=SYS.cpu_model.replace(/\(R\)|\(TM\)|CPU|Processor/g,'').replace(/\s+/g,' ').trim().slice(0,38);
  $('#sHost').title=SYS.hostname;$('#sOs').title=SYS.os}catch(e){terr(e)}}

/* ============================================================ resources */
let HIST=[];
function initResources(){
  CHARTS.cpu=Chart($('#chCpu'),{max:100,fmt:v=>v.toFixed(0)+'%',xlabels:timeLabels});
  CHARTS.mem=Chart($('#chMem'),{max:100,fmt:v=>v.toFixed(0)+'%',xlabels:timeLabels});
  CHARTS.net=Chart($('#chNet'),{fmt:v=>fmtBits(v).replace('/s',''),xlabels:timeLabels});
  CHARTS.io=Chart($('#chIo'),{fmt:v=>fmtSI(v,0),xlabels:timeLabels});
}
const CORE_COLS=['#22d3ee','#a78bfa','#1ee39a','#fbbf24','#f472b6','#60a5fa','#34d399','#f97316','#e879f9','#2dd4bf','#facc15','#818cf8'];
function renderResources(){
  if(!CHARTS.cpu||!HIST.length)return;const n=180;
  const cores=(HIST[HIST.length-1].cores||[]).map((_,i)=>({values:HIST.map(p=>p.cores[i]||0),color:CORE_COLS[i%CORE_COLS.length],width:1.1,thin:true,alpha:.65}));
  CHARTS.cpu.set({n,series:[...cores,{values:HIST.map(p=>p.cpu),color:'#3b82f6',fill:true,width:2.4}]});
  $('#lgCpu').innerHTML='<span><i style="background:#3b82f6"></i>Total</span>'+cores.slice(0,12).map((c,i)=>`<span><i style="background:${c.color}"></i>${i}</span>`).join('');
  CHARTS.mem.set({n,series:[{values:HIST.map(p=>p.mem),color:'#1ee39a',fill:true},{values:HIST.map(p=>p.swap),color:'#a78bfa',fill:true}]});
  const k=SET.units==='bytes'?1:1;CHARTS.net.set({n,series:[{values:HIST.map(p=>p.rx*k),color:'#22d3ee',fill:true},{values:HIST.map(p=>p.tx*k),color:'#a78bfa',fill:true}]});
  CHARTS.io.set({n,series:[{values:HIST.map(p=>p.dr),color:'#3b82f6',fill:true},{values:HIST.map(p=>p.dw),color:'#fbbf24',fill:true}]});
}
let SENSORS=[];
function renderSensors(s){
  const host=$('#sensors');if(!s.temps||!s.temps.length){host.innerHTML='<div class="empty" style="grid-column:1/-1">No hwmon temperature sensors exposed (common in virtual machines).</div>';SENSORS=[];return}
  if(SENSORS.length!==s.temps.length){host.innerHTML='';SENSORS=s.temps.map(t=>{const e=document.createElement('div');e.className='mini';e.innerHTML='<div></div><div class="muted" style="font-size:11.5px;text-align:center;margin-top:4px"></div>';host.appendChild(e);const g=MiniRing(e.firstChild,{label:'°C',pal:'amber',icon:'temp'});e.lastChild.textContent=`${t.sensor} · ${t.label}`;return g})}
  s.temps.forEach((t,i)=>SENSORS[i].set(t.c,t.c.toFixed(0)));
}

/* ============================================================ polling */
let pollT=null;
async function poll(){
  try{const s=await api('/api/stats');setConn(true);LAST=s;
    if(CUR==='dashboard')renderDash(s);
    if(CUR==='resources'){const h=await api('/api/history');HIST=h.points;renderResources();renderSensors(s)}
  }catch(e){setConn(false)}
  pollT=setTimeout(poll,SET.refresh);
}

/* ============================================================ processes */
let PR={rows:[],sort:'cpu',dir:-1,sel:null,timer:null};
function initProcesses(){
  $$('#prTbl th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(PR.sort===k)PR.dir*=-1;else{PR.sort=k;PR.dir=(k==='name'||k==='user'||k==='cmd')?1:-1}renderPr()});
  $('#prSearch').oninput=renderPr;$('#prMine').onchange=renderPr;
  $('#prTbl tbody').onclick=e=>{const tr=e.target.closest('tr');if(!tr)return;PR.sel=+tr.dataset.pid;renderPr()};
  $$('[data-sig]').forEach(b=>b.onclick=async()=>{const p=PR.rows.find(r=>r.pid===PR.sel);if(!p)return;const sig=b.dataset.sig;
    if(sig==='KILL'||sig==='TERM'){if(!await confirmBox(`${sig==='KILL'?'Kill':'End'} process`,`Send SIG${sig} to <b>${esc(p.name)}</b> (PID ${p.pid}, user ${esc(p.user)})?`,sig==='KILL'?'Kill':'End process',true))return}
    try{const r=await api('/api/process/signal',{pid:p.pid,signal:sig});toast(`SIG${sig} sent to ${esc(p.name)}${r.elevated?' (elevated)':''}`,'ok');loadPr()}catch(e){terr(e)}});
  $('#prNice').onclick=async()=>{const p=PR.rows.find(r=>r.pid===PR.sel);if(!p)return;const r=await promptBox(`Priority — ${esc(p.name)}`,[{id:'nice',label:'Nice (-20 … 19)',type:'number',value:p.nice}],'Apply');if(!r)return;try{await api('/api/process/renice',{pid:p.pid,nice:+r.nice});toast('Priority changed','ok');loadPr()}catch(e){terr(e)}};
}
async function loadPr(){try{const r=await api('/api/processes');PR.rows=r.processes;PR.uid=r.my_uid;renderPr()}catch(e){terr(e)}}
function renderPr(){
  const q=$('#prSearch').value.toLowerCase(),mine=$('#prMine').checked;
  let rows=PR.rows.filter(r=>(!mine||r.uid===PR.uid)&&(!q||(r.name+' '+r.user+' '+r.pid+' '+r.cmd).toLowerCase().includes(q)));
  const k=PR.sort,d=PR.dir;rows.sort((a,b)=>(a[k]>b[k]?1:a[k]<b[k]?-1:0)*d);
  $('#prCount').textContent=`${rows.length} of ${PR.rows.length}`;
  const st={R:'g',S:'',D:'a',Z:'r',T:'v',I:''};
  $('#prTbl tbody').innerHTML=rows.slice(0,600).map(r=>`<tr data-pid="${r.pid}" class="${r.pid===PR.sel?'sel':''}"><td class="num">${r.pid}</td><td><b>${esc(r.name)}</b></td><td>${esc(r.user)}</td><td><span class="badge ${st[r.state]||''}">${r.state}</span></td><td class="num" style="color:${r.cpu>50?'var(--red)':r.cpu>10?'var(--amber)':'inherit'}">${r.cpu.toFixed(1)}</td><td class="num">${fmtB(r.rss)}</td><td class="num">${r.threads}</td><td class="num">${r.nice}</td><td class="trunc mono dim" title="${esc(r.cmd)}">${esc(r.cmd)}</td></tr>`).join('');
  $$('#prTbl th').forEach(th=>{th.style.color=th.dataset.k===k?'var(--cyan)':''});
  const has=PR.rows.some(r=>r.pid===PR.sel);$$('[data-sig],#prNice').forEach(b=>b.disabled=!has);
}
SHOW.processes=()=>{loadPr();PR.timer=setInterval(loadPr,Math.max(2000,SET.refresh*2))};HIDE.processes=()=>clearInterval(PR.timer);

/* ============================================================ services */
let SV={rows:[],f:'running'};
function initServices(){
  $('#svSearch').oninput=renderSv;$('#svReload').onclick=loadSv;
  $('#svTabs').onclick=e=>{const b=e.target.closest('button');if(!b)return;SV.f=b.dataset.f;$$('#svTabs button').forEach(x=>x.classList.toggle('on',x===b));renderSv()};
  $('#svTbl tbody').onclick=async e=>{const b=e.target.closest('button[data-a]');if(!b)return;const name=b.closest('tr').dataset.n,a=b.dataset.a;
    if((a==='stop'||a==='disable')&&!await confirmBox(`${a[0].toUpperCase()+a.slice(1)} service`,`${a} <b class="mono">${esc(name)}</b>? Stopping core services can break your session.`,a,true))return;
    b.disabled=true;b.innerHTML='<span class="spin"></span>';try{await api('/api/service',{name,action:a});toast(`${esc(name)}: ${a} OK`,'ok')}catch(e){terr(e)}loadSv()};
}
async function loadSv(){try{const r=await api('/api/services');SV.rows=r.services;renderSv()}catch(e){terr(e);$('#svTbl tbody').innerHTML=`<tr><td colspan="5" class="empty">${esc(e.message)}</td></tr>`}}
function renderSv(){
  const q=$('#svSearch').value.toLowerCase();
  const rows=SV.rows.filter(s=>(SV.f==='all'||(SV.f==='running'&&s.active==='active')||(SV.f==='failed'&&s.active==='failed')||(SV.f==='enabled'&&s.enabled==='enabled'))&&(!q||(s.name+' '+s.description).toLowerCase().includes(q)));
  $('#svCount').textContent=`${rows.length} services`;
  $('#svTbl tbody').innerHTML=rows.map(s=>{const act=s.active==='active',fail=s.active==='failed',en=s.enabled==='enabled',static_=/static|indirect|generated|transient|alias/.test(s.enabled||'');
    return `<tr data-n="${esc(s.name)}"><td><b class="mono">${esc(s.name.replace(/\.service$/,''))}</b></td><td class="trunc muted">${esc(s.description)}</td><td><span class="badge ${act?'g':fail?'r':''}">${esc(s.active)} · ${esc(s.sub)}</span></td><td><span class="badge ${en?'b':s.enabled==='masked'?'r':''}">${esc(s.enabled||'—')}</span></td>
    <td style="text-align:right;white-space:nowrap">${act?`<button class="btn sm ghost" data-a="restart">Restart</button> <button class="btn sm danger" data-a="stop">Stop</button>`:`<button class="btn sm" data-a="start">Start</button>`} ${static_?'':en?`<button class="btn sm ghost" data-a="disable">Disable</button>`:s.enabled==='masked'?`<button class="btn sm ghost" data-a="unmask">Unmask</button>`:`<button class="btn sm ghost" data-a="enable">Enable</button>`}</td></tr>`}).join('')||'<tr><td colspan="5" class="empty">No services match.</td></tr>';
}
SHOW.services=loadSv;

/* ============================================================ startup */
let SU=[];
function initStartup(){
  $('#suAll').onchange=renderSu;
  $('#suAdd').onclick=async()=>{const r=await promptBox('Add startup application',[{id:'name',label:'Name',ph:'My tool'},{id:'exec',label:'Command',ph:'/usr/bin/app --flag'},{id:'comment',label:'Comment',ph:'optional'}],'Add');if(!r)return;try{await api('/api/startup/add',r);toast('Startup entry added','ok');loadSu()}catch(e){terr(e)}};
  $('#suList').addEventListener('change',async e=>{const c=e.target.closest('input[data-f]');if(!c)return;try{await api('/api/startup/toggle',{file:c.dataset.f,enabled:c.checked});toast(`${c.checked?'Enabled':'Disabled'} ${esc(c.dataset.f)}`,'ok')}catch(err){terr(err);c.checked=!c.checked}});
  $('#suList').addEventListener('click',async e=>{const b=e.target.closest('button[data-del]');if(!b)return;if(!await confirmBox('Remove entry',`Delete <b class="mono">${esc(b.dataset.del)}</b> from your autostart folder?`,'Remove',true))return;try{await api('/api/startup/delete',{file:b.dataset.del});loadSu()}catch(err){terr(err)}});
}
async function loadSu(){try{SU=(await api('/api/startup')).entries;renderSu()}catch(e){terr(e)}}
function renderSu(){const all=$('#suAll').checked;const rows=SU.filter(s=>all||s.applies);
  $('#suList').innerHTML=rows.map(s=>`<div class="si"><div class="av" style="${s.applies?'':'filter:grayscale(1);opacity:.5'}">${esc((s.name||'?')[0].toUpperCase())}</div><div class="tx"><b>${esc(s.name)}</b><small title="${esc(s.exec)}">${esc(s.exec)}</small><small style="font-family:var(--font)">${s.system?'<span class="badge">system</span> ':''}${s.user?'<span class="badge b">user</span>':''}${s.applies?'':' <span class="badge a">other desktop</span>'}</small></div>${s.user&&!s.system?`<button class="btn ico ghost" data-del="${esc(s.file)}" title="Remove">${icon('trash')}</button>`:''}<label class="sw"><input type="checkbox" data-f="${esc(s.file)}"${s.enabled?' checked':''}><span></span></label></div>`).join('')||'<div class="empty">No autostart entries.</div>'}
SHOW.startup=loadSu;

/* ============================================================ cleaner */
const CL_META={apt:['APT package cache','box'],crash:['Crash reports','bolt'],logs:['Rotated system logs','list'],journal:['systemd journal','repo'],cache:['Application caches','copy'],thumbs:['Thumbnails','image'],trash:['Trash','trash'],snap:['Old snap revisions','box']};
let CL={data:null,catSel:new Set(['apt','crash','logs','thumbs','trash','snap']),itemSel:new Set(),view:null};
function initCleaner(){
  G.clean=RingGauge($('#gClean'),{label:'SELECTED',pal:'green',icon:'broom'});G.clean.set(0,'0<tspan class="g-unit"> B</tspan>','RUN A SCAN');
  $('#clScan').onclick=scanClean;
  $('#clCats').onclick=e=>{const c=e.target.closest('.cat');if(!c)return;const k=c.dataset.k;
    if(e.target.matches('input')){if(e.target.checked)CL.catSel.add(k);else CL.catSel.delete(k);(CL.data.categories[k]||[]).forEach(i=>e.target.checked?CL.itemSel.add(i.id):CL.itemSel.delete(i.id))}
    CL.view=k;renderCl()};
  $('#clItems').onchange=e=>{const c=e.target.closest('input[data-id]');if(!c)return;c.checked?CL.itemSel.add(c.dataset.id):CL.itemSel.delete(c.dataset.id);renderClSel()};
  $('#clAllItems').onchange=e=>{(CL.data.categories[CL.view]||[]).forEach(i=>e.target.checked?CL.itemSel.add(i.id):CL.itemSel.delete(i.id));renderCl()};
  $('#clClean').onclick=async()=>{const n=CL.itemSel.size;if(!n)return;const sz=selSize();
    if(!await confirmBox('Clean system',`Remove <b>${n}</b> item(s), about <b>${fmtB(sz)}</b>? Items marked as system require your password once.`,'Clean now',true))return;
    $('#clClean').disabled=true;try{const {job}=await api('/api/cleaner/clean',{ids:[...CL.itemSel]});const j=await waitJob(job,j=>G.clean.set(j.progress*100,`${Math.round(j.progress*100)}<tspan class="g-unit">%</tspan>`,j.message.toUpperCase().slice(0,28)));
      toast(`Freed ${fmtB(j.result.freed)}`+(j.result.failed.length?` · ${j.result.failed.length} failed`:''),j.result.failed.length?'err':'ok');if(j.result.failed.length)openModal('Some items were not removed',`<pre class="out">${esc(j.result.failed.join('\n'))}</pre>`)}catch(e){terr(e)}scanClean()};
}
function selSize(){if(!CL.data)return 0;let s=0;Object.values(CL.data.categories).forEach(a=>a.forEach(i=>{if(CL.itemSel.has(i.id))s+=i.size}));return s}
async function scanClean(){$('#clScan').disabled=true;$('#clScan').innerHTML='<span class="spin"></span> Scanning…';
  try{CL.data=await api('/api/cleaner/scan',{});CL.itemSel.clear();Object.entries(CL.data.categories).forEach(([k,a])=>{if(CL.catSel.has(k))a.forEach(i=>CL.itemSel.add(i.id))});if(!CL.view||!(CL.data.categories[CL.view]||[]).length){CL.view=Object.keys(CL_META).find(k=>(CL.data.categories[k]||[]).length)||'apt'}renderCl()}catch(e){terr(e)}
  $('#clScan').disabled=false;$('#clScan').innerHTML=icon('search')+'Rescan'}
function renderCl(){if(!CL.data){$('#clCats').innerHTML=Object.entries(CL_META).map(([k,m])=>`<div class="cat" data-k="${k}"><div class="nm">${icon(m[1])}${m[0]}</div><div class="sz">—</div><div class="ct">not scanned</div></div>`).join('');return}
  const s=CL.data.summary;$('#clCats').innerHTML=Object.entries(CL_META).map(([k,m])=>`<div class="cat ${CL.view===k?'on':''}" data-k="${k}"><input type="checkbox"${CL.catSel.has(k)?' checked':''}><div class="nm">${icon(m[1])}${m[0]}</div><div class="sz">${fmtB(s[k].size)}</div><div class="ct">${s[k].count} item(s)${k==='cache'?' · <span style="color:var(--amber)">review</span>':''}</div></div>`).join('');
  const items=CL.data.categories[CL.view]||[];$('#clItems').innerHTML=items.length?items.map(i=>`<div class="item"><input type="checkbox" data-id="${i.id}"${CL.itemSel.has(i.id)?' checked':''}><span class="p" title="${esc(i.path)}">${esc(i.label)}</span>${i.root?'<span class="badge a">system</span>':''}<span class="s">${fmtB(i.size)}</span></div>`).join(''):'<div class="empty">Nothing to clean here.</div>';
  renderClSel()}
function renderClSel(){const sz=selSize(),tot=Object.values(CL.data.summary).reduce((a,b)=>a+b.size,0);const [v,u]=fmtB(sz).split(' ');G.clean.set(tot?100*sz/tot:0,`${v}<tspan class="g-unit"> ${u||''}</tspan>`,`${CL.itemSel.size} ITEMS · ${fmtB(tot)} FOUND`);$('#clSel').textContent=`${CL.itemSel.size} selected`;$('#clClean').disabled=!CL.itemSel.size}
SHOW.cleaner=()=>{if(!CL.data)renderCl()};

/* ============================================================ packages */
let PK={kind:'apt',rows:[],sel:new Set(),sort:'name',dir:1};
function initPackages(){
  $('#pkTabs').onclick=e=>{const b=e.target.closest('button');if(!b)return;PK.kind=b.dataset.k;$$('#pkTabs button').forEach(x=>x.classList.toggle('on',x===b));$('#pkManualL').style.display=PK.kind==='apt'?'':'none';$('#pkAuto').style.display=PK.kind==='apt'?'':'none';loadPk()};
  $('#pkSearch').oninput=renderPk;$('#pkManual').onchange=renderPk;
  $$('#pkTbl th[data-k]').forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(PK.sort===k)PK.dir*=-1;else{PK.sort=k;PK.dir=k==='size'?-1:1}renderPk()});
  $('#pkTbl tbody').onchange=e=>{const c=e.target.closest('input[data-n]');if(!c)return;c.checked?PK.sel.add(c.dataset.n):PK.sel.delete(c.dataset.n);$('#pkRemove').disabled=!PK.sel.size};
  $('#pkAll').onchange=e=>{filtPk().forEach(r=>e.target.checked?PK.sel.add(r.name):PK.sel.delete(r.name));renderPk()};
  $('#pkRemove').onclick=async()=>{const names=[...PK.sel];const html=`Remove ${names.length} ${PK.kind} package(s)?<pre class="out">${esc(names.join('\n'))}</pre>${PK.kind==='apt'?'<label class="chk"><input type="checkbox" id="pkPurge"> Purge configuration files too</label><div class="warnbox">APT may also remove packages that depend on these. Review the output.</div>':''}`;
    let purge=false;const ok=await new Promise(res=>{let d=false;openModal('Remove packages',html,[{label:'Cancel'},{label:'Remove',cls:'danger',onClick:()=>{purge=$('#pkPurge')?$('#pkPurge').checked:false;d=true;res(true)}}],()=>{if(!d)res(false)})});if(!ok)return;
    const inst=(PK.rows.find(r=>r.name===names[0])||{}).installation;
    runCmdJob('Removing packages',api('/api/packages/remove',{kind:PK.kind,names,purge,installation:inst}),()=>{PK.sel.clear();loadPk()})};
  $('#pkAuto').onclick=async()=>{if(!await confirmBox('Autoremove','Run <code class="mono">apt-get autoremove</code> to remove automatically installed packages that are no longer needed?','Run autoremove',true))return;runCmdJob('apt autoremove',api('/api/packages/autoremove',{}),loadPk)};
}
async function runCmdJob(title,startPromise,after){
  openModal(title,'<div class="row"><span class="spin"></span><span id="cjMsg">Starting…</span></div><pre class="out" id="cjOut" style="display:none;max-height:50vh"></pre>',[{label:'Close'}]);
  try{const {job}=await startPromise;const j=await waitJob(job,j=>{const m=$('#cjMsg');if(m)m.textContent=j.message;const L=j.live&&j.live.log,o=$('#cjOut');if(L&&L.length&&o){o.style.display='';o.textContent=L.join('\n');o.scrollTop=o.scrollHeight}});$('#cjMsg').parentElement.innerHTML=`<span class="badge g">Completed</span>`;const o=$('#cjOut');if(o){o.style.display='';o.textContent=(j.result&&j.result.output)||'(no output)';o.scrollTop=o.scrollHeight}toast(title+' — done','ok')}
  catch(e){const m=$('#cjMsg');if(m)m.parentElement.innerHTML=`<pre class="warnbox" style="width:100%;white-space:pre-wrap;margin:0">${esc(e.message)}</pre>`;terr(e)}
  if(after)after();
}
async function loadPk(){$('#pkTbl tbody').innerHTML='<tr><td colspan="5" class="empty"><span class="spin"></span></td></tr>';PK.sel.clear();$('#pkRemove').disabled=true;
  try{PK.rows=(await api('/api/packages?kind='+PK.kind)).packages;renderPk()}catch(e){$('#pkTbl tbody').innerHTML=`<tr><td colspan="5" class="empty">${esc(e.message)}</td></tr>`}}
function filtPk(){const q=$('#pkSearch').value.toLowerCase(),man=$('#pkManual').checked&&PK.kind==='apt';return PK.rows.filter(r=>(!man||r.manual)&&(!q||(r.name+' '+(r.summary||'')).toLowerCase().includes(q)))}
function renderPk(){let rows=filtPk();const k=PK.sort,d=PK.dir;rows.sort((a,b)=>(a[k]>b[k]?1:a[k]<b[k]?-1:0)*d);$('#pkCount').textContent=`${rows.length} packages`;
  $('#pkTbl tbody').innerHTML=rows.slice(0,1500).map(r=>`<tr><td><input type="checkbox" data-n="${esc(r.name)}"${PK.sel.has(r.name)?' checked':''}></td><td><b>${esc(r.name)}</b>${r.notes&&r.notes!=='-'?` <span class="badge">${esc(r.notes)}</span>`:''}</td><td class="mono dim">${esc(r.version)}</td><td class="num">${r.size?fmtB(r.size):esc(r.size_text||'—')}</td><td class="trunc muted">${esc(r.summary)}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">No packages.</td></tr>';$('#pkRemove').disabled=!PK.sel.size}
SHOW.packages=()=>{if(!PK.rows.length)loadPk()};

/* ============================================================ repos */
function initRepos(){$('#rpReload').onclick=loadRp;$('#rpTbl tbody').onchange=async e=>{const c=e.target.closest('input[data-i]');if(!c)return;const r=RP[+c.dataset.i];
  if(!await confirmBox(`${c.checked?'Enable':'Disable'} repository`,`<div class="mono">${esc(r.types)} ${esc(r.uri)} ${esc(r.suite)} ${esc(r.components)}</div><div class="muted" style="margin-top:8px">File: ${esc(r.file)} (backup is made first)</div>`,c.checked?'Enable':'Disable',!c.checked)){c.checked=!c.checked;return}
  try{await api('/api/repos/toggle',{file:r.file,index:r.index,enabled:c.checked});toast('Repository updated — run sudo apt update','ok')}catch(err){terr(err)}loadRp()}}
let RP=[];async function loadRp(){try{RP=(await api('/api/repos')).repos;$('#rpTbl tbody').innerHTML=RP.map((r,i)=>`<tr><td><label class="sw"><input type="checkbox" data-i="${i}"${r.enabled?' checked':''}><span></span></label></td><td><span class="badge ${r.types.includes('src')?'v':'b'}">${esc(r.types)}</span></td><td class="mono trunc" title="${esc(r.uri)}">${esc(r.uri)}</td><td class="mono">${esc(r.suite)}</td><td class="muted">${esc(r.components)}</td><td class="mono dim trunc">${esc(r.file.replace('/etc/apt/',''))}</td></tr>`).join('')||'<tr><td colspan="6" class="empty">No sources found.</td></tr>'}catch(e){terr(e)}}
SHOW.repos=loadRp;

/* ============================================================ disks (v1.1) */
let DK={list:[],sel:null,vol:null,meta:null,bk:[],tools:{},stamp:null,sig:null,busy:0,poll:null,loading:false};
const FS_COL={ext4:'#2563eb',ext3:'#2563eb',ext2:'#2563eb',btrfs:'#0891b2',xfs:'#0d9488',vfat:'#7c3aed',fat32:'#7c3aed',exfat:'#9333ea',ntfs:'#c026d3',swap:'#d97706',crypto_LUKS:'#e11d48',LVM2_member:'#65a30d',iso9660:'#475569',squashfs:'#64748b',f2fs:'#0284c7'};
const CRIT=['/','/boot','/boot/efi','/efi','/home','/usr','/var','/opt','/srv','[SWAP]'];
const NONFS=['swap','crypto_LUKS','LVM2_member','linux_raid_member','zfs_member'];
const isFs=n=>!!n&&((!!n.fstype&&!NONFS.includes(n.fstype))||(!n.fstype&&(n.mountpoints||[]).some(m=>m&&m!=='[SWAP]')));
const fsLeaves=n=>isFs(n)?[n]:(n.children||[]).flatMap(fsLeaves);
const isCrit=n=>allMounts(n).some(m=>CRIT.includes(m));
function initDisks(){$('#dkReload').onclick=()=>loadDk({settle:true,force:true,render:true});$('#dkSnap').onchange=()=>loadDk({render:true});
  $('#dkList').onclick=e=>{const c=e.target.closest('.dcard');if(!c)return;DK.sel=c.dataset.k;DK.vol=null;renderDkList();renderDkDetail()};
  $('#dkAttach').onclick=async()=>{const r=await promptBox('Attach disk image',[{id:'file',label:'Image file path',ph:'~/Downloads/ubuntu.iso'},{id:'ro',label:'Options',type:'check',value:true,text:'Read-only'}],'Attach');if(!r)return;try{const x=await api('/api/disk/action',{action:'attach_image',file:r.file,readonly:r.ro});toast(esc(x.message||'Attached'),'ok');loadDk()}catch(e){terr(e)}};
  $('#bkTbl').addEventListener('click',bkClick);$('#bkReload').onclick=loadBk;
  api('/api/meta').then(m=>{DK.tools=m.tools}).catch(()=>{});
}
async function loadDk(opt){opt=opt||{};if(DK.loading&&!opt.force)return;DK.loading=true;dkFresh('busy');
  try{const r=await api('/api/disks?'+(($('#dkSnap')&&$('#dkSnap').checked)?'snap=1&':'')+(opt.settle?'settle=1':''));DK.stamp=r.stamp;DK.meta=r;
    const sig=JSON.stringify(r.disks);const changed=sig!==DK.sig;DK.sig=sig;DK.list=r.disks;
    if(!DK.sel||!DK.list.find(d=>d.kname===DK.sel)){DK.sel=(DK.list.find(d=>d.type==='disk')||DK.list[0]||{}).kname;DK.vol=null}
    if(DK.vol&&!DK.vol.startsWith('free:')&&!findNode(DK.vol))DK.vol=null;
    if(changed||opt.render){const y=$('#content').scrollTop;renderDkList();renderDkDetail();$('#content').scrollTop=y}dkFresh('ok')}
  catch(e){$('#dkList').innerHTML=`<div class="empty">${esc(e.message)}</div>`;dkFresh('err')}finally{DK.loading=false}if(!opt.noBk)loadBk()}
function dkFresh(st){const el=$('#dkFresh');if(!el)return;const t=new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});el.innerHTML=st==='busy'?'<span class="spin" style="width:11px;height:11px"></span> refreshing…':st==='err'?'<span class="dot red"></span> refresh failed':`<span class="dot"></span> live · updated ${t}`}
async function dkPollTick(){if(CUR!=='disks'||DK.busy||DK.loading||document.hidden)return;DK.ticks=(DK.ticks||0)+1;const st=DK.list.some(d=>d.health&&d.health.smart&&d.health.smart.selftest_status==='inprogress');
  if((st&&DK.ticks%5===0)||DK.ticks%15===0){await loadDk({noBk:true,settle:st});return}try{const r=await api('/api/disks/stamp');if(DK.stamp&&r.stamp!==DK.stamp)await loadDk({noBk:true})}catch(e){}}
const DKX={"basics":[["A","Check that Docker works","Confirm the client and the daemon both answer, then run the official test image.",[["docker version","Client and Server versions. If 'Server' is missing, the daemon is stopped or you lack permission."],["docker info","Storage driver, number of containers/images, rootless mode, cgroup version."],["docker run --rm hello-world","Pulls a tiny image, runs it once, prints a message, then removes the container (--rm)."],["sudo usermod -aG docker $USER   # then log out and back in","Lets your user run docker without sudo. Membership in 'docker' is root-equivalent on this machine."]],[["Run hello-world",{"image":"hello-world"}]]],["B","Images: download, list, remove","An image is a read-only template. Containers are started from images.",[["docker pull nginx:alpine","Download an image. 'nginx' is the repository, 'alpine' the tag. No tag means ':latest'."],["docker images","List local images with size and age (same as 'docker image ls')."],["docker image inspect nginx:alpine","Full JSON metadata: layers, env, exposed ports, entrypoint."],["docker image rm nginx:alpine","Remove an image. Fails while a container still uses it."],["docker image prune","Remove dangling (untagged) images. Add -a to remove every image not used by a container."]],[]],["C","Containers: run, list, stop, remove","A container is a running (or stopped) instance of an image.",[["docker run -d --name web -p 127.0.0.1:8080:80 nginx:alpine","-d detached, --name a fixed name, -p host:container port. Binding to 127.0.0.1 keeps it off your LAN."],["docker ps","Running containers. 'docker ps -a' includes stopped ones."],["docker stop web && docker start web","Stop (SIGTERM, then SIGKILL after 10 s) and start again. State on the container's filesystem is kept."],["docker restart web","Stop + start in one step."],["docker rm -f web","Remove the container (-f stops it first). Data not in a volume is lost."],["docker run --rm -it alpine sh","Interactive throw-away shell: -i keep STDIN open, -t allocate a TTY, --rm delete on exit."]],[["Run nginx on 127.0.0.1:8080",{"image":"nginx:alpine","name":"web-demo","ports":["127.0.0.1:8080:80"]}]]],["D","Look inside: logs, exec, stats","Day-to-day troubleshooting commands.",[["docker logs -f --tail 100 web","Follow the last 100 lines of STDOUT/STDERR."],["docker exec -it web sh","Open a shell inside a running container (use bash if the image has it)."],["docker stats","Live CPU / memory / network / block I/O per container. Ctrl+C to quit."],["docker top web","Processes running inside the container."],["docker inspect web","Full JSON: IP address, mounts, restart policy, health."],["docker cp web:/etc/nginx/nginx.conf ./nginx.conf","Copy files between the container and the host (both directions)."]],[]],["E","Keep data: volumes and bind mounts","Container filesystems are disposable. Put data you want to keep in a volume or a host folder.",[["docker volume create appdata","Named volume managed by Docker (stored under /var/lib/docker/volumes)."],["docker run -d --name db -v appdata:/var/lib/postgresql/data -e POSTGRES_PASSWORD=change-me postgres:16","Mount the named volume at the database's data path. Replace the password."],["docker run --rm -v \"$PWD\":/work -w /work alpine ls -la","Bind mount: share the current host folder into the container."],["docker volume ls && docker volume inspect appdata","List volumes and show where one lives on disk."],["docker volume prune","Remove volumes not used by any container (asks first). Data is gone permanently."]],[]],["F","Networking","Containers on the same user-defined network reach each other by name.",[["docker network create appnet","User-defined bridge network with built-in DNS."],["docker run -d --name api --network appnet nginx:alpine","Attach at start time."],["docker run --rm --network appnet alpine wget -qO- http://api","Another container reaches 'api' by name."],["docker network connect appnet web","Attach an already running container."],["docker network ls && docker network inspect appnet","List networks / show members and subnet."]],[]],["G","Build your own image","A Dockerfile is the recipe. See the Dockerfile templates tab for ready-made ones.",[["docker build -t myapp:1.0 .","Build from the Dockerfile in the current folder and tag it myapp:1.0."],["docker run -d --name myapp -p 127.0.0.1:8000:8000 myapp:1.0","Run what you built."],["docker history myapp:1.0","Layers and the size each instruction added."],["docker tag myapp:1.0 registry.example.com/team/myapp:1.0","Add a registry name before pushing."],["docker login registry.example.com && docker push registry.example.com/team/myapp:1.0","Push to a registry. Credentials are stored in ~/.docker/config.json (use a credential helper)."]],[]],["H","Multi-container apps with Compose","Describe services, networks and volumes in compose.yaml. See the Compose templates tab.",[["docker compose up -d","Create and start everything in compose.yaml in the current folder."],["docker compose ps","Status of this project's services."],["docker compose logs -f","Follow logs of all services (add a service name to filter)."],["docker compose pull && docker compose up -d","Update: pull newer images, recreate changed containers."],["docker compose down","Stop and remove containers and the default network. Add -v to also delete volumes."]],[]],["I","Clean up disk space","Docker keeps stopped containers, old images and build cache until you remove them.",[["docker system df","How much space images, containers, volumes and build cache use."],["docker container prune","Remove all stopped containers."],["docker builder prune","Remove build cache."],["docker system prune","Stopped containers + unused networks + dangling images + build cache. Add -a for all unused images, --volumes for volumes."]],[]],["J","Restart policies and updates","Keep services running across reboots.",[["docker run -d --restart unless-stopped --name web nginx:alpine","Restart after crashes and reboots unless you stopped it yourself."],["docker update --restart unless-stopped web","Change the policy of an existing container."],["docker pull nginx:alpine && docker rm -f web && docker run -d --name web ... nginx:alpine","Manual update: pull, remove, re-create with the same options (Compose does this for you)."]],[]]],"dockerfiles":[{"id":"static","name":"Static website (nginx)","tag":"my-static-site","run":{"ports":["127.0.0.1:8080:80"]},"desc":"Serve a folder of HTML/CSS/JS with nginx on Alpine. Smallest useful image.","files":{"Dockerfile":"FROM nginx:alpine\n# Copy the site into nginx's default web root\nCOPY site/ /usr/share/nginx/html/\nEXPOSE 80\n","site/index.html":"<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>It works<\/title><\/head>\n<body style=\"font-family:sans-serif;padding:40px\"><h1>Hello from Docker<\/h1><p>Edit site/index.html and rebuild.<\/p><\/body><\/html>\n",".dockerignore":".git\n*.md\n"}},{"id":"python","name":"Python web app (Flask + gunicorn)","tag":"my-python-app","run":{"ports":["127.0.0.1:8000:8000"]},"desc":"Slim Python image, dependencies cached in their own layer, runs as a non-root user.","files":{"Dockerfile":"FROM python:3.12-slim\nENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\nWORKDIR /app\n# Install dependencies first so this layer is cached between code changes\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\n# Do not run as root inside the container\nRUN useradd --create-home --uid 10001 app\nUSER app\nEXPOSE 8000\nCMD [\"gunicorn\", \"--bind\", \"0.0.0.0:8000\", \"app:app\"]\n","requirements.txt":"flask==3.*\ngunicorn==23.*\n","app.py":"from flask import Flask\n\napp = Flask(__name__)\n\n\n@app.get(\"/\")\ndef index():\n    return {\"status\": \"ok\", \"message\": \"Hello from a Python container\"}\n",".dockerignore":"__pycache__/\n*.pyc\n.venv/\n.git\n"}},{"id":"node","name":"Node.js app (Express)","tag":"my-node-app","run":{"ports":["127.0.0.1:3000:3000"]},"desc":"Production install with npm, runs as the image's built-in 'node' user.","files":{"Dockerfile":"FROM node:22-alpine\nENV NODE_ENV=production\nWORKDIR /app\nCOPY package*.json ./\nRUN npm install --omit=dev\nCOPY . .\nUSER node\nEXPOSE 3000\nCMD [\"node\", \"server.js\"]\n","package.json":"{\n  \"name\": \"my-node-app\",\n  \"version\": \"1.0.0\",\n  \"private\": true,\n  \"main\": \"server.js\",\n  \"dependencies\": { \"express\": \"^4.21.0\" }\n}\n","server.js":"const express = require('express');\nconst app = express();\napp.get('/', (req, res) => res.json({ status: 'ok', message: 'Hello from a Node container' }));\napp.listen(3000, () => console.log('listening on 3000'));\n",".dockerignore":"node_modules\nnpm-debug.log\n.git\n"}},{"id":"go","name":"Go service (multi-stage build)","tag":"my-go-app","run":{"ports":["127.0.0.1:8090:8080"]},"desc":"Compile in a full Go image, ship only the static binary on a scratch base (a few MB).","files":{"Dockerfile":"# ---- build stage\nFROM golang:1.23-alpine AS build\nWORKDIR /src\nCOPY go.mod ./\nCOPY *.go ./\nRUN CGO_ENABLED=0 go build -trimpath -ldflags=\"-s -w\" -o /out/app .\n\n# ---- runtime stage: nothing but the binary\nFROM scratch\nCOPY --from=build /out/app /app\nUSER 65534:65534\nEXPOSE 8080\nENTRYPOINT [\"/app\"]\n","go.mod":"module example.com/app\n\ngo 1.23\n","main.go":"package main\n\nimport (\n\t\"fmt\"\n\t\"net/http\"\n)\n\nfunc main() {\n\thttp.HandleFunc(\"/\", func(w http.ResponseWriter, r *http.Request) {\n\t\tfmt.Fprintln(w, \"Hello from a Go container\")\n\t})\n\thttp.ListenAndServe(\":8080\", nil)\n}\n"}},{"id":"script","name":"Scheduled script / one-shot job (Bash on Alpine)","tag":"my-job","run":null,"desc":"Package a shell script with its tools. Run it with 'docker run --rm my-job' from cron or a systemd timer.","files":{"Dockerfile":"FROM alpine:3.20\nRUN apk add --no-cache bash curl jq tzdata\nCOPY job.sh /usr/local/bin/job.sh\nRUN chmod +x /usr/local/bin/job.sh\nUSER nobody\nENTRYPOINT [\"/usr/local/bin/job.sh\"]\n","job.sh":"#!/usr/bin/env bash\nset -euo pipefail\necho \"[$(date -Is)] job started\"\n# your work here\necho \"[$(date -Is)] job finished\"\n"}}],"compose":[{"id":"wordpress","name":"WordPress + MariaDB","url":"http://127.0.0.1:8080","desc":"Two services, two named volumes. The database password is generated for you and stored in .env (mode 600).","files":{"compose.yaml":"services:\n  db:\n    image: mariadb:11\n    restart: unless-stopped\n    environment:\n      MARIADB_DATABASE: wordpress\n      MARIADB_USER: wordpress\n      MARIADB_PASSWORD: ${DB_PASSWORD}\n      MARIADB_RANDOM_ROOT_PASSWORD: \"1\"\n    volumes:\n      - db_data:/var/lib/mysql\n\n  wordpress:\n    image: wordpress:latest\n    depends_on:\n      - db\n    restart: unless-stopped\n    ports:\n      - \"127.0.0.1:8080:80\"   # local only; remove 127.0.0.1: to expose on the LAN\n    environment:\n      WORDPRESS_DB_HOST: db\n      WORDPRESS_DB_USER: wordpress\n      WORDPRESS_DB_PASSWORD: ${DB_PASSWORD}\n      WORDPRESS_DB_NAME: wordpress\n    volumes:\n      - wp_data:/var/www/html\n\nvolumes:\n  db_data:\n  wp_data:\n",".env":"DB_PASSWORD={{RANDOM}}\n"}},{"id":"postgres","name":"PostgreSQL + Adminer","url":"http://127.0.0.1:8081","desc":"Database with a web admin UI. Log in to Adminer with system 'PostgreSQL', server 'db', user 'app' and the password from .env.","files":{"compose.yaml":"services:\n  db:\n    image: postgres:16\n    restart: unless-stopped\n    environment:\n      POSTGRES_USER: app\n      POSTGRES_DB: app\n      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}\n    ports:\n      - \"127.0.0.1:5432:5432\"\n    volumes:\n      - pg_data:/var/lib/postgresql/data\n    healthcheck:\n      test: [\"CMD-SHELL\", \"pg_isready -U app -d app\"]\n      interval: 10s\n      retries: 5\n\n  adminer:\n    image: adminer:latest\n    restart: unless-stopped\n    depends_on:\n      - db\n    ports:\n      - \"127.0.0.1:8081:8080\"\n\nvolumes:\n  pg_data:\n",".env":"POSTGRES_PASSWORD={{RANDOM}}\n"}},{"id":"uptime","name":"Uptime Kuma (service monitor)","url":"http://127.0.0.1:3001","desc":"Self-hosted uptime/status monitoring. Image tag per the project README (louislam/uptime-kuma:2).","files":{"compose.yaml":"services:\n  uptime-kuma:\n    image: louislam/uptime-kuma:2\n    restart: unless-stopped\n    ports:\n      - \"127.0.0.1:3001:3001\"\n    volumes:\n      - kuma_data:/app/data\n\nvolumes:\n  kuma_data:\n"}},{"id":"portainer","name":"Portainer CE (web UI for Docker)","url":"https://127.0.0.1:9443","desc":"Mounts /var/run/docker.sock — this gives the container full root-equivalent control of Docker. Portainer collects anonymous usage statistics by default; you can turn that off in its Settings.","files":{"compose.yaml":"services:\n  portainer:\n    image: portainer/portainer-ce:lts\n    restart: unless-stopped\n    ports:\n      - \"127.0.0.1:9443:9443\"   # HTTPS UI (self-signed certificate)\n    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n      - portainer_data:/data\n\nvolumes:\n  portainer_data:\n"}},{"id":"openwebui","name":"Open WebUI (chat UI for a local Ollama)","url":"http://127.0.0.1:3000","desc":"Connects to Ollama running on this computer through host.docker.internal. Ollama must listen on an address the container can reach (by default it listens on 127.0.0.1 only) — see the Open WebUI docs. Telemetry opt-outs are set explicitly.","files":{"compose.yaml":"services:\n  open-webui:\n    image: ghcr.io/open-webui/open-webui:main\n    restart: unless-stopped\n    ports:\n      - \"127.0.0.1:3000:8080\"\n    extra_hosts:\n      - \"host.docker.internal:host-gateway\"\n    environment:\n      OLLAMA_BASE_URL: http://host.docker.internal:11434\n      WEBUI_SECRET_KEY: ${WEBUI_SECRET_KEY}\n      SCARF_NO_ANALYTICS: \"true\"\n      DO_NOT_TRACK: \"true\"\n      ANONYMIZED_TELEMETRY: \"false\"\n    volumes:\n      - open_webui:/app/backend/data\n\nvolumes:\n  open_webui:\n",".env":"WEBUI_SECRET_KEY={{RANDOM}}\n"}}],"links":[["Start here (official Docker docs)",[["Get started","https://docs.docker.com/get-started/","Docker's own learning path: concepts, then hands-on guides.","Official · free"],["What is Docker?","https://docs.docker.com/get-started/docker-overview/","Architecture in plain terms: client, daemon, images, containers, registries.","Official · free"],["What is a container?","https://docs.docker.com/get-started/docker-concepts/the-basics/what-is-a-container/","Short concept page with a hands-on part.","Official · free"],["What is an image?","https://docs.docker.com/get-started/docker-concepts/the-basics/what-is-an-image/","Layers, tags and where images come from.","Official · free"],["Containerize an application","https://docs.docker.com/get-started/tutorials/run-an-app/","Step-by-step: from source code to a running container.","Official · free"],["Docker CLI cheat sheet (PDF)","https://docs.docker.com/get-started/docker_cheatsheet.pdf","One page of the most used commands.","Official · free"]]],["How-to topics",[["Writing a Dockerfile","https://docs.docker.com/get-started/docker-concepts/building-images/writing-a-dockerfile/","FROM, COPY, RUN, CMD explained with an example.","Official · free"],["Multi-stage builds","https://docs.docker.com/get-started/docker-concepts/building-images/multi-stage-builds/","Smaller, safer images by separating build and runtime.","Official · free"],["Publishing and exposing ports","https://docs.docker.com/get-started/docker-concepts/running-containers/publishing-ports/","What -p really does.","Official · free"],["Persisting container data","https://docs.docker.com/get-started/docker-concepts/running-containers/persisting-container-data/","Volumes vs. container filesystem.","Official · free"],["Docker Compose Quickstart","https://docs.docker.com/compose/gettingstarted/","Build a two-service app (Python + Redis) with compose.yaml.","Official · free"],["Building best practices","https://docs.docker.com/build/building/best-practices/","Base images, caching, non-root users, .dockerignore.","Official · free"]]],["Linux setup & reference",[["Install Docker Engine on Ubuntu","https://docs.docker.com/engine/install/ubuntu/","Official apt repository method (what the Setup tab automates).","Official · free"],["Linux post-installation steps","https://docs.docker.com/engine/install/linux-postinstall/","Run docker without sudo, start on boot, and the security implications.","Official · free"],["Dockerfile reference","https://docs.docker.com/reference/dockerfile/","Every instruction and option.","Official · free"],["Compose file reference","https://docs.docker.com/reference/compose-file/","Every key you can use in compose.yaml.","Official · free"]]],["Hands-on labs & community tutorials",[["Play with Docker Classroom","https://training.play-with-docker.com/","Labs that run in the browser — nothing to install. The browser playground asks for a Docker Hub account.","Docker-sponsored · free"],["PWD — IT Pros & SysAdmins, Stage 1","https://training.play-with-docker.com/ops-stage1","The basics from an operations point of view.","Docker-sponsored · free"],["PWD — Developers, Stage 1","https://training.play-with-docker.com/dev-stage1","The basics for developers, up to multi-service apps.","Docker-sponsored · free"],["Docker Curriculum","https://docker-curriculum.com/","Long single-page beginner tutorial by Prakhar Srivastav. Some sections may be dated.","Community · free"],["Educational resources (books & courses)","https://docs.docker.com/get-started/resources/","Docker's list of books and video courses, including Spanish titles. Many are paid.","Official list · mixed"]]]]};
/* ============================================================ Docker examples & templates (v1.4) */
const DKXS={tab:'basics',df:0,cp:0,file:{}};
async function copyText(t){try{await navigator.clipboard.writeText(t)}catch(e){const a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity='0';document.body.appendChild(a);a.select();try{document.execCommand('copy')}catch(_){}a.remove()}toast('Copied to clipboard','ok')}
const dkxRand=()=>{const b=new Uint8Array(24);crypto.getRandomValues(b);return [...b].map(x=>x.toString(16).padStart(2,'0')).join('')};
function dkxOpen(tab){if(tab)DKXS.tab=tab;
  openModal(`${icon('docker')} Docker examples &amp; templates`,`<div class="tabs" id="dkxTabs">${[['basics','ABC basics'],['dockerfiles','Dockerfile templates'],['compose','Compose templates'],['links','Tutorials &amp; links']].map(t=>`<button data-x="${t[0]}"${t[0]===DKXS.tab?' class="on"':''}>${t[1]}</button>`).join('')}</div><div id="dkxBody"></div>`,[{label:'Close'}]);
  $('#modal').classList.add('wide');
  $('#dkxTabs').onclick=e=>{const b=e.target.closest('[data-x]');if(!b)return;DKXS.tab=b.dataset.x;$$('#dkxTabs button').forEach(x=>x.classList.toggle('on',x===b));dkxRender()};
  $('#dkxBody').addEventListener('click',dkxClick);dkxRender()}
const dkxCode=(c,i)=>`<div class="dkxcmd"><code>${esc(c)}</code><button class="btn ico ghost" data-copy="${esc(c)}" title="Copy">${icon('copy')}</button></div>`;
function dkxFiles(files,key){const names=Object.keys(files);const cur=DKXS.file[key]&&files[DKXS.file[key]]!==undefined?DKXS.file[key]:names[0];
  return `<div class="dkxft">${names.map(n=>`<button class="${n===cur?'on':''}" data-file="${esc(n)}" data-fk="${esc(key)}">${esc(n)}</button>`).join('')}<span class="spacer"></span><button class="btn sm ghost" data-copy="${esc(files[cur])}">${icon('copy')}Copy file</button></div><pre class="dkxpre">${esc(files[cur])}</pre>`}
function dkxRender(){const B=$('#dkxBody');if(!B)return;const t=DKXS.tab;
  if(t==='basics'){B.innerHTML=`<p class="muted" style="margin:12px 0">Learn Docker in ten short steps. Copy any command into a terminal; buttons marked <b>Try it</b> run the example here and show progress. Commands that publish ports bind to <span class="mono">127.0.0.1</span> so nothing is exposed to your network.</p><div class="dkxgrid">${DKX.basics.map(([L,title,desc,cmds,tries],i)=>`<div class="dkxcard"><div class="dkxhd"><span class="dkxl">${L}</span><div><b>${esc(title)}</b><div class="muted" style="font-size:12.5px">${esc(desc)}</div></div></div>${cmds.map(([c,e])=>`${dkxCode(c)}<div class="dkxexp">${esc(e)}</div>`).join('')}${tries.length?`<div class="toolbar" style="margin-top:8px">${tries.map((x,j)=>`<button class="btn sm pri" data-try="${i}:${j}">${icon('play')}Try it: ${esc(x[0])}</button>`).join('')}</div>`:''}</div>`).join('')}</div>`;return}
  if(t==='dockerfiles'||t==='compose'){const list=t==='dockerfiles'?DKX.dockerfiles:DKX.compose,k=t==='dockerfiles'?'df':'cp',T=list[DKXS[k]]||list[0],key=t+':'+T.id;
    const dir=lsGet('dkx_dir_'+T.id,'~/docker/'+(T.tag||T.id));
    const secret=Object.values(T.files).some(v=>v.includes('{{RANDOM}}'));
    B.innerHTML=`<div class="dkxsplit"><div class="dkxlist">${list.map((x,i)=>`<button class="${x===T?'on':''}" data-pick="${k}:${i}"><b>${esc(x.name)}</b><span>${esc(x.desc.split('. ')[0])}</span></button>`).join('')}</div>
      <div class="dkxmain"><h3 style="margin:0 0 4px">${esc(T.name)}</h3><div class="muted" style="font-size:13px;margin-bottom:10px">${esc(T.desc)}${T.url?` · opens at <span class="mono">${esc(T.url)}</span>`:''}</div>
      ${dkxFiles(T.files,key)}
      ${secret?`<div class="infobox" style="margin-top:8px"><span class="mono">{{RANDOM}}</span> is replaced with a new random 48-character secret when you save. <span class="mono">.env</span> is written with permissions 600 (only you can read it).</div>`:''}
      <div class="form dkxform"><label>Save to folder (inside your home)</label><input type="text" id="dkxDir" value="${esc(dir)}">${t==='dockerfiles'?`<label>Image tag</label><input type="text" id="dkxTag" value="${esc(T.tag)}:latest">`:''}</div>
      <div class="toolbar" style="margin-top:10px"><button class="btn sm" data-save="0">${icon('save')}Save files</button>${t==='dockerfiles'?`<button class="btn sm pri" data-save="build">${icon('box')}Save &amp; build image</button>${T.run?`<button class="btn sm" data-runimg>${icon('play')}Run built image</button>`:''}`:`<button class="btn sm pri" data-save="up">${icon('play')}Save &amp; start (compose up)</button>`}</div>
      <div class="muted" style="font-size:12px;margin-top:8px">${t==='dockerfiles'?`Terminal equivalent: <span class="mono">cd ${esc(dir)} &amp;&amp; docker build -t ${esc(T.tag)}:latest .</span>${T.run?` then <span class="mono">docker run -d ${T.run.ports.map(p=>'-p '+esc(p)).join(' ')} ${esc(T.tag)}:latest</span>`:` then <span class="mono">docker run --rm ${esc(T.tag)}:latest</span>`}`:`Terminal equivalent: <span class="mono">cd ${esc(dir)} &amp;&amp; docker compose up -d</span> · stop with <span class="mono">docker compose down</span> · manage it later in the Compose tab.`}</div></div></div>`;return}
  if(t==='links'){B.innerHTML=`<p class="muted" style="margin:12px 0">Links open in your browser. Each was checked on 17 Oct 2026; sites can change after that.</p>${DKX.links.map(([g,items])=>`<h4 class="dkxg">${esc(g)}</h4><div class="dkxlinks">${items.map(([n,u,d,b])=>`<div class="dkxlink"><a href="${esc(u)}" target="_blank" rel="noopener noreferrer"><b>${esc(n)}</b></a><span class="badge ${b.startsWith('Official')?'b':'v'}">${esc(b)}</span><div class="muted" style="font-size:12.5px;margin-top:3px">${esc(d)}</div><div class="dkxurl"><span class="mono">${esc(u)}</span><button class="btn ico ghost" data-copy="${esc(u)}" title="Copy link">${icon('copy')}</button></div></div>`).join('')}</div>`).join('')}`}}
async function dkxSave(T,kind,after){const dir=$('#dkxDir').value.trim();if(!dir)return toast('Enter a folder','err');lsSet('dkx_dir_'+T.id,dir);
  const sec={};const files={};for(const [n,c] of Object.entries(T.files))files[n]=c.replace(/\{\{RANDOM\}\}/g,()=>sec[n]=dkxRand());
  try{let r=await api('/api/docker/template_save',{dir,files});
    if(r.exists){if(!await confirmBox('Overwrite files?',`These files already exist in <span class="mono">${esc(r.dir)}</span>:<ul>${r.exists.map(x=>`<li class="mono">${esc(x)}</li>`).join('')}</ul>${r.exists.includes('.env')?'<div class="warnbox">Overwriting <span class="mono">.env</span> creates a new password. A database already initialised with the old password will refuse it — keep the old .env if the stack has run before.</div>':''}`,'Overwrite',true))return null;
      r=await api('/api/docker/template_save',{dir,files,overwrite:true})}
    toast(`Saved ${r.written.length} file(s) to ${esc(r.dir)}`,'ok');
    if(after==='build'){const tag=$('#dkxTag').value.trim();closeModal();return dkrDo('image','build',[],{dir:r.dir,tag},`Build ${tag}`)}
    if(after==='up'){closeModal();const res=await dkrDo('compose','up',[],{files:[r.dir+'/compose.yaml'],project:T.id},`compose up ${T.id}`);if(res&&T.url)toast(`When it is up: <span class="mono">${esc(T.url)}</span>`,'ok');return res}
    return r}catch(e){terr(e);return null}}
function dkxClick(e){const c=e.target.closest('[data-copy]');if(c){copyText(c.dataset.copy);return}
  const f=e.target.closest('[data-file]');if(f){DKXS.file[f.dataset.fk]=f.dataset.file;dkxRender();return}
  const p=e.target.closest('[data-pick]');if(p){const [k,i]=p.dataset.pick.split(':');DKXS[k]=+i;dkxRender();return}
  const tr=e.target.closest('[data-try]');if(tr){const [i,j]=tr.dataset.try.split(':').map(Number);const x=DKX.basics[i][4][j];closeModal();dkrDo('container','run',[],x[1],`Try it: ${x[0]}`);return}
  const s=e.target.closest('[data-save]');if(s){const T=DKXS.tab==='dockerfiles'?DKX.dockerfiles[DKXS.df]:DKX.compose[DKXS.cp];dkxSave(T,DKXS.tab,s.dataset.save==='0'?null:s.dataset.save);return}
  if(e.target.closest('[data-runimg]')){const T=DKX.dockerfiles[DKXS.df],tag=$('#dkxTag').value.trim();closeModal();dkrDo('container','run',[],{image:tag,name:T.tag+'-demo',ports:T.run.ports,restart:'no'},`Run ${tag}`);toast(`Opens at http://${esc(T.run.ports[0].split(':').slice(0,2).join(':'))}`,'ok')}}
/* ============================================================ operation tracker (v1.3) */
const OPS={list:[],seq:0,tick:null,raf:0};
const ACT_LABEL={mount:'Mount',unmount:'Unmount',setlabel:'Edit label',partname:'Rename partition',takeown:'Take ownership',lock:'Lock',unlock:'Unlock',changepass:'Change passphrase',eject:'Eject',poweroff:'Power off',standby:'Standby',wakeup:'Wake up',smart_test:'Start SMART self-test',smart_abort:'Abort SMART self-test',loop_detach:'Detach loop device',pt_backup:'Back up partition table',attach_image:'Attach image'};
function opStart(o){const op=Object.assign({id:++OPS.seq,state:'running',t0:Date.now(),progress:null,indet:true,msg:'Starting…',series:[],steps:null,step:0,skipped:[]},o);OPS.list.unshift(op);OPS.list=OPS.list.filter((x,i)=>x.state==='running'||i<12);opRender();opTicker();if(CUR==='disks'&&DK.list.length)renderDkDetail();return op}
function opUpd(op,p){Object.assign(op,p);opRenderSoon()}
function opEnd(op,ok,msg){op.state=ok===true?'done':ok===null?'cancelled':'error';op.t1=Date.now();if(msg)op.msg=msg;if(ok===true){op.progress=1;op.indet=false;if(op.steps)op.step=op.steps.length}opRender();if(CUR==='disks'&&DK.list.length){renderDkList();renderDkDetail()}}
function opRenderSoon(){if(!OPS.raf)OPS.raf=requestAnimationFrame(()=>{OPS.raf=0;opRender()})}
function opTicker(){if(OPS.tick)return;OPS.tick=setInterval(()=>{if(!OPS.list.some(o=>o.state==='running')){clearInterval(OPS.tick);OPS.tick=null}opRender()},1000)}
const fmtDur=ms=>{const s=Math.max(0,Math.round(ms/1000));return s<60?`${s}s`:s<3600?`${Math.floor(s/60)}m ${pad(s%60)}s`:`${Math.floor(s/3600)}h ${pad(Math.floor(s%3600/60))}m`};
function opRing(op,size){const S=size||56,C=S/2,R=C-5,circ=2*Math.PI*R;const st=op.state;
  const col=st==='error'?'var(--red)':st==='done'&&op.warn?'var(--amber)':st==='done'?'var(--green)':st==='cancelled'?'var(--dim)':'var(--cyan)';
  const pct=op.progress!=null?Math.round(op.progress*100):null;
  const arc=st==='running'&&(op.indet||op.progress==null)?`<circle cx="${C}" cy="${C}" r="${R}" fill="none" stroke="${col}" stroke-width="4" stroke-linecap="round" stroke-dasharray="${circ*.28} ${circ}" class="opspin" style="transform-origin:${C}px ${C}px"/>`
    :`<circle cx="${C}" cy="${C}" r="${R}" fill="none" stroke="${col}" stroke-width="4" stroke-linecap="round" stroke-dasharray="${circ*(st==='running'?op.progress:1)} ${circ}" transform="rotate(-90 ${C} ${C})" style="filter:drop-shadow(0 0 4px ${col})"/>`;
  const mid=st==='done'&&op.warn?`<text x="${C}" y="${C+7}" text-anchor="middle" style="font:700 ${S>70?26:20}px var(--font);fill:${col}">!</text>`:st==='done'?`<path d="M${C-8} ${C} l5 5 l10 -11" fill="none" stroke="${col}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>`:st==='error'?`<path d="M${C-6} ${C-6} l12 12 M${C+6} ${C-6} l-12 12" stroke="${col}" stroke-width="3" stroke-linecap="round"/>`
    :`<text x="${C}" y="${C+4}" text-anchor="middle" style="font:600 ${S>70?17:12}px var(--mono);fill:var(--text)">${pct!=null&&!op.indet?pct+'%':'…'}</text>`;
  return `<svg width="${S}" height="${S}" viewBox="0 0 ${S} ${S}" style="flex:none"><circle cx="${C}" cy="${C}" r="${R}" fill="none" stroke="var(--track)" stroke-width="4"/>${arc}${mid}</svg>`}
function opSpark(series,w,h){if(!series||series.length<2)return '';const mx=Math.max(...series,0.001);const pts=series.map((v,i)=>`${(i/(series.length-1)*w).toFixed(1)},${(h-2-(v/mx)*(h-4)).toFixed(1)}`).join(' ');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" class="opspark"><polyline points="0,${h} ${pts} ${w},${h}" fill="rgba(34,211,238,.15)" stroke="none"/><polyline points="${pts}" fill="none" stroke="var(--cyan)" stroke-width="1.6"/></svg>`}
function opCard(op,big){const run=op.state==='running',el=(op.t1||Date.now())-op.t0;
  const lbl=op.warn&&op.state==='done'?['a','ATTENTION']:{running:['a','RUNNING'],done:['g','DONE'],error:['r','FAILED'],cancelled:['','CANCELLED']}[op.state];
  const steps=op.steps?`<div class="opsteps">${op.steps.map((s,i)=>{const sk=(op.skipped||[]).includes(s);const c=sk?'sk':i<op.step||op.state==='done'?'ok':i===op.step&&run?'cur':op.state==='error'&&i===op.step?'bad':'';return `<span class="opstep ${c}">${c==='ok'?'✓':c==='cur'?'<span class="spin" style="width:9px;height:9px;border-width:2px"></span>':c==='bad'?'✕':''}${esc(s)}</span>`}).join('<span class="oparrow">›</span>')}</div>`:'';
  const right=[op.rate?`<b class="mono">${fmtSI(op.rate)}/s</b>`:'',op.done!=null&&op.total?`<span class="mono">${fmtSI(op.done)} / ${fmtSI(op.total)}</span>`:'',run&&op.eta!=null?`<span>ETA ${fmtEta(op.eta)}</span>`:'',`<span>${run?'elapsed ':'took '}${fmtDur(el)}</span>`].filter(Boolean).join(' · ');
  return `<div class="opcard ${op.state} ${big?'big':''}" data-op="${op.id}">${opRing(op,big?92:54)}<div class="opmain">
    <div class="row" style="gap:8px;flex-wrap:nowrap"><b style="white-space:nowrap">${esc(op.title)}</b>${op.dev?`<span class="mono dim" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0">${esc(op.dev)}</span>`:''}<span class="badge ${lbl[0]}">${lbl[1]}</span><span class="spacer"></span>${run&&op.job?`<button class="btn sm ghost" data-opcancel="${op.id}">Cancel</button>`:''}${!run?`<button class="btn ico ghost" data-opx="${op.id}" title="Dismiss">${icon('x')}</button>`:''}</div>
    <div class="muted opstats">${right}</div>
    ${steps}<div class="opbar ${run&&(op.indet||op.progress==null)?'indet':''} ${op.state}"><i style="width:${run?(op.indet||op.progress==null?100:Math.max(2,op.progress*100)):100}%"></i></div>
    <div class="row" style="gap:10px;align-items:flex-end"><div class="opmsg">${esc(op.msg||'')}</div><span class="spacer"></span>${opSpark(op.series,big?240:150,big?44:28)}</div></div></div>`}
function opRender(){const running=OPS.list.filter(o=>o.state==='running');if(CUR==='disks'&&$('#dkList')&&DK.list.length){const sig=running.map(o=>o.id+':'+Math.round((o.progress||0)*100)).join(',');if(sig!==OPS.lsig){OPS.lsig=sig;renderDkList()}}
  const pill=$('#opsPill');if(pill){if(running.length){pill.style.display='';pill.innerHTML=`<span class="spin" style="width:12px;height:12px"></span>${running.length} operation${running.length>1?'s':''} running · ${esc(running[0].title)}${running[0].progress!=null&&!running[0].indet?' '+Math.round(running[0].progress*100)+'%':''}`}else pill.style.display='none'}
  const h=$('#dkOps');if(h){const show=OPS.list.slice(0,8);h.innerHTML=`<h3>${icon('gauge')}Operations<span class="r muted" style="font-size:12px">${running.length?`${running.length} running`:'idle'}${OPS.list.some(o=>o.state!=='running')?` · <a style="color:var(--cyan);cursor:pointer" id="opClear">clear finished</a>`:''}</span></h3>`+(show.length?show.map(o=>opCard(o)).join(''):'<div class="muted" style="font-size:12.5px;padding:4px 2px">No operations yet. Every disk action — mount, check, repair, format, backup, restore — appears here with its live progress.</div>')}
  $$('[data-opmodal]').forEach(m=>{const op=OPS.list.find(o=>o.id===+m.dataset.opmodal);if(op)m.innerHTML=opCard(op,true)})}
document.addEventListener('click',e=>{const c=e.target.closest('[data-opcancel]');if(c){const op=OPS.list.find(o=>o.id===+c.dataset.opcancel);if(op&&op.job){api('/api/job/cancel',{id:op.job});opUpd(op,{msg:'Cancelling…'})}return}
  const x=e.target.closest('[data-opx]');if(x){OPS.list=OPS.list.filter(o=>o.id!==+x.dataset.opx);opRender();return}
  if(e.target.id==='opClear'){OPS.list=OPS.list.filter(o=>o.state==='running');opRender();return}
  if(e.target.closest('#opsPill')){go('disks');setTimeout(()=>{const d=$('#dkOps');if(d)d.scrollIntoView({behavior:'smooth'})},200)}});
/* follow a server job and mirror it into an op */
async function opFollow(op,jobId,onTick){op.job=jobId;for(;;){const j=await api('/api/job?id='+encodeURIComponent(jobId));const L=j.live||{};
    const p={msg:j.message,progress:j.progress||0,indet:L.indeterminate!==undefined?!!L.indeterminate:!(j.progress>0)};
    if(L.steps){p.steps=L.steps;p.step=L.step||0;p.skipped=L.skipped||[]}
    if(L.rate!=null)p.rate=L.rate;if(L.eta!==undefined)p.eta=L.eta;if(L.total)p.total=L.total;if(L.done!=null)p.done=L.done;if(L.series)p.series=L.series;
    opUpd(op,p);if(onTick)onTick(j);
    if(j.state!=='running'){if(j.state==='error')throw new Error(j.error);return j}await new Promise(r=>setTimeout(r,400))}}
/* run any server job as a tracked operation with an optional detail modal */
async function opRunJob(title,dev,startPromise,fmtRes,opts){opts=opts||{};const op=opStart({title,dev,msg:'Waiting for authorization…'});
  if(!opts.noModal)openModal(esc(title),`<div data-opmodal="${op.id}"></div><div id="djRes" style="margin-top:12px"></div><pre class="out" id="djLog" style="display:none;margin-top:12px;max-height:38vh"></pre><div class="dim" style="font-size:12px;margin-top:8px">You can close this window — the operation keeps running and stays visible in Disk Utility → Operations and in the header.</div>`,[{label:'Close'}]);
  opRender();DK.busy++;let j=null;
  try{const {job}=await startPromise;j=await opFollow(op,job,j=>{const Lg=j.live&&j.live.log,o=$('#djLog');if(Lg&&Lg.length&&o&&$(`[data-opmodal="${op.id}"]`)){o.style.display='';o.textContent=Lg.join('\n');o.scrollTop=o.scrollHeight}});
    if(j.state==='cancelled'){opEnd(op,null,'Cancelled');return null}
    const rc=j.result&&j.result.clean;if(rc===false)op.warn=true;opEnd(op,true,opts.doneMsg?opts.doneMsg(j):rc===false?'Finished — problems found, the filesystem needs repair':rc===true?'Finished — filesystem is clean':'Completed');const res=$('#djRes');if(res&&$(`[data-opmodal="${op.id}"]`)){res.innerHTML=fmtRes?fmtRes(j):'';const out=j.result&&j.result.output,o=$('#djLog');if(out&&o){o.style.display='';o.textContent=out;o.scrollTop=o.scrollHeight}}
    toast(`${esc(title)} — done`,'ok')}
  catch(e){opEnd(op,false,e.message.split('\n')[0]);const res=$('#djRes');if(res&&$(`[data-opmodal="${op.id}"]`))res.innerHTML=`<pre class="warnbox" style="white-space:pre-wrap;margin:0">${esc(e.message)}</pre>`;terr(e)}
  finally{DK.busy=Math.max(0,DK.busy-1)}
  if(!opts.noRefresh){opUpd(op,{});await dkSettle();loadBk()}return j}
/* ---- disk actions as tracked operations */
async function dkAct(action,dev,params,okMsg){const quickMount=action==='mount'||action==='unmount';
  const op=opStart({title:ACT_LABEL[action]||action,dev,steps:['Request','Verify','Refresh'],msg:'Waiting for UDisks2 (authorize if prompted)…'});
  DK.busy++;let r=null;try{r=await api('/api/disk/action',{action,dev,params:params||{}});opUpd(op,{step:1,msg:r.message||okMsg||'Applied — verifying…'})}
  catch(e){DK.busy--;opEnd(op,false,e.message);terr(e);return}DK.busy--;
  const check=quickMount?()=>{const x=dkByPath(dev);return x&&((x.mountpoints||[]).length>0)===(action==='mount')}:null;
  opUpd(op,{step:2,msg:'Refreshing disk status…'});const done=await dkSettle(check);
  if(check&&!done){opEnd(op,false,`The system still reports ${dev} as ${action==='mount'?'not mounted':'mounted'}`);toast(`The system still reports ${esc(dev)} as ${action==='mount'?'not mounted':'mounted'}. If it persists, another program may be holding the volume.`,'err');return}
  opEnd(op,true,r.message||okMsg||'Done');toast(esc(r.message||okMsg||'Done'),r.verified===false?'err':'ok')}
async function mountMany(nodes,act){const n=nodes.length;const op=opStart({title:act==='mount'?`Mount all (${n})`:`Unmount all (${n})`,dev:nodes.map(x=>x.path).join(', '),indet:false,progress:0,steps:nodes.map(x=>x.path.split('/').pop()).concat(['Refresh'])});
  DK.busy++;let ok=0,fail=[];try{for(let i=0;i<n;i++){opUpd(op,{step:i,progress:i/(n+1),msg:`${act==='mount'?'Mounting':'Unmounting'} ${nodes[i].path}…`});try{const r=await api('/api/disk/action',{action:act,dev:nodes[i].path,params:{}});ok++;if(r.verified===false)fail.push(nodes[i].path)}catch(e){fail.push(nodes[i].path);terr(e)}}}finally{DK.busy--}
  opUpd(op,{step:n,progress:n/(n+1),msg:'Refreshing disk status…'});const paths=nodes.map(x=>x.path);
  await dkSettle(()=>paths.every(p=>{const x=dkByPath(p);return x&&((x.mountpoints||[]).length>0)===(act==='mount')}));
  if(fail.length)opEnd(op,false,`${ok} of ${n} done · problem with ${fail.join(', ')}`);else opEnd(op,true,`${act==='mount'?'Mounted':'Unmounted'} ${ok} volume(s)`)}
async function dkJob(title,action,dev,params,confirm,fmtRes){return opRunJob(title,dev,api('/api/disk/action',{action,dev,params:params||{},confirm}),fmtRes)}
/* ---- drive status badges */
const BI=(n)=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">${IC[n]}</svg>`;
function devOnDisk(dev,d){if(!dev)return false;return String(dev).split(', ').some(p=>p===d.path||!!dkByPath(p,[d]))}
function topVolOf(d,leaf){return (d.children||[]).find(c=>c===leaf||!!dkByPath(leaf.path,[c]))}
function driveBadges(d){const B=[];const busy=OPS.list.find(o=>o.state==='running'&&devOnDisk(o.dev,d));
  if(busy)B.push(`<span class="badge a" title="${esc(busy.title)}"><span class="spin" style="width:10px;height:10px;border-width:2px"></span>WORKING${busy.progress!=null&&!busy.indet?' '+Math.round(busy.progress*100)+'%':''}</span>`);
  const s=d.health&&d.health.smart;
  if(s){if(s.failing)B.push(`<span class="badge r" title="SMART reports the drive is failing — back up now">${BI('heart')}FAILING</span>`);else if(s.supported!==false)B.push(`<span class="badge g" title="SMART health OK">${BI('heart')}OK</span>`);
    if(s.selftest_status==='inprogress')B.push(`<span class="badge b">${BI('gauge')}SELF-TEST ${100-(s.selftest_remaining||0)}%</span>`)}
  if(d.io_errors)B.push(`<span class="badge r" title="${d.io_errors} I/O error(s) in this boot's kernel log for this drive">${BI('bolt')}I/O ERRORS ${d.io_errors}</span>`);
  const L=fsLeaves(d),need=L.filter(n=>n.fshealth&&n.fshealth.state==='repair');
  if(need.length)B.push(`<button class="badge r bfix" data-fix="${esc(need[0].path)}" title="${esc(need.map(n=>n.path+': '+n.fshealth.issues.join('; ')).join('\n'))}">${BI('gear')}NEEDS REPAIR${need.length>1?' ('+need.length+')':''}</button>`);
  if(L.length){const m=L.filter(n=>n.mountpoints.length);B.push(m.length?`<span class="badge b" title="${esc(m.map(n=>n.mountpoints[0]).join('\n'))}">${BI('folder')}MOUNTED${L.length>1?' '+m.length+'/'+L.length:''}</span>`:`<span class="badge" title="No filesystem on this drive is mounted">${BI('folder')}NOT MOUNTED</span>`)}
  else if(d.type==='disk'&&!(d.children||[]).length)B.push(`<span class="badge">${BI('disk')}EMPTY</span>`);
  if(d.critical)B.push(`<span class="badge b" title="Holds the running system">${BI('shield')}SYSTEM</span>`);
  return B.join('')}
function healthBadge(d){return ''}
function renderDkList(){$('#dkList').innerHTML=DK.list.map(d=>`<div class="dcard ${d.kname===DK.sel?'on':''}" data-k="${esc(d.kname)}">${icon(diskIcon(d),'di')}<div class="tx"><b>${esc(fmtSI(d.size))} ${esc(d.model||d.vendor||(d.type==='loop'?'Loop device':d.type==='rom'?'Optical drive':'Drive'))}</b><small class="mono">${esc(d.path)}</small> <small>${esc((d.tran||d.type||'').toUpperCase())}${d.type==='disk'?(d.rota?' · HDD':' · SSD'):''}</small><div class="dbadges">${driveBadges(d)}</div></div></div>`).join('')||'<div class="empty">No block devices.</div>';
  $$('#dkList [data-fix]').forEach(b=>b.onclick=ev=>{ev.stopPropagation();const p=b.dataset.fix;const d=DK.list.find(x=>dkByPath(p,[x]));if(!d)return;DK.sel=d.kname;const leaf=dkByPath(p,[d]);const tv=topVolOf(d,leaf);DK.vol=tv?tv.kname:d.kname;renderDkList();renderDkDetail();fsMaint(leaf,'repair')})}

/* After an action: refresh with udev settle, then keep re-checking for a few seconds until the system reports the change */
async function dkSettle(check){for(const wait of [0,700,1500,3000]){if(wait)await new Promise(r=>setTimeout(r,wait));await loadDk({settle:true,force:true,noBk:true,render:true});if(!check||check())return true}return false}
function diskIcon(d){if(d.type==='loop')return 'image';if(d.tran==='usb'||d.rm)return 'usb';if(d.type==='rom')return 'disk';return d.rota?'hdd':'ssd'}
function findNode(k,list){for(const d of list||DK.list){if(d.kname===k)return d;const f=findNode(k,d.children||[]);if(f)return f}return null}
function allMounts(n){let m=[...(n.mountpoints||[])];(n.children||[]).forEach(c=>m=m.concat(allMounts(c)));return m}
/* action group builder: [{label,ic,fn,cls,dis,why}] */
function actGroup(title,btns){const h=document.createElement('div');h.className='agrp';h.innerHTML=`<div class="agt">${title}</div><div class="toolbar"></div>`;const bar=h.lastChild;
  btns.filter(Boolean).forEach(b=>{const e=document.createElement('button');e.className='btn sm '+(b.cls||'ghost');e.innerHTML=icon(b.ic)+b.label;if(b.dis){e.disabled=true;e.title=b.why||''}else{e.onclick=b.fn;if(b.why)e.title=b.why}bar.appendChild(e)});
  if(btns.some(b=>b&&b.dis&&b.why)){const why=[...new Set(btns.filter(b=>b&&b.dis&&b.why).map(b=>b.why))];h.insertAdjacentHTML('beforeend',`<div class="dim" style="font-size:11.5px;margin-top:6px">${why.map(esc).join(' · ')}</div>`)}return h}
function renderDkDetail(){
  const d=DK.list.find(x=>x.kname===DK.sel);const host=$('#dkDetail');if(!d){host.innerHTML='<div class="card empty">Select a drive.</div>';return}
  const h=d.health||{},sm=h.smart,crit=!!d.critical;
  const segs=[];if(d.type==='disk'&&d.pttype){const parts=(d.children||[]).filter(c=>c.start!=null).map(c=>({k:'p',n:c,o:c.start,s:c.size}));(d.free||[]).forEach(f=>parts.push({k:'f',o:f.offset,s:f.size,f}));parts.sort((a,b)=>a.o-b.o);segs.push(...parts)}else{segs.push({k:'p',n:d,o:0,s:d.size})}
  if(!DK.vol&&segs.length){const first=segs.find(s=>s.k==='p');DK.vol=first?first.n.kname:'free:0'}
  const lay=segs.map((s,i)=>{const w=Math.max(4,100*s.s/d.size);if(s.k==='f')return `<div class="vol free ${DK.vol==='free:'+i?'sel':''}" data-v="free:${i}" style="flex:${w} 1 0"><b>Free space</b><small>${fmtSI(s.s)}</small></div>`;
    const n=s.n,inner=(n.children&&n.children[0])||n,fs=inner.fstype||n.fstype,col=FS_COL[n.fstype]||FS_COL[fs]||'#334155',used=inner.fsused&&inner.size?100*inner.fsused/inner.size:null;
    const bad=fsLeaves(n).some(x=>x.fshealth&&x.fshealth.state==='repair');const busyV=OPS.list.some(o=>o.state==='running'&&String(o.dev||'').split(', ').some(p=>p===n.path||!!dkByPath(p,[n])));
    return `<div class="vol ${DK.vol===n.kname?'sel':''} ${bad?'volbad':''} ${busyV?'volbusy':''}" data-v="${esc(n.kname)}" style="flex:${w} 1 0;background:linear-gradient(160deg,${col},${hexMix(col,'#000000',.35)});color:#fff"><b>${bad?'⚠ ':''}${esc(n.label||inner.label||n.partlabel||n.name)}</b><small>${fmtSI(n.size)} ${esc(fs||'')}${n.fstype==='crypto_LUKS'?(n.children&&n.children.length?' · unlocked':' · locked'):''}</small><small>${esc((allMounts(n)[0])||'not mounted')}</small>${used!=null?`<span class="use" style="width:${used}%"></span>`:''}</div>`}).join('');
  const temp=sm&&sm.temp_c!=null?sm.temp_c:null;
  host.innerHTML=`<div class="card glow"><div class="row sb"><div><div style="font-size:18px;font-weight:650">${esc(d.model||h.model||d.vendor||d.name)}</div><div class="muted mono" style="font-size:12.5px">${esc(d.path)} · ${fmtSI(d.size)} (${d.size.toLocaleString()} bytes)</div></div><div class="row">${crit?'<span class="badge b">SYSTEM DISK</span>':''}${healthBadge(d)}</div></div>
   <div class="kv" style="margin-top:14px;grid-template-columns:max-content 1fr max-content 1fr">
    <div>Serial</div><div class="mono">${esc(d.serial||h.serial||'—')}</div><div>Partitioning</div><div>${esc(d.pttype?d.pttype.toUpperCase():(d.type==='disk'?'None':'—'))}</div>
    <div>Bus</div><div>${esc((d.tran||h.bus||'—').toUpperCase())}</div><div>Media</div><div>${d.type==='disk'?(d.rota?(h.rotation>0?h.rotation+' RPM HDD':'Rotational'):'Solid-state'):esc(d.type)}</div>
    <div>Sector size</div><div>${d.logsec||'—'} / ${d.physec||'—'} B</div><div>Firmware</div><div class="mono">${esc(h.revision||'—')}</div>
    ${sm?`<div>SMART</div><div>${sm.failing?'<span class="badge r">Failing</span>':'<span class="badge g">Healthy</span>'} ${temp!=null?temp+' °C':''}</div><div>Powered on</div><div>${sm.power_on_hours!=null?sm.power_on_hours.toLocaleString()+' h':'—'}</div>`:''}
   </div><div id="dkDrvActs" class="agrid" style="margin-top:16px"></div></div>
   <div class="card"><h3>${icon('disk')}Volumes<span class="r muted">click a block to select it</span></h3><div class="layout" id="dkLay">${lay}</div><div id="dkVol" style="margin-top:16px"></div></div>`;
  $('#dkLay').onclick=e=>{const v=e.target.closest('.vol');if(!v)return;DK.vol=v.dataset.v;renderDkDetail()};
  const fsAll=fsLeaves(d),unm=fsAll.filter(n=>!n.mountpoints.length),mtd=fsAll.filter(n=>n.mountpoints.length&&!isCrit(n));
  const A=$('#dkDrvActs'),sysWhy='Running system disk — use a live USB';
  A.appendChild(actGroup('Mount',[{label:`Mount all${unm.length?' ('+unm.length+')':''}`,ic:'play',cls:'pri',dis:!unm.length,why:unm.length?'':'Nothing to mount',fn:()=>mountMany(unm,'mount')},{label:`Unmount all${mtd.length?' ('+mtd.length+')':''}`,ic:'x',dis:!mtd.length,why:mtd.length?'':(crit?'System volumes stay mounted':'Nothing mounted'),fn:()=>mountMany(mtd,'unmount')}]));
  if(d.type==='disk'||d.type==='loop'){const fr=d.free||[];A.appendChild(actGroup('Partitions',[{label:'Create partition…',ic:'plus',cls:'pri',dis:!d.pttype||!fr.length,why:!d.pttype?'No partition table — create one first':(!fr.length?'No unallocated space — shrink a partition with Resize… first':''),fn:()=>newPartDlg(d)},{label:d.pttype?'New partition table…':'Create partition table…',ic:'list',cls:d.pttype?'danger':'',dis:crit,why:crit?sysWhy:'',fn:()=>fmtDisk(d)},{label:'Recover lost partitions…',ic:'life',fn:()=>{go('recovery');setTimeout(()=>{if(RC.targets.length){$('#rcDev').value=d.path;selectDev()}},400)}}]))}
  A.appendChild(actGroup('Backup &amp; restore',[{label:'Back up disk…',ic:'save',cls:'pri',fn:()=>backupDlg(d,'image')},{label:'Restore disk image…',ic:'image',cls:'danger',dis:crit||d.type==='rom',why:crit?sysWhy:'',fn:()=>imgRestore(d)},d.type==='disk'&&d.pttype?{label:'Partition table…',ic:'list',fn:()=>ptMenu(d)}:null]));
  A.appendChild(actGroup('Drive',[d.type==='disk'?{label:'SMART & self-tests',ic:'heart',fn:()=>smartDlg(d)}:null,{label:'Benchmark',ic:'gauge',fn:()=>benchDlg(d.path)},d.type!=='loop'&&d.type!=='rom'?{label:'Format disk…',ic:'x',cls:'danger',dis:crit,why:crit?sysWhy:'',fn:()=>fmtDisk(d)}:null,d.type==='loop'?{label:'Detach',ic:'x',cls:'danger',fn:()=>dkAct('loop_detach',d.path,{},'Loop device detached')}:null,sm&&sm.type==='ata'&&sm.pm_supported?{label:'Standby',ic:'moon',fn:()=>dkAct('standby',d.path)}:null,sm&&sm.type==='ata'&&sm.pm_supported?{label:'Wake',ic:'sun',fn:()=>dkAct('wakeup',d.path)}:null,h.ejectable?{label:'Eject',ic:'up',fn:()=>dkAct('eject',d.path)}:null,h.can_poweroff?{label:'Power off',ic:'power',cls:'danger',dis:crit,why:crit?sysWhy:'',fn:()=>dkAct('poweroff',d.path,{},'Drive powered off — safe to unplug')}:null]));
  renderVol(d,segs);
}
function dkByPath(p,list){for(const d of list||DK.list){if(d.path===p)return d;const f=dkByPath(p,d.children||[]);if(f)return f}return null}
function renderVol(d,segs){
  const host=$('#dkVol');if(!DK.vol){host.innerHTML='';return}
  if(DK.vol.startsWith('free:')){const s=segs[+DK.vol.slice(5)];if(!s){host.innerHTML='';return}
    host.innerHTML=`<div class="kv"><div>Free space</div><div>${fmtSI(s.s)} at offset ${fmtSI(s.o)}</div></div><div class="toolbar" style="margin-top:12px"><button class="btn pri sm" id="dkNewPart">${icon('plus')}Create partition…</button></div>`;
    $('#dkNewPart').onclick=()=>newPart(d,s.f);return}
  const n=findNode(DK.vol,[d]);if(!n){host.innerHTML='';return}
  const inner=(n.fstype==='crypto_LUKS'&&n.children&&n.children[0])||n;const mps=allMounts(n);
  const leaf=isFs(inner)?inner:((!inner.fstype&&!(inner.children||[]).length&&inner.type!=='rom')?inner:null),used=inner.fsused,tot=inner.fsused!=null&&inner.fsavail!=null?inner.fsused+inner.fsavail:null;
  const crit=isCrit(n),mounted=!!(leaf&&leaf.mountpoints.length),sysWhy='Running system volume — boot a live USB for this';
  host.innerHTML=`<div class="grid g12" style="gap:14px"><div class="c8"><div class="kv">
    <div>Device</div><div class="mono">${esc(n.path)}${inner!==n?' → '+esc(inner.path):''}</div>
    <div>Size</div><div>${fmtSI(n.size)} (${n.size.toLocaleString()} bytes)</div>
    <div>Contents</div><div>${esc(n.fstype||'Unknown')}${n.fsver?' '+esc(n.fsver):''}${inner!==n?' / '+esc(inner.fstype||'?'):''}${n.fstype==='crypto_LUKS'?(n.children&&n.children.length?' — <span class="badge g">unlocked</span>':' — <span class="badge a">locked</span>'):''}</div>
    <div>Label</div><div>${esc(inner.label||n.label||'—')}</div>
    ${n.partlabel!=null||n.parttypename?`<div>Partition</div><div>${esc(n.partlabel||'')}${n.parttypename?' <span class="dim">('+esc(n.parttypename)+')</span>':''}</div>`:''}
    <div>UUID</div><div class="mono dim" style="font-size:12px">${esc(inner.uuid||n.uuid||'—')}</div>
    ${leaf?`<div>Filesystem state</div><div>${fsStateHtml(leaf)}</div>`:''}
    <div>Mounted at</div><div>${mps.length?mps.map(m=>`<a class="mono" style="color:var(--cyan);cursor:pointer" data-open="${esc(m)}">${esc(m)}</a>`).join(', '):'<span class="badge a">Not mounted</span>'}</div></div>
    <div id="dkFstab" class="muted" style="font-size:12px;margin-top:8px"></div></div>
    <div class="c4">${tot?`<div id="gVol"></div>`:''}</div></div><div class="agrid" id="dkVolActs" style="margin-top:12px"></div><div id="dkLeaves"></div>`;
  if(tot){const g=SegGauge($('#gVol'),{label:'USED',pal:used/tot>.9?'amber':'cyan',n:40});g.set(100*used/tot,`${(100*used/tot).toFixed(0)}<tspan class="g-unit">%</tspan>`,`${fmtSI(inner.fsavail)} FREE`)}
  host.querySelectorAll('[data-open]').forEach(a=>a.onclick=()=>api('/api/open',{path:a.dataset.open}).catch(terr));
  api('/api/disk/fstab?dev='+encodeURIComponent((leaf||n).path)).then(r=>{if(r.entries.length&&$('#dkFstab'))$('#dkFstab').innerHTML='fstab: <span class="mono">'+r.entries.map(esc).join('<br>')+'</span> <span class="dim">(read-only)</span>'}).catch(()=>{});
  const A=$('#dkVolActs'),noFs='No mountable filesystem on this volume';
  A.appendChild(actGroup('Mount',[mounted?{label:'Unmount',ic:'x',cls:'pri',dis:crit,why:crit?'The running system cannot unmount its own volume':'',fn:()=>dkAct('unmount',leaf.path,{},'Unmounted')}:{label:'Mount',ic:'play',cls:'pri',dis:!leaf,why:leaf?'':noFs,fn:()=>dkAct('mount',leaf.path)},leaf&&mounted?{label:'Open folder',ic:'folder',fn:()=>api('/api/open',{path:leaf.mountpoints[0]}).catch(terr)}:null]));
  A.appendChild(actGroup('Check &amp; repair',[{label:'Check filesystem…',ic:'shield',dis:!leaf||crit,why:!leaf?noFs:crit?sysWhy:'',fn:()=>fsMaint(leaf,'check')},{label:'Repair filesystem…',ic:'gear',cls:'danger',dis:!leaf||crit,why:!leaf?noFs:crit?sysWhy:'',fn:()=>fsMaint(leaf,'repair')}]));
  A.appendChild(actGroup('Backup &amp; restore',[{label:'Back up as image…',ic:'save',cls:'pri',fn:()=>backupDlg(n,'image')},{label:'Back up files…',ic:'copy',dis:!mounted,why:mounted?'':'Mount the volume to back up its files',fn:()=>backupDlg(leaf,'files')},{label:'Restore image…',ic:'image',cls:'danger',dis:crit,why:crit?sysWhy:'',fn:()=>imgRestore(n)},{label:'Restore files…',ic:'down',dis:!mounted,why:mounted?'':'Mount the volume to restore files into it',fn:()=>filesRestore(leaf)}]));
  const ed=[];
  if(leaf)ed.push({label:'Edit label…',ic:'list',fn:async()=>{const r=await promptBox('Filesystem label',[{id:'label',label:'Label',value:leaf.label||''}],'Apply');if(r)dkAct('setlabel',leaf.path,{label:r.label},'Label changed')}});
  if(n.type==='part'&&d.pttype==='gpt')ed.push({label:'Partition name…',ic:'list',fn:async()=>{const r=await promptBox('GPT partition name',[{id:'name',label:'Name',value:n.partlabel||''}],'Apply');if(r)dkAct('partname',n.path,{name:r.name},'Partition renamed')}});
  if(mounted)ed.push({label:'Take ownership…',ic:'shield',fn:async()=>{const r=await promptBox('Take ownership',[{id:'recursive',label:'Scope',type:'check',value:false,text:'Apply recursively to all files'}],'Apply');if(r)dkAct('takeown',leaf.path,{recursive:r.recursive},'Ownership changed')}});
  if(n.type==='part'){ed.push({label:'Format…',ic:'x',cls:'danger',dis:crit,why:crit?sysWhy:'',fn:()=>fmtPart(n)},{label:'Resize…',ic:'swap',cls:'danger',dis:crit,why:crit?sysWhy:'',fn:()=>resizeDlg(d,n)},{label:'Delete…',ic:'trash',cls:'danger',dis:crit,why:crit?sysWhy:'',fn:async()=>{if(!await confirmBox('Delete partition',`All data on <b class="mono">${esc(n.path)}</b> will be lost.`,'Delete',true,n.path))return;dkJob('Delete partition','delete_partition',n.path,{},n.path)}})}
  if(ed.length)A.appendChild(actGroup('Edit',ed));
  if(n.fstype==='crypto_LUKS')A.appendChild(actGroup('Encryption',[n.children&&n.children.length?{label:'Lock',ic:'lock',dis:crit,why:crit?sysWhy:'',fn:()=>dkAct('lock',n.path,{},'Locked')}:{label:'Unlock…',ic:'lock',cls:'pri',fn:async()=>{const r=await promptBox('Unlock encrypted volume',[{id:'passphrase',label:'Passphrase',type:'password'}],'Unlock');if(r)dkAct('unlock',n.path,{passphrase:r.passphrase})}},{label:'Change passphrase…',ic:'lock',fn:async()=>{const r=await promptBox('Change passphrase',[{id:'old',label:'Current',type:'password'},{id:'new',label:'New',type:'password'},{id:'new2',label:'Confirm new',type:'password'}],'Change');if(!r)return;if(r.new!==r.new2){toast('New passphrases do not match','err');return}dkAct('changepass',n.path,{old:r.old,new:r.new},'Passphrase changed')}}]));
  A.appendChild(actGroup('Performance',[{label:'Benchmark',ic:'gauge',fn:()=>benchDlg(n.path)}]));
  const leaves=fsLeaves(n);if(leaves.length&&!(leaves.length===1&&leaves[0]===leaf)){
    $('#dkLeaves').innerHTML=`<h3 style="margin-top:18px">${icon('list')}Filesystems inside this volume</h3><div class="tbl-wrap" style="max-height:260px"><table class="t"><thead><tr><th>Device</th><th>Type</th><th>Label</th><th>Size</th><th>Mounted at</th><th style="text-align:right">Actions</th></tr></thead><tbody>${leaves.map(l=>{const c=isCrit(l),m=l.mountpoints.length;return `<tr data-p="${esc(l.path)}"><td class="mono">${esc(l.path)}</td><td>${esc(l.fstype)}</td><td>${esc(l.label||'—')}</td><td class="num">${fmtSI(l.size)}</td><td class="mono">${esc(l.mountpoints.join(', ')||'—')}</td><td style="text-align:right;white-space:nowrap">${m?`<button class="btn sm ghost" data-a="unmount"${c?' disabled title="System volume"':''}>Unmount</button>`:`<button class="btn sm" data-a="mount">Mount</button>`} <button class="btn sm ghost" data-a="check"${c?' disabled':''}>Check</button> <button class="btn sm danger" data-a="repair"${c?' disabled':''}>Repair</button> <button class="btn sm ghost" data-a="backup">Back up</button></td></tr>`}).join('')}</tbody></table></div>`;
    $('#dkLeaves').onclick=e=>{const b=e.target.closest('button[data-a]');if(!b)return;const l=leaves.find(x=>x.path===b.closest('tr').dataset.p);const a=b.dataset.a;if(a==='mount'||a==='unmount')dkAct(a,l.path);else if(a==='check'||a==='repair')fsMaint(l,a);else backupDlg(l,'image')}}
}
async function fsMaint(l,mode){const mounted=l.mountpoints.length>0;
  const r=await promptBox(`${mode==='check'?'Check':'Repair'} filesystem — ${esc(l.path)} (${esc(l.fstype)})`,Object.assign([{id:'engine',label:'Method',type:'select',options:[['auto','Automatic — UDisks2, fall back to fsck'],['udisks','UDisks2 only (same as GNOME Disks)'],['fsck','fsck tool directly (shows full output)']],value:'auto'},{id:'auto_unmount',label:'If mounted',type:'check',value:true,text:'Unmount automatically'},{id:'remount',label:'Afterwards',type:'check',value:mounted,text:'Mount again when finished'}],{note:mode==='repair'?'<div class="warnbox">Repair writes to the filesystem. If it is damaged, some data may be lost — back it up first if you can.</div>':'<div class="infobox">Check is read-only. The volume must be unmounted while it runs.</div>'}),mode==='check'?'Check':'Next');
  if(!r)return;if(mode==='repair'&&!await confirmBox('Repair filesystem',`Repair <b class="mono">${esc(l.path)}</b> (${esc(l.fstype)}).`,'Repair',true,l.path))return;
  dkJob(mode==='check'?'Check filesystem':'Repair filesystem',mode,l.path,{engine:r.engine,auto_unmount:r.auto_unmount,remount:r.remount},mode==='repair'?l.path:null,j=>{const R=j.result||{};const ok=R.clean;return `${ok===true?`<span class="badge g">${mode==='check'?'Filesystem is clean':'Repair completed — filesystem is clean'}</span>`:ok===false?`<span class="badge r">${mode==='check'?'Errors found — run Repair':'Repair finished with problems — see output'}</span>`:'<span class="badge">Finished</span>'} <span class="muted">via ${esc(R.engine||'?')}</span>${R.remount_error?` <span class="badge a">Remount failed: ${esc(R.remount_error)}</span>`:''}`})}
const FS_OPTS=[['ext4','ext4 — Linux'],['xfs','XFS — Linux'],['btrfs','Btrfs — Linux'],['exfat','exFAT — all systems'],['vfat','FAT32 — all systems'],['ntfs','NTFS — Windows'],['f2fs','F2FS — flash'],['swap','Linux swap'],['empty','No filesystem']];
async function fmtPart(n){const r=await promptBox(`Format ${esc(n.path)}`,[{id:'fstype',label:'Type',type:'select',options:FS_OPTS,value:'ext4'},{id:'label',label:'Volume label',ph:'DATA'},{id:'passphrase',label:'LUKS2 passphrase',type:'password',ph:'leave empty for no encryption'},{id:'erase',label:'Erase',type:'select',options:[['','Quick (don\'t overwrite)'],['zero','Overwrite with zeros (slow)']],value:''}],'Next');if(!r)return;
  if(!await confirmBox('Format volume',`Format <b class="mono">${esc(n.path)}</b> as <b>${esc(r.fstype)}</b>${r.passphrase?' with LUKS2 encryption':''}. All data on it will be lost.`,'Format',true,n.path))return;dkJob('Format volume','format',n.path,r,n.path)}
async function fmtDisk(d){const r=await promptBox(`Format disk ${esc(d.path)}`,[{id:'fstype',label:'Partitioning',type:'select',options:[['gpt','GPT — modern systems / UEFI'],['dos','MBR / DOS — legacy BIOS'],['empty','No partitioning (empty)']],value:'gpt'},{id:'erase',label:'Erase',type:'select',options:[['','Quick'],['zero','Overwrite whole disk with zeros (slow)']],value:''}],'Next');if(!r)return;
  if(!await confirmBox('Format disk',`Every partition and all data on <b class="mono">${esc(d.path)}</b> (${fmtSI(d.size)} ${esc(d.model||'')}) will be destroyed.`,'Format disk',true,d.path))return;dkJob('Format disk','format',d.path,r,d.path)}
async function newPartDlg(d){const fr=d.free||[];if(!fr.length)return;if(fr.length===1)return newPart(d,fr[0]);
  const r=await promptBox('Create partition — choose free space',[{id:'i',label:'Unallocated region',type:'select',options:fr.map((f,i)=>[String(i),`${fmtSI(f.size)} at ${fmtSI(f.offset)} (sector ${(f.offset/512).toLocaleString()})`]),value:'0'}],'Next');if(r)newPart(d,fr[+r.i])}
async function newPart(d,f){const MiB=1048576,max=Math.floor(f.size/MiB);const r=await promptBox('Create partition',[{id:'size',label:`Size (MiB, max ${max})`,type:'number',value:max},{id:'fstype',label:'Filesystem',type:'select',options:FS_OPTS,value:'ext4'},{id:'label',label:'Volume label',ph:'DATA'},{id:'name',label:'Partition name (GPT)',ph:'optional'},{id:'passphrase',label:'LUKS2 passphrase',type:'password',ph:'empty = not encrypted'}],'Create');if(!r)return;
  const sz=Math.min(max,Math.max(1,parseInt(r.size)||0))*MiB;dkJob('Create partition','create_partition',d.path,{offset:f.offset,size:sz,fstype:r.fstype,label:r.label,name:r.name,passphrase:r.passphrase})}
async function resizeDlg(d,n){const MiB=1048576;const after=(d.free||[]).find(f=>f.offset>=n.start+n.size&&f.offset-(n.start+n.size)<2*MiB);const max=Math.floor((n.size+(after?after.size+(after.offset-(n.start+n.size)):0))/MiB);const minUsed=n.fsused?Math.ceil(n.fsused/MiB)+64:1;
  const r=await promptBox(`Resize ${esc(n.path)}`,[{id:'size',label:`New size MiB (${minUsed}–${max})`,type:'number',value:Math.floor(n.size/MiB)}],'Next');if(!r)return;const v=parseInt(r.size);if(!(v>=minUsed&&v<=max)){toast('Size out of range','err');return}
  if(!await confirmBox('Resize partition',`Resize <b class="mono">${esc(n.path)}</b> from ${fmtSI(n.size)} to ${fmtSI(v*MiB)}. Shrinking requires a filesystem that supports it (ext4 offline, Btrfs, NTFS); XFS cannot shrink. Back up first.`,'Resize',true,n.path))return;dkJob('Resize partition','resize',n.path,{size:v*MiB},n.path)}
function stamp(){const d=new Date();return `${d.getFullYear()}${pad(d.getMonth()+1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`}
async function backupDlg(n,mode){if(!n)return;const mounted=(n.mountpoints||[]).length>0;const z=DK.tools&&DK.tools.zstd;
  const nm=(n.label||n.model||n.name||'disk').replace(/[^A-Za-z0-9_.-]+/g,'_');
  const fields=[{id:'mode',label:'Backup type',type:'select',options:[['image','Image — exact block-level copy (restorable to a device)']].concat(mounted?[['files','Files — tar archive of the mounted filesystem']]:[]),value:mode},
    {id:'compression',label:'Compression',type:'select',options:[['zstd','zstd — fast, small'+(z?'':' (not installed)')],['gzip','gzip — compatible'],['none','None — raw size']],value:z?'zstd':'gzip'},
    {id:'dest',label:'Save as',value:`~/Backups/${nm}-${stamp()}`},{id:'checksum',label:'Integrity',type:'check',value:true,text:'Write a SHA-256 checksum file'}];
  fields.note=`<div class="infobox">Source <b class="mono">${esc(n.path)}</b> · ${fmtSI(n.size)}${n.fsused!=null?` · ${fmtSI(n.fsused)} used`:''}. The extension (.img/.tar + .gz/.zst) is added automatically. Save to a different drive than the one you are backing up.</div>${mounted?'<div class="warnbox">This volume is mounted. An image of a filesystem that is being written to may be inconsistent — unmount it first, or choose the Files type.</div>':''}`;
  const r=await promptBox(`Back up ${esc(n.path)}`,fields,'Start backup');if(!r)return;
  dkJob('Backup','backup',n.path,r,null,j=>{const R=j.result||{};return `<span class="badge g">Backup complete</span><div class="kv" style="margin-top:10px;width:100%"><div>File</div><div class="mono">${esc(R.file)}</div><div>Size</div><div>${fmtB(R.size)} (from ${fmtB(R.source_bytes)})</div><div>SHA-256</div><div class="mono dim" style="font-size:11.5px">${esc(R.sha256||'not computed')}</div><div>Duration</div><div>${R.duration}s</div></div>`})}
async function imgRestore(n,src){const r=await promptBox(`Restore image to ${esc(n.path)}`,[{id:'src',label:'Image file',value:src||'',ph:'~/Backups/disk.img.zst'},{id:'verify',label:'Integrity',type:'check',value:true,text:'Verify the .sha256 checksum first (if present)'}],'Next');if(!r)return;
  if(!await confirmBox('Restore disk image',`Everything on <b class="mono">${esc(n.path)}</b> (${fmtSI(n.size)}) will be overwritten by <span class="mono">${esc(r.src)}</span>.`,'Restore',true,n.path))return;dkJob('Restore image','image_restore',n.path,{src:r.src,verify:r.verify},n.path)}
async function filesRestore(l,src){const t=l?l.mountpoints[0]:'';const r=await promptBox('Restore files from archive',[{id:'src',label:'Archive (.tar/.tar.gz/.tar.zst)',value:src||'',ph:'~/Backups/data.tar.zst'},{id:'target',label:'Extract into folder',value:t},{id:'verify',label:'Integrity',type:'check',value:true,text:'Verify the .sha256 checksum first (if present)'}],'Next');if(!r)return;
  if(!await confirmBox('Restore files',`Extract <span class="mono">${esc(r.src)}</span> into <b class="mono">${esc(r.target)}</b>. Existing files with the same names are overwritten.`,'Restore',true,r.target))return;
  opRunJob('Restore files',r.target,api('/api/disk/action',{action:'files_restore',params:{src:r.src,target:r.target,verify:r.verify},confirm:r.target}),()=>'<span class="badge g">Files restored</span>')}
function ptMenu(d){openModal(`Partition table — ${esc(d.path)} (${esc((d.pttype||'').toUpperCase())})`,`<div class="infobox">The partition table records where each partition starts and ends. Backing it up is quick (a few KB) and lets you recover from an accidental deletion or a damaged table.</div>
  <div class="agrid" style="margin-top:12px"><div class="agrp"><div class="agt">Inspect</div><div class="toolbar"><button class="btn sm" id="ptV">${icon('shield')}Verify table</button></div></div>
  <div class="agrp"><div class="agt">Back up / restore</div><div class="toolbar"><button class="btn sm pri" id="ptB">${icon('save')}Back up table…</button><button class="btn sm danger" id="ptR"${d.critical?' disabled title="System disk"':''}>${icon('image')}Restore table…</button></div></div>
  ${d.pttype==='gpt'?`<div class="agrp"><div class="agt">GPT repair</div><div class="toolbar"><button class="btn sm danger" id="ptG">${icon('gear')}Repair GPT (relocate backup header)</button></div><div class="dim" style="font-size:11.5px;margin-top:6px">Fixes the "backup GPT header not at end of disk" problem after cloning to a larger disk or growing a VM disk. Requires gdisk.</div></div>`:''}</div>`,[{label:'Close'}]);
  $('#ptV').onclick=()=>{closeModal();dkJob('Verify partition table','pt_verify',d.path,{})};
  $('#ptB').onclick=async()=>{closeModal();const r=await promptBox('Back up partition table',[{id:'dest',label:'Save as',value:`~/Backups/${d.name}-ptable-${stamp()}`}],'Save');if(!r)return;dkAct('pt_backup',d.path,{dest:r.dest})};
  if($('#ptR'))$('#ptR').onclick=async()=>{closeModal();const r=await promptBox('Restore partition table',[{id:'src',label:'sfdisk dump file',ph:'~/Backups/sda-ptable.sfdisk'}],'Next');if(!r)return;if(!await confirmBox('Restore partition table',`Replace the partition table of <b class="mono">${esc(d.path)}</b> with <span class="mono">${esc(r.src)}</span>. Partition data is not touched, but a wrong table makes partitions unreadable.`,'Restore',true,d.path))return;dkJob('Restore partition table','pt_restore',d.path,{src:r.src},d.path)};
  if($('#ptG'))$('#ptG').onclick=async()=>{closeModal();if(!await confirmBox('Repair GPT',`Run <span class="mono">sgdisk -e</span> on <b class="mono">${esc(d.path)}</b> to move the backup GPT header to the end of the disk, then verify.`,'Repair',true,d.path))return;dkJob('Repair GPT','pt_repair_gpt',d.path,{},d.path)}}
async function loadBk(){try{const r=await api('/api/backups');DK.bk=r.backups;renderBk()}catch(e){}}
function renderBk(){$('#bkTbl tbody').innerHTML=DK.bk.map((b,i)=>`<tr data-i="${i}"><td>${fmtDate(b.ts)}</td><td><span class="badge ${b.type==='image'?'b':b.type==='files'?'v':''}">${esc(b.type==='ptable'?'partition table':b.type)}</span></td><td><b>${esc(b.label||'')}</b> <span class="mono dim">${esc(b.dev)}</span></td><td class="mono trunc" title="${esc(b.file)}">${esc(b.file)}</td><td class="num">${b.exists?fmtB(b.size):'<span class="badge r">missing</span>'}</td><td>${esc(b.compression)}</td><td>${b.sha256?'<span class="badge g">SHA-256</span>':'—'}</td>
  <td style="text-align:right;white-space:nowrap"><button class="btn sm ghost" data-a="verify"${b.sha256&&b.exists?'':' disabled'}>Verify</button> <button class="btn sm ghost" data-a="open"${b.exists?'':' disabled'}>Folder</button> <button class="btn sm danger" data-a="restore"${b.exists?'':' disabled'}>Restore…</button> <button class="btn sm ghost" data-a="forget">Forget</button></td></tr>`).join('')||'<tr><td colspan="8" class="empty">No backups yet. Use “Back up disk…” or “Back up as image…”.</td></tr>'}
async function bkClick(e){const btn=e.target.closest('button[data-a]');if(!btn)return;const b=DK.bk[+btn.closest('tr').dataset.i];const a=btn.dataset.a;
  if(a==='open')return api('/api/open',{path:b.file}).catch(terr);
  if(a==='verify'){opRunJob('Verify backup',b.file,api('/api/backups/verify',{file:b.file}),()=>'<span class="badge g">Checksum matches — backup is intact</span>',{noRefresh:true});return}
  if(a==='forget'){const ok=await new Promise(res=>{let d=false;openModal('Forget backup',`Remove <span class="mono">${esc(b.file)}</span> from the list?<div style="margin-top:10px"><label class="chk"><input type="checkbox" id="bkDel"> Also delete the file from disk</label></div>`,[{label:'Cancel'},{label:'Forget',cls:'danger',onClick:()=>{d=true;res({del:$('#bkDel').checked})}}],()=>{if(!d)res(null)})});if(!ok)return;try{await api('/api/backups/forget',{file:b.file,delete_file:ok.del});loadBk()}catch(err){terr(err)}return}
  if(a==='restore'){if(b.type==='files')return filesRestore(null,b.file);
    const targets=[];DK.list.forEach(d=>{const walk=n=>{if(!isCrit(n)&&n.type!=='rom'&&n.size>=(b.source_bytes||0))targets.push(n);(n.children||[]).forEach(walk)};walk(d)});
    if(!targets.length){toast('No non-system device is large enough for this image','err');return}
    const r=await promptBox('Restore backup to…',[{id:'dev',label:'Target device',type:'select',options:targets.map(t=>[t.path,`${t.path} · ${fmtSI(t.size)} ${t.model||t.label||t.fstype||''}`]),value:targets[0].path}],'Next');if(!r)return;
    const t=targets.find(x=>x.path===r.dev);if(b.type==='ptable'){if(!await confirmBox('Restore partition table',`Write the saved table to <b class="mono">${esc(t.path)}</b>.`,'Restore',true,t.path))return;return dkJob('Restore partition table','pt_restore',t.path,{src:b.file},t.path)}
    return imgRestore(t,b.file)}}
async function benchDlg(dev){const r=await promptBox(`Benchmark ${esc(dev)} (read-only)`,[{id:'samples',label:'Transfer-rate samples',type:'number',value:50},{id:'sample_mib',label:'Sample size (MiB)',type:'number',value:10},{id:'access',label:'Access-time samples',type:'number',value:200}],'Start');if(!r)return;
  openModal(`Benchmark — ${esc(dev)}`,`<div class="row sb"><span id="bmMsg" class="muted">Waiting for authorization…</span><span class="spin" id="bmSpin"></span></div><div class="metrics" style="grid-template-columns:repeat(4,1fr);margin:14px 0"><div class="metric"><div class="k">AVG READ</div><div class="v" id="bmAvg">—</div></div><div class="metric"><div class="k">MIN</div><div class="v" id="bmMin">—</div></div><div class="metric"><div class="k">MAX</div><div class="v" id="bmMax">—</div></div><div class="metric"><div class="k">ACCESS</div><div class="v" id="bmAcc">—</div></div></div><canvas class="chart" id="chBench" style="height:240px"></canvas><div class="legend" style="margin-top:6px"><span><i style="background:#22d3ee"></i>Read rate vs position (%)</span><span><i style="background:#fbbf24"></i>Access time (scaled)</span></div>`,[{label:'Close'}],()=>{if(BMJ)api('/api/job/cancel',{id:BMJ}).catch(()=>{})});
  const ch=Chart($('#chBench'),{fmt:v=>fmtSI(v,0)+'/s',xlabels:()=>[]});
  const bop=opStart({title:'Benchmark',dev,steps:['Transfer rate','Access time'],msg:'Waiting for authorization…'});
  try{const {job}=await api('/api/disk/action',{action:'benchmark',dev,params:{samples:+r.samples,sample_mib:+r.sample_mib,access:+r.access}});BMJ=job;bop.job=job;
    const show=(rates,acc)=>{if(!$('#chBench'))return;const mx=Math.max(...rates.map(x=>x.Bps),1);const am=Math.max(...(acc||[]).map(a=>a.ms),1);ch.set({series:[{values:rates.map(x=>x.Bps),x:rates.map(x=>x.pos),color:'#22d3ee',fill:true},{values:(acc||[]).map(a=>a.ms/am*mx*0.9),x:(acc||[]).map(a=>a.pos),color:'#fbbf24',dots:true}],n:101})};
    const j=await waitJob(job,j=>{const rs=(j.live&&j.live.rates)||[];opUpd(bop,{progress:j.progress,indet:!(j.progress>0),msg:j.message,step:j.progress>=0.8?1:0,rate:rs.length?rs[rs.length-1].Bps:null,series:rs.map(x=>+(x.Bps/1e6).toFixed(1))});if($('#bmMsg'))$('#bmMsg').textContent=j.message;if(rs.length)show(rs)});BMJ=null;const R=j.result;
    if(j.state==='cancelled')opEnd(bop,null,'Cancelled');else opEnd(bop,true,R?`Average read ${fmtSI(R.avg_Bps)}/s · access ${R.avg_access_ms.toFixed(2)} ms`:'Completed');if(!R||!$('#bmAvg'))return;
    $('#bmAvg').innerHTML=fmtSI(R.avg_Bps)+'<small>/s</small>';$('#bmMin').innerHTML=fmtSI(R.min_Bps)+'<small>/s</small>';$('#bmMax').innerHTML=fmtSI(R.max_Bps)+'<small>/s</small>';$('#bmAcc').innerHTML=R.avg_access_ms.toFixed(2)+'<small>ms</small>';$('#bmSpin').remove();$('#bmMsg').textContent=`Completed · ${R.rates.length} × ${R.sample_mib} MiB samples, ${R.access.length} random 4 KiB reads`;show(R.rates,R.access)}catch(e){BMJ=null;opEnd(bop,false,e.message);if($('#bmMsg'))$('#bmMsg').innerHTML=`<span style="color:var(--red)">${esc(e.message)}</span>`;if($('#bmSpin'))$('#bmSpin').remove()}}
let BMJ=null;

async function smartDlg(d){openModal(`SMART — ${esc(d.model||d.path)}`,'<div class="row"><span class="spin"></span>Loading…</div>',[{label:'Close'}]);
  const mb=()=>$('#modal .mb');
  const render=(r,detail)=>{const s=(r.summary||{}).smart;if(!mb())return;
    if(!s){mb().innerHTML='<div class="infobox">UDisks2 does not report SMART for this drive (common for USB bridges, virtual disks and SD cards).'+(DK.meta&&DK.meta.smartctl?' Try "Detailed attributes" (smartctl).':' Install smartmontools for more options.')+'</div><div class="row"><button class="btn sm" id="smDet">Detailed attributes</button></div><div id="smTbl"></div>';}
    else{mb().innerHTML=`<div class="grid g12"><div class="c5"><div id="gSmart"></div></div><div class="c7"><div class="kv"><div>Overall</div><div>${s.failing?'<span class="badge r">DISK IS FAILING — back up now</span>':'<span class="badge g">Disk is OK</span>'}</div><div>Temperature</div><div>${s.temp_c!=null?s.temp_c+' °C':'—'}</div><div>Powered on</div><div>${s.power_on_hours!=null?s.power_on_hours.toLocaleString()+' hours ('+(s.power_on_hours/24/365).toFixed(1)+' years)':'—'}</div>${s.type==='ata'?`<div>Bad sectors</div><div>${s.bad_sectors??'—'}</div><div>Attributes failing</div><div>${s.attrs_failing??'—'} now · ${s.attrs_failed_past??'—'} in past</div>`:`<div>Critical warnings</div><div>${(s.critical_warning||[]).join(', ')||'none'}</div>`}<div>Self-test</div><div>${esc(s.selftest_status||'—')}${s.selftest_remaining>0?` (${s.selftest_remaining}% remaining)`:''}</div><div>Updated</div><div>${s.updated?fmtDate(s.updated):'—'}</div></div>
      <div class="toolbar" style="margin-top:14px"><button class="btn sm" data-t="short">Short self-test</button><button class="btn sm" data-t="extended">Extended self-test</button>${s.type==='ata'?'<button class="btn sm ghost" data-t="conveyance">Conveyance</button>':''}<button class="btn sm ghost" data-t="abort">Abort test</button><button class="btn sm ghost" id="smDet">Detailed attributes</button></div></div></div><div id="smTbl" style="margin-top:14px"></div>`;
      const g=SegGauge($('#gSmart'),{label:'HEALTH',pal:s.failing?'amber':'green',n:44});g.set(s.failing?25:100,s.failing?'BAD':'OK',s.temp_c!=null?s.temp_c+' °C':'');
      mb().querySelectorAll('[data-t]').forEach(b=>b.onclick=async()=>{try{await api('/api/disk/action',{action:b.dataset.t==='abort'?'smart_abort':'smart_test',dev:d.path,params:{type:b.dataset.t}});toast(b.dataset.t==='abort'?'Self-test aborted':'Self-test started — progress updates in this dialog when reopened','ok')}catch(e){terr(e)}})}
    $('#smDet').onclick=async()=>{$('#smTbl').innerHTML='<div class="row"><span class="spin"></span>Reading SMART attributes (authorize if prompted)…</div>';try{const x=await api('/api/disk/smart?detail=1&dev='+encodeURIComponent(d.path));
      $('#smTbl').innerHTML=`<div class="muted" style="margin-bottom:8px">Source: ${esc(x.source)}${x.passed!=null?' · overall-health self-assessment: '+(x.passed?'<span class="badge g">PASSED</span>':'<span class="badge r">FAILED</span>'):''}</div><div class="tbl-wrap" style="max-height:340px"><table class="t"><thead><tr><th>ID</th><th>Attribute</th><th>Value</th><th>Worst</th><th>Thresh</th><th>Raw / Pretty</th><th>Failed</th></tr></thead><tbody>${x.attributes.map(a=>`<tr><td class="num">${esc(a.id)}</td><td>${esc(a.name)}${a.prefail?' <span class="badge a">pre-fail</span>':''}</td><td class="num">${esc(a.value)}</td><td class="num">${esc(a.worst)}</td><td class="num">${esc(a.thresh)}</td><td class="mono">${esc(a.raw)}</td><td>${esc(a.when_failed||'')}</td></tr>`).join('')}</tbody></table></div>`}catch(e){$('#smTbl').innerHTML=`<div class="warnbox">${esc(e.message)}</div>`}}};
  try{render(await api('/api/disk/smart?dev='+encodeURIComponent(d.path)))}catch(e){if(mb())mb().innerHTML=`<div class="warnbox">${esc(e.message)}</div>`}}
SHOW.disks=()=>{opRender();loadDk({render:true});clearInterval(DK.poll);DK.poll=setInterval(dkPollTick,2000)};HIDE.disks=()=>clearInterval(DK.poll);

/* ============================================================ duplicates + search (v1.1) */
let DU={job:null,res:null,sel:new Set(),mode:'dup',sres:null,ssel:new Set()};
function initDupes(){
  G.dup=SegGauge($('#gDupes'),{label:'SCAN',pal:'cyan',n:48});G.dup.set(0,'0<tspan class="g-unit">%</tspan>','IDLE');
  $('#duRoots').value=lsGet('du_roots','~');
  $('#duTabs').onclick=e=>{const b=e.target.closest('button');if(!b)return;DU.mode=b.dataset.m;$$('#duTabs button').forEach(x=>x.classList.toggle('on',x===b));duModeUi()};
  $('#duStart').onclick=startDupes;$('#duCancel').onclick=()=>{if(DU.job)api('/api/job/cancel',{id:DU.job})};
  $('#duAuto').onchange=e=>{autoSel(e.target.value);e.target.value=''};
  $('#duFilter').oninput=()=>DU.mode==='search'?renderSearch():renderDupes();
  $('#duRes').addEventListener('change',e=>{const c=e.target.closest('input[data-p]');if(!c)return;const S=DU.mode==='search'?DU.ssel:DU.sel;c.checked?S.add(c.dataset.p):S.delete(c.dataset.p);updDuSel()});
  $('#duRes').addEventListener('click',e=>{const o=e.target.closest('[data-open]');if(o){api('/api/open',{path:o.dataset.open}).catch(terr);return}const h=e.target.closest('.grp-hd');if(h&&!e.target.closest('input')){const bd=h.nextElementSibling;bd.style.display=bd.style.display==='none'?'':'none'}
    const all=e.target.closest('#srAll');if(all){const rows=filtSearch();rows.forEach(r=>all.checked?DU.ssel.add(r.path):DU.ssel.delete(r.path));renderSearch()}});
  $('#duDelete').onclick=delSelected;
  duModeUi();
}
function duModeUi(){const m=DU.mode;$$('[data-mode]').forEach(e=>e.style.display=e.dataset.mode.split(' ').includes(m)?'':'none');
  $('#duStart').innerHTML=icon('search')+({dup:'Find duplicates',of:'Find copies of this file',search:'Search'}[m]);
  $('#duHint').textContent={dup:'Matches by size → first 64 KiB hash → full BLAKE2b hash. Hard links are counted once.',of:'Finds every file with the same size and content as the reference file.',search:'Name and text matching is case-insensitive unless "Match case" is on. Binary files are skipped for text search.'}[m];
  $('#duL2').textContent=m==='search'?'Matches':'Duplicate groups';$('#duL3').textContent=m==='search'?'Total size':'Reclaimable';
  $('#duAuto').style.display=m==='search'?'none':'';
  if(m==='search')renderSearch();else renderDupes();}
function duBody(){const roots=$('#duRoots').value.split('\n').map(s=>s.trim()).filter(Boolean);lsSet('du_roots',$('#duRoots').value);
  return {roots,hidden:$('#duHidden').checked,excludes:$('#duEx').value.split(',').map(s=>s.trim()).filter(Boolean)}}
async function startDupes(){const m=DU.mode,body=duBody();let url;
  if(m==='dup'){Object.assign(body,{min_size:Math.max(1,(+$('#duMin').value||1)*(+$('#duMinU').value)),max_size:(+$('#duMax').value||0)*1048576,includes:$('#duInc').value.split(',').map(s=>s.trim()).filter(Boolean)});url='/api/dupes/start'}
  else if(m==='of'){const f=$('#duRef').value.trim();if(!f){toast('Enter the path of the reference file','err');return}body.file=f;url='/api/dupes/of'}
  else{const nm=$('#srName').value.trim(),tx=$('#srText').value;if(!nm&&!tx){toast('Enter a file name and/or text to search for','err');return}
    Object.assign(body,{name:nm,name_mode:$('#srNameMode').value,text:tx,case:$('#srCase').checked,text_regex:$('#srRegex').checked,min_size:(+$('#srMin').value||0)*1024,max_size:(+$('#srMax').value||0)*1048576,days:+$('#srDays').value||0,max_results:+$('#srMaxRes').value||2000,max_file_mb:+$('#srMaxFile').value||50});url='/api/search/start'}
  $('#duStart').disabled=true;$('#duCancel').disabled=false;$('#duRes').innerHTML='<div class="empty"><span class="spin"></span> Working…</div>';
  try{const {job}=await api(url,body);DU.job=job;const j=await waitJob(job,j=>{const L=j.live||{};
      if(m==='search'){G.dup.set((Date.now()/40)%100,`${(L.matches||0).toLocaleString()}`,'SEARCHING');$('#duFiles').textContent=(L.files||0).toLocaleString();$('#duGroups').textContent=(L.matches||0).toLocaleString()}
      else{G.dup.set(m==='of'?(Date.now()/40)%100:j.progress*100,m==='of'?`${L.groups?'✓':'…'}`:`${Math.round(j.progress*100)}<tspan class="g-unit">%</tspan>`,(L.phase==='scan'?'INDEXING':L.phase==='hash'?'HASHING':'STARTING'));$('#duFiles').textContent=(L.files||0).toLocaleString();$('#duGroups').textContent=(L.groups||0).toLocaleString();$('#duWaste').textContent=fmtB(L.wasted||0)}});
    if(j.state==='cancelled'){$('#duRes').innerHTML='<div class="empty">Cancelled.</div>';G.dup.set(0,'—','CANCELLED')}
    else if(m==='search'){DU.sres=j.result;DU.ssel.clear();G.dup.set(100,j.result.results.length.toLocaleString(),'MATCHES');$('#duFiles').textContent=j.result.scanned.toLocaleString();renderSearch()}
    else{DU.res=j.result;DU.sel.clear();G.dup.set(100,'100<tspan class="g-unit">%</tspan>','COMPLETE');$('#duFiles').textContent=j.result.scanned.toLocaleString();renderDupes()}}
  catch(e){terr(e);$('#duRes').innerHTML=`<div class="empty">${esc(e.message)}</div>`}
  DU.job=null;$('#duStart').disabled=false;$('#duCancel').disabled=true}
function renderDupes(){const R=DU.res;if(!R){$('#duRes').innerHTML='<div class="empty">No scan yet.</div>';updDuSel();return}const q=$('#duFilter').value.toLowerCase();
  const groups=q?R.groups.filter(g=>g.files.some(f=>f.path.toLowerCase().includes(q))):R.groups;
  const tot=R.groups.reduce((a,g)=>a+g.wasted,0);$('#duGroups').textContent=R.groups.length.toLocaleString();$('#duWaste').textContent=fmtB(tot);
  if(!groups.length){$('#duRes').innerHTML=`<div class="empty">${R.groups.length?'No groups match the filter.':R.reference?'No other copies of this file were found.':'No duplicates found. 🎉'}</div>`;updDuSel();return}
  $('#duRes').innerHTML=(R.truncated?'<div class="infobox">Showing the 5,000 groups with the most reclaimable space.</div>':'')+groups.slice(0,800).map(g=>`<div class="grp-card"><div class="grp-hd">${icon('copy')}<b>${g.files.length} copies</b><span class="muted">· ${fmtB(g.size)} each · ${fmtB(g.wasted)} reclaimable</span><span class="spacer"></span><span class="mono dim" style="font-size:11px">${g.hash.slice(0,12)}</span></div><div class="grp-bd">${g.files.map(f=>`<div class="item"><input type="checkbox" data-p="${esc(f.path)}"${DU.sel.has(f.path)?' checked':''}>${f.ref?'<span class="badge b">REFERENCE</span>':''}<span class="p" title="${esc(f.path)}">${hl(f.path,q)}</span><span class="s">${new Date(f.mtime*1000).toLocaleDateString()}</span><button class="btn ico ghost" data-open="${esc(f.path)}" title="Open folder">${icon('folder')}</button></div>`).join('')}</div></div>`).join('')+(groups.length>800?`<div class="empty">${groups.length-800} more groups not shown — narrow with the filter or use Auto-select.</div>`:'');updDuSel()}
function hl(s,q,a,b){s=String(s);if(a!=null&&b!=null&&b>a)return esc(s.slice(0,a))+'<mark>'+esc(s.slice(a,b))+'</mark>'+esc(s.slice(b));if(!q)return esc(s);const i=s.toLowerCase().indexOf(q);if(i<0)return esc(s);return esc(s.slice(0,i))+'<mark>'+esc(s.slice(i,i+q.length))+'</mark>'+esc(s.slice(i+q.length))}
function filtSearch(){const R=DU.sres;if(!R)return [];const q=$('#duFilter').value.toLowerCase();return q?R.results.filter(r=>(r.path+' '+(r.preview||'')).toLowerCase().includes(q)):R.results}
function renderSearch(){const R=DU.sres;if(!R){$('#duRes').innerHTML='<div class="empty">Enter a file name and/or text, then press Search.</div>';updDuSel();return}
  const rows=filtSearch(),q=$('#duFilter').value.toLowerCase();$('#duGroups').textContent=R.results.length.toLocaleString();$('#duWaste').textContent=fmtB(R.results.reduce((a,r)=>a+r.size,0));
  const note=(R.truncated?`<div class="infobox">Result limit reached — refine the search to see more.</div>`:'')+(R.skipped_large?`<div class="infobox">${R.skipped_large} file(s) larger than the text-search size limit were skipped.</div>`:'');
  if(!rows.length){$('#duRes').innerHTML=note+'<div class="empty">No matching files.</div>';updDuSel();return}
  const hasText=rows.some(r=>r.preview!=null);
  $('#duRes').innerHTML=note+`<div class="tbl-wrap" style="max-height:560px"><table class="t"><thead><tr><th style="width:34px"><input type="checkbox" id="srAll"></th><th>Name</th><th>Folder</th>${hasText?'<th>Match</th>':''}<th>Size</th><th>Modified</th><th></th></tr></thead><tbody>${rows.slice(0,3000).map(r=>{const i=r.path.lastIndexOf('/');const dir=r.path.slice(0,i)||'/',fn=r.path.slice(i+1);return `<tr><td><input type="checkbox" data-p="${esc(r.path)}"${DU.ssel.has(r.path)?' checked':''}></td><td><b>${hl(fn,q)}</b></td><td class="mono dim trunc" title="${esc(dir)}">${esc(dir)}</td>${hasText?`<td class="mono trunc" style="max-width:520px" title="${esc(r.preview||'')}">${r.preview!=null?`<span class="badge">L${r.line}</span> ${hl(r.preview,null,r.hl&&r.hl[0],r.hl&&r.hl[1])}${r.count>1?` <span class="dim">(+${r.count-1})</span>`:''}`:''}</td>`:''}<td class="num">${fmtB(r.size)}</td><td class="num">${new Date(r.mtime*1000).toLocaleDateString()}</td><td><button class="btn ico ghost" data-open="${esc(r.path)}" title="Open folder">${icon('folder')}</button></td></tr>`}).join('')}</tbody></table></div>`+(rows.length>3000?`<div class="empty">${rows.length-3000} more not shown.</div>`:'');updDuSel()}
function autoSel(mode){if(!DU.res)return;DU.sel.clear();if(mode!=='none')DU.res.groups.forEach(g=>{let keep;const f=g.files;if(DU.res.reference)keep=f.find(x=>x.ref);else if(mode==='newest')keep=f.reduce((a,b)=>b.mtime>a.mtime?b:a);else if(mode==='oldest')keep=f.reduce((a,b)=>b.mtime<a.mtime?b:a);else keep=f.reduce((a,b)=>b.path.length<a.path.length?b:a);f.forEach(x=>{if(x!==keep)DU.sel.add(x.path)})});renderDupes()}
function updDuSel(){const n=(DU.mode==='search'?DU.ssel:DU.sel).size;$('#duDelete').disabled=!n;$('#duDelete').innerHTML=icon('trash')+`Remove selected (${n})`}
async function delSelected(){const search=DU.mode==='search',S=search?DU.ssel:DU.sel,paths=[...S],mode=$('#duMode').value;
  const sz=search?DU.sres.results.filter(r=>S.has(r.path)).reduce((a,r)=>a+r.size,0):DU.res.groups.reduce((a,g)=>a+g.files.filter(f=>S.has(f.path)).length*g.size,0);
  if(!await confirmBox(mode==='trash'?'Move to Trash':'Delete permanently',`${mode==='trash'?'Move':'Permanently delete'} <b>${paths.length}</b> file(s), ${fmtB(sz)}?`,mode==='trash'?'Move to Trash':'Delete',true,mode==='delete'?'DELETE':null))return;
  try{const r=await api(search?'/api/search/delete':'/api/dupes/delete',{paths,mode});toast(`${r.done.length} file(s) removed`+(r.failed.length?` · ${r.failed.length} failed`:''),r.failed.length?'err':'ok');const gone=new Set(r.done);
    if(search){DU.sres.results=DU.sres.results.filter(x=>!gone.has(x.path));DU.ssel.clear();renderSearch()}
    else{DU.res.groups=DU.res.groups.map(g=>({...g,files:g.files.filter(f=>!gone.has(f.path))})).filter(g=>g.files.length>1).map(g=>({...g,wasted:g.size*(g.files.length-1)}));DU.sel.clear();renderDupes()}}catch(e){terr(e)}}

/* ============================================================ speed test */
let SPD={servers:[],pings:{},sel:lsGet('sp_srv','cf'),job:null,speedo:null,chart:null};
function initSpeed(){
  SPD.speedo=Speedo($('#speedo'));SPD.chart=Chart($('#chSpeed'),{fmt:v=>v.toFixed(0)});
  $('#spSearch').oninput=renderSrv;
  $('#spList').onclick=e=>{const r=e.target.closest('.srv');if(!r)return;SPD.sel=r.dataset.id;lsSet('sp_srv',SPD.sel);renderSrv()};
  $('#spPing').onclick=pingAll;$('#spGo').onclick=runSpeed;$('#spStop').onclick=()=>{if(SPD.job)api('/api/job/cancel',{id:SPD.job})};
  $('#spClear').onclick=async()=>{if(!await confirmBox('Clear history','Delete all locally stored speed test results?','Clear',true))return;await api('/api/speed/history/clear',{});loadHist()};
}
async function loadSrv(){try{const r=await api('/api/speed/servers');SPD.servers=r.servers;if(r.warning)$('#spNote').innerHTML=esc(r.warning)+' — Cloudflare is still available.';renderSrv();if(!Object.keys(SPD.pings).length&&SPD.servers.length>1)pingAll()}catch(e){$('#spList').innerHTML=`<div class="empty">${esc(e.message)}</div>`}}
function renderSrv(){const q=$('#spSearch').value.toLowerCase();let rows=SPD.servers.filter(s=>!q||(s.name+' '+s.sponsor).toLowerCase().includes(q));
  rows.sort((a,b)=>{if(a.id==='cf')return -1;if(b.id==='cf')return 1;const pa=SPD.pings[a.id],pb=SPD.pings[b.id];if(pa!=null&&pb!=null)return pa-pb;if(pa!=null)return -1;if(pb!=null)return 1;return a.name.localeCompare(b.name)});
  const ranked=rows.filter(s=>s.id!=='cf'&&SPD.pings[s.id]!=null).slice(0,10).map(s=>s.id);
  $('#spList').innerHTML=rows.map(s=>{const p=SPD.pings[s.id];const top=ranked.indexOf(s.id);return `<div class="srv ${s.id===SPD.sel?'on':''}" data-id="${esc(s.id)}">${icon(s.id==='cf'?'globe':'net')}<div class="nm"><b>${esc(s.name)}</b><small>${esc(s.sponsor)}${s.id==='cf'?' · anycast, auto-routes to the nearest data center':''}</small></div>${top>=0&&top<10?`<span class="badge ${top<3?'g':'b'}">TOP ${top+1}</span>`:''}<span class="pg" style="color:${p==null?'var(--dim)':p<30?'var(--green)':p<80?'var(--amber)':'var(--red)'}">${p===undefined?'':p===null?'timeout':p.toFixed(0)+' ms'}</span></div>`}).join('');
  const s=SPD.servers.find(x=>x.id===SPD.sel);$('#spSrvPill').textContent='Server: '+(s?s.name:'—')}
async function pingAll(){$('#spPing').disabled=true;$('#spPing').innerHTML='<span class="spin"></span> Pinging';try{const {job}=await api('/api/speed/pingall',{});const j=await waitJob(job,j=>{if(j.live&&j.live.pings){SPD.pings=j.live.pings;renderSrv()}});SPD.pings=j.result.pings;renderSrv()}catch(e){terr(e)}$('#spPing').disabled=false;$('#spPing').textContent='Ping all'}
async function runSpeed(){
  $('#spGo').disabled=true;$('#spStop').disabled=false;SPD.speedo.reset();['#mPing','#mJit','#mDl','#mUl'].forEach(s=>$(s).innerHTML='—<small>'+(s.includes('Ping')||s.includes('Jit')?'ms':'Mbps')+'</small>');
  let dlc=[],ulc=[];
  try{const {job}=await api('/api/speed/start',{server:SPD.sel,streams:+$('#spStreams').value,duration:+$('#spDur').value});SPD.job=job;
    const j=await waitJob(job,j=>{const L=j.live||{};
      if(L.ping!=null)$('#mPing').innerHTML=L.ping.toFixed(1)+'<small>ms</small>';if(L.jitter!=null)$('#mJit').innerHTML=L.jitter.toFixed(1)+'<small>ms</small>';
      if(L.colo)$('#spSrvPill').textContent=`Server: Cloudflare ${L.colo}`;
      if(L.phase==='ping'){const ps=L.ping_samples||[];SPD.speedo.set(0,`${icon('ping')} LATENCY ${ps.length?ps[ps.length-1].toFixed(0)+' ms':''}`,'down')}
      if(L.phase==='download'){dlc=L.curve||dlc;SPD.speedo.set(L.mbps||0,`${icon('down')} DOWNLOAD`,'down');$('#mDl').innerHTML=(L.mbps||0).toFixed(1)+'<small>Mbps</small>'}
      if(L.phase==='upload'){ulc=L.curve||ulc;if(L.download!=null)$('#mDl').innerHTML=L.download.toFixed(1)+'<small>Mbps</small>';SPD.speedo.set(L.mbps||0,`${icon('up')} UPLOAD`,'up');$('#mUl').innerHTML=(L.mbps||0).toFixed(1)+'<small>Mbps</small>'}
      $$('#spPh svg').forEach(s=>s.style.cssText='width:14px;height:14px');
      SPD.chart.set({series:[{values:dlc,color:'#22d3ee',fill:true},{values:ulc,color:'#a78bfa',fill:true}],n:Math.max(dlc.length,ulc.length,10)})});
    if(j.state==='cancelled'){SPD.speedo.set(0,'STOPPED','down')}else{const R=j.result;$('#mDl').innerHTML=R.download.toFixed(1)+'<small>Mbps</small>';$('#mUl').innerHTML=R.upload.toFixed(1)+'<small>Mbps</small>';SPD.speedo.set(R.download,`${icon('down')} ${R.download.toFixed(1)} · ${icon('up')} ${R.upload.toFixed(1)} MBPS`,'down');$$('#spPh svg').forEach(s=>s.style.cssText='width:14px;height:14px');
      SPD.chart.set({series:[{values:R.dl_curve,color:'#22d3ee',fill:true},{values:R.ul_curve,color:'#a78bfa',fill:true}],n:Math.max(R.dl_curve.length,R.ul_curve.length,10)});toast(`↓ ${R.download.toFixed(1)} Mbps · ↑ ${R.upload.toFixed(1)} Mbps · ${R.ping.toFixed(0)} ms`,'ok');loadHist()}}
  catch(e){terr(e);SPD.speedo.set(0,'<span style="color:var(--red)">FAILED</span>','down')}
  SPD.job=null;$('#spGo').disabled=false;$('#spStop').disabled=true}
async function loadHist(){try{const h=(await api('/api/speed/history')).history;$('#spHist tbody').innerHTML=h.map(r=>`<tr><td>${fmtDate(r.ts)}</td><td>${esc(r.server)}${r.colo?' <span class="badge">'+esc(r.colo)+'</span>':''}</td><td class="num">${r.ping} ms</td><td class="num">${r.jitter} ms</td><td class="num" style="color:var(--cyan)">${r.download} Mbps</td><td class="num" style="color:var(--violet)">${r.upload} Mbps</td><td class="num">${r.streams}</td></tr>`).join('')||'<tr><td colspan="7" class="empty">No tests yet.</td></tr>'}catch(e){}}
SHOW.speed=()=>{if(!SPD.servers.length)loadSrv();loadHist()};

/* ============================================================ docker (v1.1) */
let DKR={st:null,tab:'containers',items:{},stats:{},timer:null,statT:null,showAll:true,busy:false};
function initDocker(){
  G.dkC=RingGauge($('#gDkC'),{label:'CONTAINERS',pal:'cyan',icon:'docker'});G.dkI=SegGauge($('#gDkI'),{label:'DISK USAGE',pal:'violet',n:44});
  $('#dkrTabs').onclick=e=>{const b=e.target.closest('button');if(!b)return;dkrTab(b.dataset.t)};
  $('#dkrPanels').addEventListener('click',dkrClick);
  $('#dkrSearch').oninput=()=>renderDkr();$('#dkrAll').onchange=e=>{DKR.showAll=e.target.checked;renderDkr()};
  $('#dkrRefresh').onclick=()=>loadDocker(true);$('#dkrLearn').onclick=()=>dkxOpen();
}
function dkrTab(t){DKR.tab=t;$$('#dkrTabs button').forEach(x=>x.classList.toggle('on',x.dataset.t===t));$$('.dkp').forEach(p=>p.style.display=p.dataset.t===t?'':'none');
  $('#dkrTools').style.display=['containers','images','volumes','networks','compose'].includes(t)?'':'none';$('#dkrAllL').style.display=t==='containers'?'':'none';
  renderToolbar();if(t==='setup')renderSetup();else loadList(t)}
async function loadDocker(force){
  try{DKR.st=await api('/api/docker/status')}catch(e){terr(e);return}
  const s=DKR.st;renderEngine();
  const ok=s.installed&&s.access==='ok';$('#dkrMain').style.display=ok?'':'none';
  if(!s.installed){$('#dkrBanner').innerHTML='';dkrTab('setup');$('#dkrMain').style.display='';return}
  if(s.access!=='ok'){$('#dkrBanner').innerHTML=s.access==='denied'?`<div class="warnbox"><b>Docker is installed, but your user cannot reach the daemon.</b> ${s.in_group?'You are in the docker group, but this session started before that. Relaunch MySysPilot with the docker group, or log out and back in.':'Add yourself to the docker group, then relaunch.'}<div class="toolbar" style="margin-top:10px">${s.in_group?'':`<button class="btn sm pri" data-act="group_add">Add me to the docker group</button>`}<button class="btn sm" data-act="relaunch"${s.in_group?'':' disabled'}>Relaunch with docker group</button></div><div class="dim" style="font-size:12px;margin-top:6px">Membership of the docker group grants root-level privileges (Docker documentation).</div></div>`
      :`<div class="warnbox"><b>The Docker daemon is not running.</b> <span class="mono dim">${esc(s.access_error||'')}</span><div class="toolbar" style="margin-top:10px"><button class="btn sm pri" data-act="start">Start Docker</button></div></div>`;
    $('#dkrBanner').querySelectorAll('[data-act]').forEach(b=>b.onclick=()=>setupAct(b.dataset.act));$('#dkrMain').style.display='';dkrTab('setup');return}
  $('#dkrBanner').innerHTML='';if(force||!DKR.items[DKR.tab])dkrTab(DKR.tab);loadGauges();
}
function renderEngine(){const s=DKR.st,i=s.info||{};
  const run=i.ContainersRunning||0,tot=i.Containers||0;G.dkC.set(tot?100*run/tot:0,`${run}<tspan class="g-unit">/${tot}</tspan>`,s.access==='ok'?`RUNNING · ${i.ContainersPaused||0} PAUSED`:(s.installed?'DAEMON UNAVAILABLE':'NOT INSTALLED'));
  const act=s.access==='ok'?'active':(s.service_active||'unknown');
  $('#dkrEngine').innerHTML=`<div class="hd"><span class="dot ${s.access==='ok'?'':act==='active'?'amber':'red'}"></span>${s.installed?(s.access==='ok'?'ENGINE RUNNING':act==='active'?'NO ACCESS':'ENGINE STOPPED'):'NOT INSTALLED'}</div>
   <div class="ln">${icon('docker')}Engine<b>${esc(s.server||s.client||'—')}</b></div>
   <div class="ln">${icon('box')}Compose / Buildx<b>${esc(s.compose||'—')} / ${esc(s.buildx||'—')}</b></div>
   <div class="ln">${icon('repo')}Installed from<b>${esc(s.source||'—')}</b></div>
   <div class="ln">${icon('disk')}Storage<b>${esc(i.Driver||'—')} · ${esc(i.DockerRootDir||'—')}</b></div>
   <div class="ln">${icon('shield')}docker group<b>${s.user==='root'?'n/a (running as root)':s.group_active?'active':s.in_group?'member (re-login needed)':'not a member'}</b></div>
   <div class="ln" style="border:0">${icon('power')}Service<b>${esc(act)} · boot: ${esc(s.service_enabled||'—')}</b></div>
   ${s.installed?`<div class="toolbar" style="margin-top:10px">${act==='active'?'<button class="btn sm ghost" data-act="restart">Restart</button><button class="btn sm danger" data-act="stop">Stop</button>':'<button class="btn sm ok" data-act="start">Start</button>'}${s.service_enabled==='enabled'?'<button class="btn sm ghost" data-act="disable">Disable at boot</button>':'<button class="btn sm ghost" data-act="enable">Enable at boot</button>'}</div>`:''}`;
  $('#dkrEngine').querySelectorAll('[data-act]').forEach(b=>b.onclick=()=>setupAct(b.dataset.act));
  $('#dkrSys').innerHTML=s.access==='ok'?`<div class="kv"><div>Host OS</div><div>${esc(i.OperatingSystem)}</div><div>Kernel</div><div class="mono">${esc(i.KernelVersion)}</div><div>CPUs / Memory</div><div>${i.NCPU} · ${fmtB(i.MemTotal)}</div><div>Cgroup</div><div>${esc(i.CgroupDriver)} v${esc(i.CgroupVersion)}</div><div>Logging</div><div>${esc(i.LoggingDriver)}</div><div>Images</div><div>${i.Images}</div><div>Swarm</div><div>${esc(i.Swarm||'inactive')}</div></div>${(i.Warnings||[]).length?`<div class="warnbox" style="font-size:12px">${(i.Warnings||[]).map(esc).join('<br>')}</div>`:''}`:'<div class="muted">Engine details appear once the daemon is reachable.</div>';
}
async function loadGauges(){try{const r=await api('/api/docker/list?what=df');DKR.items.df=r.items;const im=r.items.find(x=>x.Type==='Images')||{};const pc=parseFloat(((im.Reclaimable||'').match(/\(([\d.]+)%\)/)||[0,0])[1]);
  const tot=r.items.map(x=>x.Size).join(' + ');G.dkI.set(100-pc,esc(im.Size||'0B'),`${pc.toFixed(0)}% RECLAIMABLE`);if(DKR.tab==='system')renderSystem()}catch(e){}}
async function loadList(t){if(!DKR.st||DKR.st.access!=='ok')return;const what=t==='system'?'df':t;
  try{const r=await api('/api/docker/list?what='+what);DKR.items[t]=r.items;if(t==='containers')loadStats();renderDkr()}catch(e){$(`.dkp[data-t="${t}"] .dkbody`).innerHTML=`<div class="empty">${esc(e.message)}</div>`}}
async function loadStats(){if(!(DKR.items.containers||[]).some(c=>c.State==='running'))return;try{const r=await api('/api/docker/list?what=stats');DKR.stats={};r.items.forEach(s=>DKR.stats[s.ID]=s);if(DKR.tab==='containers')renderDkr()}catch(e){}}
function renderToolbar(){const t=DKR.tab,h=$('#dkrBtns');const B={containers:[['run','plus','Run container…','pri']],images:[['pull','down','Pull image…','pri'],['prune_img','broom','Prune dangling'],['prune_img_all','broom','Prune unused','danger']],volumes:[['vol_create','plus','Create volume…','pri'],['prune_vol','broom','Prune unused','danger']],networks:[['net_create','plus','Create network…','pri'],['prune_net','broom','Prune unused','danger']],compose:[['compose_file','play','Up from compose file…','pri']]}[t]||[];
  h.innerHTML=B.map(b=>`<button class="btn sm ${b[3]||'ghost'}" data-tb="${b[0]}">${icon(b[1])}${b[2]}</button>`).join('');h.querySelectorAll('[data-tb]').forEach(b=>b.onclick=()=>toolAct(b.dataset.tb))}
const dkState=s=>({running:'g',paused:'a',exited:'',created:'v',restarting:'a',dead:'r',removing:'a'}[s]||'');
const shortId=id=>String(id||'').replace(/^sha256:/,'').slice(0,12);
function renderDkr(){const t=DKR.tab,q=$('#dkrSearch').value.toLowerCase(),it=DKR.items[t]||[],body=$(`.dkp[data-t="${t}"] .dkbody`);if(!body)return;const m=x=>!q||JSON.stringify(x).toLowerCase().includes(q);
  const A=(a,ic,tt,cls)=>`<button class="btn ico ${cls||'ghost'}" data-a="${a}" title="${tt}">${icon(ic)}</button>`;
  if(t==='containers'){const rows=it.filter(c=>(DKR.showAll||c.State==='running')&&m(c));
    body.innerHTML=`<div class="tbl-wrap"><table class="t"><thead><tr><th>Name</th><th>Image</th><th>State</th><th>Ports</th><th>CPU</th><th>Memory</th><th style="text-align:right">Actions</th></tr></thead><tbody>${rows.map(c=>{const s=DKR.stats[c.ID]||{},run=c.State==='running',pau=c.State==='paused';return `<tr data-id="${esc(c.ID)}" data-name="${esc(c.Names)}"><td><b>${esc(c.Names)}</b><div class="mono dim" style="font-size:11px">${shortId(c.ID)}</div></td><td class="mono trunc" style="max-width:220px" title="${esc(c.Image)}">${esc(c.Image)}</td><td><span class="badge ${dkState(c.State)}">${esc(c.State)}</span><div class="dim" style="font-size:11.5px">${esc(c.Status)}</div></td><td class="mono" style="font-size:12px;max-width:220px;white-space:normal">${esc((c.Ports||'').replace(/, /g,'\n')).replace(/\n/g,'<br>')||'—'}</td><td class="num">${run?esc(s.CPUPerc||'…'):'—'}</td><td class="num">${run?esc((s.MemUsage||'…').split(' / ')[0]):'—'}</td>
      <td style="text-align:right;white-space:nowrap">${run||pau?A('stop','stop','Stop','danger'):A('start','play','Start','ok')}${A('restart','refresh','Restart')}${run?A('pause','pause','Pause'):pau?A('unpause','play','Resume'):''}${A('logs','list','Logs')}${run?A('exec','term','Run command'):''}${A('inspect','eye','Inspect')}${A('restartpol','gear','Restart policy / rename')}${A('rm','trash','Remove','danger')}</td></tr>`}).join('')||'<tr><td colspan="7" class="empty">No containers.</td></tr>'}</tbody></table></div>`}
  else if(t==='images'){const rows=it.filter(m);body.innerHTML=`<div class="tbl-wrap"><table class="t"><thead><tr><th>Repository</th><th>Tag</th><th>ID</th><th>Created</th><th>Size</th><th style="text-align:right">Actions</th></tr></thead><tbody>${rows.map(i=>{const ref=i.Repository!=='<none>'?`${i.Repository}:${i.Tag}`:i.ID;return `<tr data-id="${esc(ref)}" data-iid="${esc(i.ID)}"><td><b class="mono">${esc(i.Repository)}</b></td><td><span class="badge ${i.Tag==='<none>'?'a':'b'}">${esc(i.Tag)}</span></td><td class="mono dim">${shortId(i.ID)}</td><td>${esc(i.CreatedSince)}</td><td class="num">${esc(i.Size)}</td><td style="text-align:right;white-space:nowrap">${A('runimg','play','Run container from image','ok')}${A('tag','list','Tag…')}${A('inspect','eye','Inspect')}${A('rmimg','trash','Remove','danger')}</td></tr>`}).join('')||'<tr><td colspan="6" class="empty">No images.</td></tr>'}</tbody></table></div>`}
  else if(t==='volumes'){const rows=it.filter(m);body.innerHTML=`<div class="tbl-wrap"><table class="t"><thead><tr><th>Name</th><th>Driver</th><th>Scope</th><th>Mountpoint</th><th style="text-align:right">Actions</th></tr></thead><tbody>${rows.map(v=>`<tr data-id="${esc(v.Name)}"><td class="mono"><b>${esc(v.Name.length>40?v.Name.slice(0,16)+'…'+v.Name.slice(-8):v.Name)}</b></td><td>${esc(v.Driver)}</td><td>${esc(v.Scope)}</td><td class="mono dim trunc">${esc(v.Mountpoint||'')}</td><td style="text-align:right;white-space:nowrap">${A('inspect','eye','Inspect')}${A('rmvol','trash','Remove','danger')}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">No volumes.</td></tr>'}</tbody></table></div>`}
  else if(t==='networks'){const rows=it.filter(m);body.innerHTML=`<div class="tbl-wrap"><table class="t"><thead><tr><th>Name</th><th>Driver</th><th>Scope</th><th>ID</th><th style="text-align:right">Actions</th></tr></thead><tbody>${rows.map(n=>{const bi=['bridge','host','none'].includes(n.Name);return `<tr data-id="${esc(n.Name)}"><td><b>${esc(n.Name)}</b>${bi?' <span class="badge">built-in</span>':''}</td><td>${esc(n.Driver)}</td><td>${esc(n.Scope)}</td><td class="mono dim">${shortId(n.ID)}</td><td style="text-align:right;white-space:nowrap">${A('inspect','eye','Inspect')}${bi?'':A('rmnet','trash','Remove','danger')}</td></tr>`}).join('')||'<tr><td colspan="5" class="empty">No networks.</td></tr>'}</tbody></table></div>`}
  else if(t==='compose'){const rows=it.filter(m);body.innerHTML=`<div class="tbl-wrap"><table class="t"><thead><tr><th>Project</th><th>Status</th><th>Compose files</th><th style="text-align:right">Actions</th></tr></thead><tbody>${rows.map(c=>`<tr data-id="${esc(c.Name)}" data-files="${esc(c.ConfigFiles||'')}"><td><b>${esc(c.Name)}</b></td><td><span class="badge ${/running/.test(c.Status)?'g':''}">${esc(c.Status)}</span></td><td class="mono dim trunc" title="${esc(c.ConfigFiles||'')}">${esc(c.ConfigFiles||'')}</td><td style="text-align:right;white-space:nowrap"><button class="btn sm ok" data-a="cup">Up</button> <button class="btn sm ghost" data-a="crestart">Restart</button> <button class="btn sm ghost" data-a="cpull">Pull</button> <button class="btn sm ghost" data-a="cstop">Stop</button> <button class="btn sm danger" data-a="cdown">Down</button></td></tr>`).join('')||'<tr><td colspan="4" class="empty">No Compose projects found. Start one with “Up from compose file…”.</td></tr>'}</tbody></table></div>`}
  else if(t==='system')renderSystem();
}
function renderSystem(){const body=$('.dkp[data-t="system"] .dkbody');const it=DKR.items.df||DKR.items.system||[];
  body.innerHTML=`<div class="tbl-wrap" style="max-height:none"><table class="t"><thead><tr><th>Type</th><th>Total</th><th>Active</th><th>Size</th><th>Reclaimable</th></tr></thead><tbody>${it.map(x=>`<tr><td><b>${esc(x.Type)}</b></td><td class="num">${esc(x.TotalCount)}</td><td class="num">${esc(x.Active)}</td><td class="num">${esc(x.Size)}</td><td class="num">${esc(x.Reclaimable)}</td></tr>`).join('')}</tbody></table></div>
  <div class="agrid" style="margin-top:16px"><div class="agrp"><div class="agt">Clean up</div><div class="toolbar"><button class="btn sm" data-sys="p1">${icon('broom')}Prune stopped containers, unused networks, dangling images, build cache</button><button class="btn sm danger" data-sys="p2">${icon('broom')}…plus all unused images</button><button class="btn sm danger" data-sys="p3">${icon('trash')}…plus unused volumes (data loss)</button></div></div></div>`;
  body.querySelectorAll('[data-sys]').forEach(b=>b.onclick=async()=>{const k=b.dataset.sys;const msg={p1:'Remove stopped containers, unused networks, dangling images and build cache?',p2:'Also remove every image not used by a container?',p3:'Also remove every volume not used by a container? Data in those volumes is deleted.'}[k];
    if(!await confirmBox('Docker system prune',msg,'Prune',true,k==='p3'?'DELETE VOLUMES':null))return;runCmdJob('docker system prune',api('/api/docker/action',{kind:'system',action:'prune',params:{all:k!=='p1',volumes:k==='p3'}}),()=>{loadDocker(true)})})}
async function dkrDo(kind,action,ids,params,msg){try{const r=await api('/api/docker/action',{kind,action,ids,params:params||{}});if(r.job){runCmdJob(msg||`${kind} ${action}`,Promise.resolve(r),()=>loadDocker(true));return r}toast(esc(r.message||msg||'Done'),'ok');loadList(DKR.tab);loadGauges();return r}catch(e){terr(e)}}
async function dkrClick(e){const b=e.target.closest('button[data-a]');if(!b)return;const tr=b.closest('tr'),id=tr.dataset.id,a=b.dataset.a,name=tr.dataset.name||id;
  b.disabled=true;setTimeout(()=>{b.disabled=false},1500);
  if(['start','stop','restart','pause','unpause'].includes(a))return dkrDo('container',a,[id],{},`${a} ${name}`);
  if(a==='rm'){const r=await new Promise(res=>{let d=false;openModal('Remove container',`Remove <b>${esc(name)}</b>?<div style="margin-top:10px"><label class="chk"><input type="checkbox" id="rmF" checked> Force (stop if running)</label> &nbsp; <label class="chk"><input type="checkbox" id="rmV"> Remove its anonymous volumes</label></div>`,[{label:'Cancel'},{label:'Remove',cls:'danger',onClick:()=>{d=true;res({force:$('#rmF').checked,volumes:$('#rmV').checked})}}],()=>{if(!d)res(null)})});if(r)dkrDo('container','rm',[id],r,`Removed ${name}`);return}
  if(a==='logs')return logsDlg(id,name);
  if(a==='exec')return execDlg(id,name);
  if(a==='inspect'){const kind={containers:'container',images:'image',volumes:'volume',networks:'network'}[DKR.tab];try{const r=await api(`/api/docker/inspect?kind=${kind}&id=${encodeURIComponent(kind==='image'?tr.dataset.iid:id)}`);openModal(`Inspect — ${esc(name)}`,`<pre class="out" style="max-height:65vh">${esc(JSON.stringify(r.data,null,2))}</pre>`)}catch(err){terr(err)}return}
  if(a==='restartpol'){const r=await promptBox(`Container settings — ${esc(name)}`,[{id:'policy',label:'Restart policy',type:'select',options:[['no','no'],['always','always'],['unless-stopped','unless-stopped'],['on-failure','on-failure']],value:'unless-stopped'},{id:'name',label:'Rename to',value:name}],'Apply');if(!r)return;await dkrDo('container','update_restart',[id],{policy:r.policy},'Restart policy updated');if(r.name&&r.name!==name)dkrDo('container','rename',[id],{name:r.name},'Renamed');return}
  if(a==='rmimg'){if(!await confirmBox('Remove image',`Remove image <b class="mono">${esc(id)}</b>?<div style="margin-top:8px"><label class="chk"><input type="checkbox" id="rmIF"> Force</label></div>`,'Remove',true))return;return dkrDo('image','rm',[id],{force:!!($('#rmIF')&&$('#rmIF').checked)},'Image removed')}
  if(a==='tag'){const r=await promptBox(`Tag ${esc(id)}`,[{id:'tag',label:'New tag',ph:'myrepo/app:1.0'}],'Tag');if(r)dkrDo('image','tag',[id],{tag:r.tag},'Tagged');return}
  if(a==='runimg')return runDlg(id);
  if(a==='rmvol'){if(!await confirmBox('Remove volume',`Delete volume <b class="mono">${esc(id)}</b> and all data in it?`,'Remove',true,id))return;return dkrDo('volume','rm',[id],{},'Volume removed')}
  if(a==='rmnet'){if(!await confirmBox('Remove network',`Remove network <b>${esc(id)}</b>?`,'Remove',true))return;return dkrDo('network','rm',[id],{},'Network removed')}
  if(a[0]==='c'&&['cup','crestart','cpull','cstop','cdown'].includes(a)){const act={cup:'up',crestart:'restart',cpull:'pull',cstop:'stop',cdown:'down'}[a];if(act==='down'&&!await confirmBox('Compose down',`Stop and remove containers and networks of project <b>${esc(id)}</b>? Named volumes are kept.`,'Down',true))return;
    return dkrDo('compose',act,[],{project:id,files:(tr.dataset.files||'').split(',').filter(Boolean)},`compose ${act} ${id}`)}
}
async function toolAct(k){
  if(k==='run')return runDlg('');
  if(k==='pull'){const r=await promptBox('Pull image',[{id:'image',label:'Image',ph:'nginx:latest'}],'Pull');if(r&&r.image)dkrDo('image','pull',[],{image:r.image.trim()},'docker pull '+r.image);return}
  if(k==='prune_img'||k==='prune_img_all'){if(!await confirmBox('Prune images',k==='prune_img'?'Remove dangling (untagged) images?':'Remove every image not used by any container?','Prune',true))return;return dkrDo('image','prune',[],{all:k==='prune_img_all'},'image prune')}
  if(k==='vol_create'){const r=await promptBox('Create volume',[{id:'name',label:'Name',ph:'app-data'}],'Create');if(r)dkrDo('volume','create',[],{name:r.name},'Volume created');return}
  if(k==='prune_vol'){if(!await confirmBox('Prune volumes','Remove all volumes not used by any container? Their data is deleted.','Prune',true,'DELETE VOLUMES'))return;return dkrDo('volume','prune',[],{all:true},'volume prune')}
  if(k==='net_create'){const r=await promptBox('Create network',[{id:'name',label:'Name',ph:'app-net'},{id:'driver',label:'Driver',type:'select',options:[['bridge','bridge'],['macvlan','macvlan'],['ipvlan','ipvlan'],['overlay','overlay (swarm)']],value:'bridge'},{id:'subnet',label:'Subnet (optional)',ph:'172.30.0.0/24'}],'Create');if(r)dkrDo('network','create',[],r,'Network created');return}
  if(k==='prune_net'){if(!await confirmBox('Prune networks','Remove all custom networks not used by any container?','Prune',true))return;return dkrDo('network','prune',[],{},'network prune')}
  if(k==='compose_file'){const r=await promptBox('Start a Compose project',[{id:'file',label:'Compose file',ph:'~/projects/app/compose.yaml'},{id:'project',label:'Project name (optional)',ph:'defaults to folder name'}],'Up');if(r&&r.file)dkrDo('compose','up',[],{files:[r.file],project:r.project||''},'compose up');return}
}
function runDlg(image){openModal('Run container',`<div class="form"><label>Image</label><input type="text" id="rnImg" value="${esc(image)}" placeholder="nginx:latest">
  <label>Name</label><input type="text" id="rnName" placeholder="optional">
  <label>Ports</label><input type="text" id="rnPorts" placeholder="8080:80, 443:443">
  <label>Environment</label><textarea id="rnEnv" rows="3" placeholder="KEY=value (one per line)"></textarea>
  <label>Volumes</label><textarea id="rnVol" rows="2" placeholder="/host/path:/container/path[:ro] or volume-name:/data"></textarea>
  <label>Restart policy</label><select id="rnRestart"><option>no</option><option selected>unless-stopped</option><option>always</option><option>on-failure</option></select>
  <label>Network</label><input type="text" id="rnNet" placeholder="optional (bridge by default)">
  <label>Command</label><input type="text" id="rnCmd" placeholder="optional override"></div>
  <div class="infobox">Runs detached (<span class="mono">docker run -d</span>). The image is pulled automatically if it is not present.</div>`,
  [{label:'Cancel'},{label:'Run',cls:'pri',onClick:()=>{const img=$('#rnImg').value.trim();if(!img){toast('Image is required','err');return false}
    const p={image:img,name:$('#rnName').value.trim(),ports:$('#rnPorts').value.split(',').map(s=>s.trim()).filter(Boolean),env:$('#rnEnv').value.split('\n').map(s=>s.trim()).filter(Boolean),volumes:$('#rnVol').value.split('\n').map(s=>s.trim()).filter(Boolean),restart:$('#rnRestart').value,network:$('#rnNet').value.trim(),command:$('#rnCmd').value.trim()};
    setTimeout(()=>dkrDo('container','run',[],p,'docker run '+img),50)}}])}
let LOGT=null;
function logsDlg(id,name){openModal(`Logs — ${esc(name)}`,`<div class="row" style="margin-bottom:10px"><select id="lgTail"><option>100</option><option selected>300</option><option>1000</option><option>5000</option></select><label class="chk"><input type="checkbox" id="lgTs"> Timestamps</label><label class="chk"><input type="checkbox" id="lgFollow" checked> Follow (2 s)</label><div class="search" style="flex:1"><span data-ic="search"></span><input type="text" id="lgFind" placeholder="Filter lines" style="width:100%"></div></div><pre class="out" id="lgOut" style="max-height:60vh;min-height:300px">Loading…</pre>`,[{label:'Close'}],()=>{clearInterval(LOGT)});
  let raw='';const show=()=>{const q=$('#lgFind').value.toLowerCase();const o=$('#lgOut');if(!o)return;const atEnd=o.scrollTop+o.clientHeight>=o.scrollHeight-20;o.textContent=q?raw.split('\n').filter(l=>l.toLowerCase().includes(q)).join('\n'):raw;if(atEnd)o.scrollTop=o.scrollHeight};
  const load=async()=>{try{const r=await api(`/api/docker/logs?id=${encodeURIComponent(id)}&tail=${$('#lgTail').value}&ts=${$('#lgTs').checked?1:0}`);raw=r.log||'(no output)';show()}catch(e){raw=e.message;show()}};
  load();$('#lgTail').onchange=load;$('#lgTs').onchange=load;$('#lgFind').oninput=show;clearInterval(LOGT);LOGT=setInterval(()=>{if(!$('#lgOut')){clearInterval(LOGT);return}if($('#lgFollow').checked)load()},2000)}
function execDlg(id,name){openModal(`Run command in ${esc(name)}`,`<div class="row"><input type="text" id="exCmd" style="flex:1" placeholder="ls -la /  ·  cat /etc/os-release" class="mono"><label class="chk"><input type="checkbox" id="exSh" checked> via sh -c</label><button class="btn pri sm" id="exGo">${icon('play')}Run</button></div><pre class="out" id="exOut" style="margin-top:12px;min-height:200px;max-height:55vh">Output appears here. Commands run non-interactively (docker exec, 60 s limit).</pre>`,[{label:'Close'}]);
  const go=async()=>{const c=$('#exCmd').value.trim();if(!c)return;$('#exOut').textContent='Running…';try{const r=await api('/api/docker/action',{kind:'container',action:'exec',ids:[id],params:{cmd:c,shell:$('#exSh').checked}});$('#exOut').textContent=(r.output||'')+`\n[exit ${r.rc}]`}catch(e){$('#exOut').textContent=e.message}};
  $('#exGo').onclick=go;$('#exCmd').onkeydown=e=>{if(e.key==='Enter')go()}}
async function setupAct(a){const s=DKR.st||{};
  if(a==='relaunch'){if(!await confirmBox('Relaunch MySysPilot','MySysPilot restarts with the docker group active and opens a new window. Close this window afterwards.','Relaunch'))return;try{await api('/api/docker/action',{kind:'setup',action:'relaunch'})}catch(e){}document.body.innerHTML='<div class="empty" style="padding-top:30vh;font-size:16px">Relaunching MySysPilot with the docker group… a new window will open. You can close this one.</div>';return}
  if(a==='stop'&&!await confirmBox('Stop Docker','Stop the Docker daemon? Running containers stop with it.','Stop',true))return;
  try{const r=await api('/api/docker/action',{kind:'setup',action:a});toast(esc(r.message||`Docker: ${a} OK`),'ok')}catch(e){terr(e)}setTimeout(()=>loadDocker(true),800)}
function renderSetup(){const s=DKR.st||{};const body=$('.dkp[data-t="setup"] .dkbody');const d=s.distro||{};
  body.innerHTML=`<div class="grid g12">
   <div class="card c6"><h3>${icon('down')}${s.installed?'Reinstall / repair Docker':'Install Docker Engine'}</h3>
    <div class="kv" style="margin-bottom:12px"><div>System</div><div>${esc(d.name||'?')} <span class="mono dim">(${esc(d.codename||'?')})</span></div><div>Status</div><div>${s.installed?`<span class="badge g">Installed</span> ${esc(s.source||'')}`:'<span class="badge a">Not installed</span>'}</div></div>
    ${d.family?'':'<div class="warnbox">Automatic install supports Ubuntu/Debian-family systems (apt). For other distributions, follow docs.docker.com.</div>'}
    <div class="form"><label>Source</label><select id="inMethod"><option value="official">Docker official repository (docker-ce) — recommended</option><option value="distro">Distribution package (docker.io)</option></select>
    <label>Service</label><label class="chk"><input type="checkbox" id="inEnable" checked> Start Docker now and at every boot</label>
    <label>Permissions</label><label class="chk"><input type="checkbox" id="inGroup"> Add <b>${esc(s.user||'me')}</b> to the docker group (run docker without sudo)</label></div>
    <div class="warnbox" style="font-size:12.5px">The docker group grants root-level privileges to its members. Leave it unchecked if you prefer <span class="mono">sudo docker</span>.</div>
    <div class="infobox" style="font-size:12.5px">The official method removes conflicting packages (docker.io, podman-docker, containerd, runc…), adds Docker's signing key to <span class="mono">/etc/apt/keyrings/docker.asc</span> and the repository to <span class="mono">/etc/apt/sources.list.d/docker.sources</span>, then installs docker-ce, docker-ce-cli, containerd.io, docker-buildx-plugin and docker-compose-plugin.</div>
    <div class="toolbar"><button class="btn pri" id="inGo"${d.family?'':' disabled'}>${icon('down')}${s.installed?'Reinstall':'Install Docker'}</button></div></div>
   <div class="card c6"><h3>${icon('shield')}Access</h3>
    <div class="kv"><div>User</div><div class="mono">${esc(s.user)}</div><div>docker group</div><div>${s.in_group?'<span class="badge g">member</span>':'<span class="badge">not a member</span>'} ${s.in_group&&!s.group_active?'<span class="badge a">re-login needed</span>':''}</div><div>Daemon access</div><div>${s.access==='ok'?'<span class="badge g">OK</span>':'<span class="badge r">'+esc(s.access||'n/a')+'</span>'}</div></div>
    <div class="toolbar" style="margin-top:12px">${s.in_group?'<button class="btn sm ghost" data-act="group_remove">Remove me from docker group</button>':'<button class="btn sm" data-act="group_add">Add me to docker group</button>'}<button class="btn sm ghost" data-act="relaunch"${s.in_group&&!s.group_active?'':' disabled'}>Relaunch with docker group</button></div>
    <h3 style="margin-top:20px">${icon('gear')}Daemon configuration <span class="r mono dim">/etc/docker/daemon.json</span></h3>
    <textarea id="djText" rows="9" class="mono" placeholder='{\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "10m", "max-file": "3"}\n}'>${esc(s.daemon_json||'')}</textarea>
    <div class="toolbar" style="margin-top:8px"><button class="btn sm" id="djSave"${s.installed?'':' disabled'}>Save</button><button class="btn sm pri" id="djSaveR"${s.installed?'':' disabled'}>Save &amp; restart Docker</button><button class="btn sm ghost" id="djLog">Insert log-rotation example</button></div>
    ${s.installed?`<h3 style="margin-top:20px;color:var(--red)">${icon('trash')}Uninstall</h3><div class="toolbar"><label class="chk"><input type="checkbox" id="unData"> Also delete all images, containers and volumes (/var/lib/docker)</label><label class="chk"><input type="checkbox" id="unRepo"> Remove Docker's apt repository</label><button class="btn sm danger" id="unGo">Uninstall Docker</button></div>`:''}</div></div>`;
  paintIcons(body);body.querySelectorAll('[data-act]').forEach(b=>b.onclick=()=>setupAct(b.dataset.act));
  $('#inGo').onclick=async()=>{const p={method:$('#inMethod').value,enable:$('#inEnable').checked,add_group:$('#inGroup').checked};if(!await confirmBox('Install Docker',`Install Docker from the <b>${p.method==='official'?'official Docker repository':'distribution packages'}</b>? This downloads packages from the internet and asks for your password once.`,'Install'))return;
    runCmdJob('Installing Docker',api('/api/docker/action',{kind:'setup',action:'install',params:p}),()=>loadDocker(true))};
  $('#djLog').onclick=()=>{let o={};try{o=JSON.parse($('#djText').value||'{}')}catch(e){}o['log-driver']='json-file';o['log-opts']={'max-size':'10m','max-file':'3'};$('#djText').value=JSON.stringify(o,null,2)};
  const save=async r=>{try{JSON.parse($('#djText').value||'{}')}catch(e){toast('Invalid JSON: '+esc(e.message),'err');return}if(r&&!await confirmBox('Restart Docker','Restarting the daemon stops running containers (those with a restart policy come back).','Save & restart',true))return;try{await api('/api/docker/action',{kind:'setup',action:'daemon_json',params:{text:$('#djText').value,restart:r}});toast('daemon.json saved'+(r?' — Docker restarted':''),'ok');loadDocker(true)}catch(e){terr(e)}};
  if($('#djSave')){$('#djSave').onclick=()=>save(false);$('#djSaveR').onclick=()=>save(true)}
  if($('#unGo'))$('#unGo').onclick=async()=>{const data=$('#unData').checked;const c=await confirmBox('Uninstall Docker','Remove the Docker packages'+(data?' <b>and delete all images, containers and volumes</b>':'')+'?','Uninstall',true,data?'DELETE DOCKER DATA':null);if(!c)return;
    runCmdJob('Uninstalling Docker',api('/api/docker/action',{kind:'setup',action:'uninstall',params:{purge_data:data,remove_repo:$('#unRepo').checked},confirm:data?'DELETE DOCKER DATA':''}),()=>loadDocker(true))};
}
SHOW.docker=()=>{loadDocker(true);clearInterval(DKR.timer);DKR.timer=setInterval(()=>{if(DKR.st&&DKR.st.access==='ok'&&DKR.tab==='containers'&&!$('#modalBg').classList.contains('on')){loadList('containers')}},5000)};
HIDE.docker=()=>clearInterval(DKR.timer);

/* ============================================================ data recovery (v1.2) */
let RC={targets:[],dev:null,search:null,report:null,undel:null,usel:new Set(),job:null,tab:'part',mounts:[]};
const FSNAME={vfat:'FAT',ntfs:'NTFS',exfat:'exFAT',ext2:'ext2',ext3:'ext3',ext4:'ext4',xfs:'XFS',btrfs:'Btrfs',crypto_LUKS:'LUKS',swap:'swap',LVM2_member:'LVM PV',iso9660:'ISO9660'};
const CARVE_TYPES=[['jpg','JPEG photos','image'],['png','PNG images','image'],['gif','GIF images','image'],['bmp','BMP images','image'],['pdf','PDF documents','list'],['zip','Office docs (docx/xlsx/pptx/odt) & ZIP','box'],['ole','Legacy Office (doc/xls/ppt/msg)','list'],['mp4','Video MP4 / MOV / HEIC','play'],['riff','AVI / WAV / WebP','play'],['mp3','MP3 audio','wave'],['ogg','Ogg audio','wave'],['sqlite','SQLite databases','repo'],['7z','7-Zip archives','box']];
function initRecovery(){
  G.rec=SegGauge($('#gRec'),{label:'PROGRESS',pal:'cyan',n:52});G.rec.set(0,'0<tspan class="g-unit">%</tspan>','READY');
  $('#rcTabs').onclick=e=>{const b=e.target.closest('button');if(!b)return;RC.tab=b.dataset.t;$$('#rcTabs button').forEach(x=>x.classList.toggle('on',x===b));$$('.rcp').forEach(p=>p.style.display=p.dataset.t===RC.tab?'':'none');if(RC.tab==='undo')loadUndo();if(RC.tab==='cli')renderCli()};
  $('#rcDev').onchange=()=>{$('#rcImgRow').style.display=$('#rcDev').value==='__img'?'':'none';selectDev()};$('#rcImgGo').onclick=selectDev;$('#rcReload').onclick=loadTargets;$('#rcCancel').onclick=()=>{if(RC.job)api('/api/job/cancel',{id:RC.job})};
  $('#rcQuick').onclick=()=>psearch('quick');$('#rcDeep').onclick=()=>psearch('deep');$('#rcWrite').onclick=writeTable;
  $('#rcFound').addEventListener('click',foundClick);$('#rcFound').addEventListener('change',e=>{if(e.target.matches('[data-ci]'))updWriteBtn()});
  $('#rbGo').onclick=analyze;$('#rbOut').addEventListener('click',repairClick);
  $('#ruGo').onclick=undelScan;$('#ruRecover').onclick=undelRecover;$('#ruFilter').oninput=renderUndel;$('#ruStatus').onchange=renderUndel;
  $('#ruTbl').addEventListener('change',e=>{const c=e.target.closest('input[data-id]');if(c){c.checked?RC.usel.add(+c.dataset.id):RC.usel.delete(+c.dataset.id);updUndelSel()}if(e.target.id==='ruAll'){filtUndel().forEach(f=>e.target.checked?RC.usel.add(f.id):RC.usel.delete(f.id));renderUndel()}});
  $('#rcTypes').innerHTML=CARVE_TYPES.map(t=>`<label class="ctype"><input type="checkbox" value="${t[0]}" checked>${icon(t[2])}<span>${t[1]}</span><b class="mono" data-cnt="${t[0]}"></b></label>`).join('');
  $('#rcCarve').onclick=carve;$('#rcTypesAll').onclick=()=>{const all=$$('#rcTypes input').every(i=>i.checked);$$('#rcTypes input').forEach(i=>i.checked=!all)};
  $('#rcUndo').addEventListener('click',undoClick);
  const out=lsGet('rc_out','~/Recovered');$('#ruOut').value=out;$('#rcOut').value=out;
}
let RC_LOADING=0;
async function loadTargets(){if(RC_LOADING)return;const sel=$('#rcDev'),info=$('#rcInfo'),t0=Date.now();RC_LOADING=1;
  const cur=sel.value;sel.disabled=true;if(!sel.options.length||cur==='')sel.innerHTML='<option value="">Loading devices…</option>';
  const paint=()=>{const s=((Date.now()-t0)/1000).toFixed(0);info.innerHTML=`<div class="rcload"><span class="spin"></span><div style="flex:1"><b>Scanning disks, partitions and images…</b> <span class="muted mono">${s}s</span><div class="opbar indet"><i style="width:100%"></i></div>${s>=15?'<span class="muted">Still waiting on the backend. If a password dialog is open (polkit), answer it; otherwise check that only one MySysPilot instance is running.</span>':'<span class="muted">Reading lsblk, partition tables and mount state.</span>'}</div></div>`};
  paint();const tm=setInterval(paint,1000);
  try{const r=await api('/api/rec/targets');RC.targets=r.targets;RC.tools=r;
    sel.innerHTML=r.targets.map(t=>`<option value="${esc(t.path)}">${esc(t.path)} · ${fmtSI(t.size)} ${esc(t.model||t.fstype||t.type)}${t.critical?' · SYSTEM':''}</option>`).join('')+'<option value="__img">Disk image file…</option>';
    if(cur&&[...sel.options].some(o=>o.value===cur))sel.value=cur;else{const nc=r.targets.find(t=>!t.critical);if(nc)sel.value=nc.path}
    clearInterval(tm);sel.disabled=false;RC_LOADING=0;selectDev();
    const nd=r.targets.filter(t=>t.type==='disk'||t.type==='loop').length;
    info.insertAdjacentHTML('beforeend',`<div class="muted rcok" style="margin-top:6px;font-size:12px">${icon("shield")} ${r.targets.length} device(s) found${nd&&nd!==r.targets.length?` (${nd} disks)`:''} · ${((Date.now()-t0)/1000).toFixed(1)}s · updated ${new Date().toLocaleTimeString()} · <a href="#" id="rcRescan">rescan</a></div>`);
    const rl=$("#rcRescan");if(rl)rl.onclick=e=>{e.preventDefault();loadTargets()};
  }catch(e){clearInterval(tm);RC_LOADING=0;sel.disabled=false;if(!sel.options.length||sel.value==='')sel.innerHTML='<option value="">— no devices loaded —</option><option value="__img">Disk image file…</option>';
    info.innerHTML=`<div class="rcload err">${icon('info')}<div style="flex:1"><b>Could not load the device list</b><div class="opbar error"><i style="width:100%"></i></div><span class="mono" style="font-size:12px">${esc(e.message||String(e))}</span></div><button class="btn" id="rcRetry">Retry</button></div>`;
    $('#rcRetry').onclick=loadTargets}}
function selectDev(){const v=$('#rcDev').value;let dev;
  if(v==='__img'){const p=$('#rcImg').value.trim();if(!p){$('#rcInfo').innerHTML='<span class="muted">Enter the path of a disk image (.img, .dd, .raw) to work on a copy instead of the drive.</span>';RC.dev=null;fillTargets();return}dev={path:p,type:'image',size:0,parts:[],image:true}}
  else dev=RC.targets.find(t=>t.path===v);RC.dev=dev;RC.search=null;$('#rcFound').innerHTML='<div class="empty">Run a Quick Search (seconds) or Deep Search (reads the whole device).</div>';$('#rcMap').innerHTML='';
  if(dev&&!dev.image)$('#rcInfo').innerHTML=`<b>${esc(dev.path)}</b> · ${fmtSI(dev.size)} · ${esc(dev.model||dev.type)} · table: ${esc((dev.pttype||'none').toUpperCase())} · ${dev.parts.length} partition(s)${dev.mounted.length?` · <span class="badge a">mounted: ${esc(dev.mounted.join(', '))}</span>`:''}${dev.critical?' <span class="badge r">SYSTEM DISK — read-only analysis only</span>':''}`;
  else if(dev)$('#rcInfo').innerHTML=`<b class="mono">${esc(dev.path)}</b> · image file`;
  fillTargets();updWriteBtn()}
function targetList(){const d=RC.dev;if(!d)return [];const L=[];
  L.push({path:d.path,offset:0,size:d.size,label:`Whole ${d.image?'image':'device'} ${d.path}${d.fstype?' ('+(FSNAME[d.fstype]||d.fstype)+')':''}`});
  (d.parts||[]).forEach(p=>L.push({path:p.path,offset:0,size:p.size,label:`${p.path} · ${fmtSI(p.size)} ${FSNAME[p.fstype]||p.fstype||''} ${p.label||''}${p.mounted.length?' · mounted':''}`}));
  if(RC.search)RC.search.candidates.filter(c=>!c.backup).forEach(c=>L.push({path:d.path,offset:c.start,size:c.size,label:`Found: ${FSNAME[c.fs]||c.fs} at sector ${(c.start/512).toLocaleString()} · ${fmtSI(c.size)} ${c.label||''} [${c.status}]`}));
  return L}
function fillTargets(){const L=targetList();const html=L.map((t,i)=>`<option value="${i}">${esc(t.label)}</option>`).join('')||'<option>Select a device above</option>';['#rbTarget','#ruTarget','#rcTarget'].forEach(s=>{const keep=$(s).value;$(s).innerHTML=html;if(keep&&L[+keep])$(s).value=keep;else if(L.length>1&&s!=='#rcTarget')$(s).value=1})}
const tgt=sel=>targetList()[+$(sel).value];
async function recRun(title,startP,onTick){
  const rop=opStart({title:'Recovery: '+title,dev:RC.dev&&RC.dev.path,msg:'Waiting for authorization…'});
  RC.job=null;$('#rcCancel').disabled=false;$('#rcTitle').textContent=title;G.rec.set(0,'0<tspan class="g-unit">%</tspan>','STARTING');$('#rcBar').style.width='0';$('#rcS1').textContent='Waiting for authorization…';$('#rcS2').textContent='';$('#rcS3').textContent='';
  try{const r=await startP;RC.job=r.job;rop.job=r.job;const j=await waitJob(r.job,j=>{const L=j.live||{};const p=j.progress*100;opUpd(rop,{progress:j.progress,indet:!(j.progress>0),msg:j.message,rate:L.rate||null,eta:L.eta,series:L.rate?(rop.series||[]).concat([+(L.rate/1e6).toFixed(1)]).slice(-120):rop.series});G.rec.set(p,`${p<10?p.toFixed(1):Math.round(p)}<tspan class="g-unit">%</tspan>`,title.toUpperCase().slice(0,22));$('#rcBar').style.width=p+'%';$('#rcS1').textContent=j.message;
      $('#rcS2').textContent=L.rate?`${fmtSI(L.rate)}/s`:'';$('#rcS3').textContent=L.eta!=null?`ETA ${fmtEta(L.eta)}`:'';if(onTick)onTick(j)});
    RC.job=null;$('#rcCancel').disabled=true;if(j.state==='cancelled'){opEnd(rop,null,'Cancelled');G.rec.set(j.progress*100,'—','CANCELLED');return null}opEnd(rop,true,j.message);G.rec.set(100,'100<tspan class="g-unit">%</tspan>','COMPLETE');$('#rcBar').style.width='100%';$('#rcS1').textContent=j.message;return j.result}
  catch(e){opEnd(rop,false,e.message);RC.job=null;$('#rcCancel').disabled=true;G.rec.set(0,'!','FAILED');$('#rcS1').textContent=e.message;terr(e);return null}}
function fsStateHtml(l){const h=l.fshealth;if(!h)return '<span class="badge">Unknown</span>';const when=h.checked?` <span class="dim">checked ${new Date(h.checked*1000).toLocaleString()}</span>`:'';
  if(h.state==='repair')return `<span class="badge r">Needs repair</span> <a style="color:var(--cyan);cursor:pointer" onclick="fsMaint(dkByPath('${esc(l.path)}'),'repair')">Repair now…</a><div class="muted" style="font-size:12px;margin-top:4px">${h.issues.map(esc).join('<br>')}</div>`;
  if(h.state==='clean')return `<span class="badge g">Clean</span>${when}`;return `<span class="badge">Not checked by MySysPilot</span> <span class="dim" style="font-size:12px">${DK.list.some(d=>d.klog)?'no errors in the kernel log':'kernel log not readable'}</span>`}
function fmtEta(s){s=Math.round(s);if(s<60)return s+'s';if(s<3600)return `${Math.floor(s/60)}m ${pad(s%60)}s`;return `${Math.floor(s/3600)}h ${pad(Math.floor(s%3600/60))}m`}
/* ---- partition recovery */
async function psearch(mode){if(!RC.dev)return toast('Select a device first','err');
  $('#rcFound').innerHTML='<div class="empty"><span class="spin"></span> Searching…</div>';
  const res=await recRun(mode==='quick'?'Quick search':'Deep search',api('/api/rec/search',{path:RC.dev.path,mode}),j=>{const L=j.live||{};if(L.found&&L.found.length)$('#rcFound').innerHTML=`<div class="muted" style="margin-bottom:8px">Found so far:</div>`+L.found.map(f=>`<span class="badge b" style="margin:2px">${esc(FSNAME[f.fs]||f.fs)} @ ${(f.start/512).toLocaleString()} · ${fmtSI(f.size||0)} ${esc(f.label||'')}</span>`).join('')});
  if(!res){if(!RC.search)$('#rcFound').innerHTML='<div class="empty">Search did not complete.</div>';return}
  RC.search=res;if(RC.dev.image)RC.dev.size=res.size;renderFound();fillTargets()}
function renderFound(){const R=RC.search,d=RC.dev;if(!R)return;const sz=R.size||d.size||1;
  const seg=(s,n,cls,txt)=>`<div class="rseg ${cls}" style="left:${100*s/sz}%;width:${Math.max(.6,100*n/sz)}%" title="${esc(txt)}">${esc(txt)}</div>`;
  $('#rcMap').innerHTML=`<div class="rrow"><span>Current table</span><div class="rtrack">${R.table.map(p=>seg(p.start,p.size,'t',p.name.split('/').pop())).join('')||'<em class="dim">no partitions</em>'}</div></div>
    <div class="rrow"><span>Found</span><div class="rtrack">${R.candidates.filter(c=>!c.backup).map(c=>seg(c.start,c.size||0,c.status,`${FSNAME[c.fs]||c.fs} ${c.label||''}`)).join('')}</div></div>`;
  const showBk=$('#rcShowBk')&&$('#rcShowBk').checked;const rows=R.candidates.map((c,i)=>Object.assign(c,{_i:i})).filter(c=>showBk||!c.backup);const prim=rows.filter(c=>!c.backup);const nbk=R.candidates.length-prim.length;
  $('#rcFound').innerHTML=prim.length?`<div class="tbl-wrap" style="max-height:420px"><table class="t"><thead><tr><th style="width:30px"></th><th>Status</th><th>Filesystem</th><th>Label</th><th>Start sector</th><th>End sector</th><th>Size</th><th>Found by</th><th style="text-align:right">Actions</th></tr></thead><tbody>${rows.map(c=>{const i=c._i;const st={'in-table':['g','In table'],lost:['r','LOST'],overlap:['a','Overlaps'],backup:['','Backup copy']}[c.status]||['',c.status];const ss=R.sector||512;
    return `<tr class="${c.backup?'dimrow':''}" data-i="${i}"><td>${c.backup?'':`<input type="checkbox" data-ci="${i}"${c.status==='lost'?' checked':''}>`}</td><td><span class="badge ${st[0]}">${st[1]}</span>${c.part?` <span class="mono dim">${esc(c.part.split('/').pop())}</span>`:''}</td><td><b>${esc(FSNAME[c.fs]||c.fs)}</b>${c.repair?' <span class="badge a" title="boot sector needs repair">boot damaged</span>':''}</td><td>${esc(c.label||'—')}</td><td class="num">${(c.start/ss).toLocaleString()}</td><td class="num">${c.size?((c.start+c.size)/ss-1).toLocaleString():'?'}</td><td class="num">${c.size?fmtSI(c.size):'?'}${c.size_guess?' <span class="badge a" title="size not stored in the filesystem header; extended to the next partition">est.</span>':''}</td><td class="dim" style="font-size:12px">${esc(c.backup||c.how)}${c.overlaps?' · overlaps '+esc(c.overlaps):''}</td>
      <td style="text-align:right;white-space:nowrap">${c.backup?'':`<button class="btn sm ghost" data-fa="browse" title="Mount read-only and open in file manager">${icon('folder')}Browse</button> <button class="btn sm ghost" data-fa="analyze">${icon('shield')}Repair</button> <button class="btn sm ghost" data-fa="undel">${icon('copy')}Undelete</button> <button class="btn sm ghost" data-fa="carve">${icon('search')}Carve</button>`}</td></tr>`}).join('')}</tbody></table></div>`:'<div class="empty">No filesystems were found. Try Deep Search, or File Carving to recover individual files.</div>';
  if(nbk)$('#rcFound').insertAdjacentHTML('beforeend',`<label class="chk" style="margin-top:8px"><input type="checkbox" id="rcShowBk"${showBk?' checked':''}> Show ${nbk} backup boot sector / superblock cop${nbk===1?'y':'ies'}</label>`);if($('#rcShowBk'))$('#rcShowBk').onchange=renderFound;
  renderMounts();updWriteBtn()}
function updWriteBtn(){const n=$$('[data-ci]:checked').length;const d=RC.dev;$('#rcWrite').disabled=!n||!d||d.critical||(d.type&&d.type!=='disk'&&!d.image);$('#rcWrite').innerHTML=icon('save')+`Write ${n||''} partition${n===1?'':'s'} to table…`;
  $('#rcWriteNote').textContent=!d?'':d.critical?'This is the running system disk — writing is disabled.':(d.type&&d.type!=='disk'&&!d.image)?'Select a whole disk (not a partition) to write a partition table.':''}
async function writeTable(){const R=RC.search;const items=$$('[data-ci]:checked').map(c=>R.candidates[+c.dataset.ci]).map(c=>({start:c.start,size:c.size,fs:c.fs}));const mode=$('#rcWMode').value,label=$('#rcWLabel').value,p=RC.dev.path;
  const list=items.map(i=>`<li class="mono">${esc(FSNAME[i.fs]||i.fs)} · sector ${(i.start/512).toLocaleString()} · ${fmtSI(i.size)}</li>`).join('');
  if(!await confirmBox('Write partition table',`${mode==='new'?`<b>Replace</b> the partition table of <b class="mono">${esc(p)}</b> with a new ${label.toUpperCase()} table containing:`:`<b>Add</b> these entries to the existing table of <b class="mono">${esc(p)}</b>:`}<ul>${list}</ul><div class="infobox">Only the partition table is written — filesystem data is not touched. The current table is saved first (listed under Disk Utility → Backups).</div>`,'Write partition table',true))return;
  const r=await recRun('Writing table',api('/api/rec/write_table',{path:p,mode,label,items,confirm:p}));if(r){openModal('Partition table written',`<div class="infobox">Done. Previous table saved to <span class="mono">${esc(r.backup)}</span>.</div><pre class="out">${esc(r.output||'')}</pre>`);setTimeout(()=>{loadTargets();psearch('quick')},800)}}
async function foundClick(e){const b=e.target.closest('button[data-fa]');if(!b)return;const c=RC.search.candidates[+b.closest('tr').dataset.i];const a=b.dataset.fa;const tl=targetList();const idx=tl.findIndex(t=>t.offset===c.start&&t.path===RC.dev.path);
  if(a==='browse'){try{const r=await api('/api/rec/mount',{path:RC.dev.path,offset:c.start,size:c.size});toast(`Mounted read-only at ${esc(r.mountpoint)}`,'ok');RC.mounts.push(r);renderMounts();api('/api/open',{path:r.mountpoint}).catch(()=>{})}catch(err){terr(err)}return}
  const tab={analyze:'boot',undel:'undel',carve:'carve'}[a];$(`#rcTabs button[data-t="${tab}"]`).click();const sel={boot:'#rbTarget',undel:'#ruTarget',carve:'#rcTarget'}[tab];if(idx>=0)$(sel).value=idx;if(a==='analyze')analyze()}
function renderMounts(){const h=$('#rcMounts');if(!h)return;h.innerHTML=RC.mounts.length?`<div class="agrp" style="margin-top:12px"><div class="agt">Recovered filesystems mounted read-only</div>${RC.mounts.map((m,i)=>`<div class="row" style="margin:4px 0"><span class="mono">${esc(m.mountpoint)}</span><span class="dim">(${esc(m.loop)})</span><span class="spacer"></span><button class="btn sm ghost" data-mo="${i}">${icon('folder')}Open</button><button class="btn sm danger" data-mu="${i}">Unmount</button></div>`).join('')}<div class="dim" style="font-size:12px">Copy the files you need to another drive, then unmount.</div></div>`:'';
  h.querySelectorAll('[data-mo]').forEach(b=>b.onclick=()=>api('/api/open',{path:RC.mounts[+b.dataset.mo].mountpoint}).catch(terr));
  h.querySelectorAll('[data-mu]').forEach(b=>b.onclick=async()=>{const m=RC.mounts[+b.dataset.mu];try{await api('/api/rec/unmount',{loop:m.loop});RC.mounts.splice(+b.dataset.mu,1);renderMounts()}catch(err){terr(err)}})}
/* ---- boot & superblock repair */
async function analyze(){const t=tgt('#rbTarget');if(!t)return;$('#rbOut').innerHTML='<div class="empty"><span class="spin"></span> Analyzing…</div>';
  const r=await recRun('Analyzing',api('/api/rec/analyze',{path:t.path,offset:t.offset,size:t.size}));if(!r){$('#rbOut').innerHTML='';return}RC.report=Object.assign(r,{target:t});renderReport()}
function renderReport(){const r=RC.report,t=r.target;const ic={ok:['g','shield','OK'],warn:['a','info','Attention'],bad:['r','x','Damaged']};
  $('#rbOut').innerHTML=`<div class="row" style="margin-bottom:12px"><span class="badge b" style="font-size:13px">${esc(FSNAME[r.fs]||r.fs||'Unknown')}</span><span class="mono dim">${esc(t.path)}${t.offset?` @ sector ${(t.offset/512).toLocaleString()}`:''}</span></div>
   <div class="checks">${r.checks.map(c=>`<div class="chk-row ${ic[c.status][0]}">${icon(ic[c.status][1])}<div><b>${esc(c.name)}</b> <span class="badge ${ic[c.status][0]}">${ic[c.status][2]}</span><div class="muted" style="font-size:12.5px">${esc(c.detail)}</div></div></div>`).join('')}</div>
   ${r.actions.length?`<div class="agrp" style="margin-top:14px"><div class="agt">Repairs available</div><div class="toolbar">${r.actions.map((a,i)=>`<button class="btn sm ${a.danger?'danger':''} ${a.recommended?'pri':''}" data-ra="${i}">${icon('gear')}${esc(a.label)}${a.recommended?' (recommended)':''}</button>`).join('')}</div><div class="dim" style="font-size:12px;margin-top:6px">Sector writes are verified after writing and the overwritten bytes are saved to the Undo log.</div></div>`:'<div class="infobox" style="margin-top:12px">No repair needed for the structures checked.</div>'}
   ${r.info?`<details style="margin-top:12px"><summary class="muted">Technical details</summary><pre class="out">${esc(JSON.stringify(r.info,null,2))}</pre></details>`:''}`}
async function repairClick(e){const b=e.target.closest('button[data-ra]');if(!b)return;const r=RC.report,a=r.actions[+b.dataset.ra],t=r.target;
  const ro=['xfs_check','btrfs_check'].includes(a.action);
  if(!ro&&!await confirmBox('Apply repair',`<b>${esc(a.label)}</b> on <span class="mono">${esc(t.path)}</span>${t.offset?` (filesystem at sector ${(t.offset/512).toLocaleString()})`:''}.<div class="infobox">The volume must not be mounted. Changed sectors are backed up to the Undo log first (tool-based repairs such as e2fsck cannot be undone).</div>`,'Repair',true,t.path))return;
  const res=await recRun('Repairing',api('/api/rec/repair',{path:t.path,offset:t.offset,action:a.action,params:{size:t.size,block:a.block,bs:a.bs},confirm:t.path}));
  if(res){if(res.output)openModal('Repair output',`<pre class="out" style="max-height:60vh">${esc(res.output)}</pre>`);else toast(`Repair written (${res.written} region(s)) and verified`,'ok');analyze()}}
/* ---- undelete */
async function undelScan(){const t=tgt('#ruTarget');if(!t)return;$('#ruTbl').innerHTML='<div class="empty"><span class="spin"></span> Scanning…</div>';RC.usel.clear();
  const r=await recRun('Undelete scan',api('/api/rec/undelete/scan',{path:t.path,offset:t.offset}));if(!r){$('#ruTbl').innerHTML='';return}RC.undel=r;renderUndel()}
function filtUndel(){const R=RC.undel;if(!R)return [];const q=$('#ruFilter').value.toLowerCase(),st=$('#ruStatus').value;return R.files.filter(f=>(!q||(f.dir+'/'+f.name).toLowerCase().includes(q))&&(st==='all'||(st==='good'?f.status==='good':st==='partial'?f.status.startsWith('partial'):f.status!=='good'&&!f.status.startsWith('partial'))))}
function renderUndel(){const R=RC.undel;if(!R)return;const rows=filtUndel();const good=R.files.filter(f=>f.status==='good').length;
  $('#ruSum').innerHTML=`<span class="badge b">${esc(FSNAME[R.fs]||R.fs)}</span> ${R.files.length.toLocaleString()} deleted files · <span class="badge g">${good.toLocaleString()} recoverable</span> · ${fmtB(R.files.reduce((a,f)=>a+f.size,0))}`;
  $('#ruTbl').innerHTML=rows.length?`<div class="tbl-wrap" style="max-height:480px"><table class="t"><thead><tr><th style="width:30px"><input type="checkbox" id="ruAll"></th><th>Name</th><th>Folder</th><th>Size</th><th>Modified</th><th>State</th></tr></thead><tbody>${rows.slice(0,5000).map(f=>`<tr><td><input type="checkbox" data-id="${f.id}"${RC.usel.has(f.id)?' checked':''}></td><td><b>${esc(f.name)}</b></td><td class="mono dim trunc" title="${esc(f.dir)}">${esc(f.dir)}</td><td class="num">${fmtB(f.size)}</td><td class="num">${f.mtime?new Date(f.mtime*1000).toLocaleString():'—'}</td><td><span class="badge ${f.status==='good'?'g':f.status.startsWith('partial')?'a':'r'}">${esc(f.status)}</span></td></tr>`).join('')}</tbody></table></div>${rows.length>5000?`<div class="empty">${rows.length-5000} more — narrow with the filter.</div>`:''}`:'<div class="empty">No deleted files match.</div>';updUndelSel()}
function updUndelSel(){$('#ruRecover').disabled=!RC.usel.size;$('#ruRecover').innerHTML=icon('save')+`Recover ${RC.usel.size||''} selected`}
async function undelRecover(){const out=$('#ruOut').value.trim();lsSet('rc_out',out);const ids=[...RC.usel];
  const r=await recRun('Recovering files',api('/api/rec/undelete/recover',{key:RC.undel.key,ids,outdir:out}));if(r)openModal('Files recovered',`<div class="kv"><div>Recovered</div><div>${r.recovered.toLocaleString()} files · ${fmtB(r.bytes)}</div><div>Folder</div><div class="mono">${esc(r.folder)}</div></div>${r.failed.length?`<div class="warnbox">${r.failed.map(esc).join('<br>')}</div>`:''}<div class="toolbar" style="margin-top:12px"><button class="btn sm pri" id="ruOpen">${icon('folder')}Open folder</button></div>`),$('#ruOpen')&&($('#ruOpen').onclick=()=>api('/api/open',{path:r.folder}))}
/* ---- carving */
async function carve(){const t=tgt('#rcTarget');if(!t)return;const types=$$('#rcTypes input:checked').map(i=>i.value);if(!types.length)return toast('Select at least one file type','err');const out=$('#rcOut').value.trim();lsSet('rc_out',out);
  $$('[data-cnt]').forEach(e=>e.textContent='');$('#rcRecent').innerHTML='';
  const p=t.path===RC.dev.path?{path:t.path,start:t.offset,end:t.offset+t.size}:{path:t.path,start:0,end:0};
  const r=await recRun('File carving',api('/api/rec/carve',Object.assign(p,{types,outdir:out,align:+$('#rcAlign').value})),j=>{const L=j.live||{};const c=L.counts||{};
      const grp={jpg:['jpg'],png:['png'],gif:['gif'],bmp:['bmp'],pdf:['pdf'],zip:['zip','docx','xlsx','pptx','odt','ods','odp','epub','apk','jar'],ole:['doc','xls','ppt','msg','ole'],mp4:['mp4','mov','m4a','heic','3gp'],riff:['wav','avi','webp'],mp3:['mp3'],ogg:['ogg'],sqlite:['sqlite'],'7z':['7z']};
      Object.entries(grp).forEach(([k,exts])=>{const n=exts.reduce((a,e)=>a+(c[e]||0),0);const el=$(`[data-cnt="${k}"]`);if(el)el.textContent=n?n.toLocaleString():''});
      $('#rcFiles').textContent=`${(L.files||0).toLocaleString()} files · ${fmtB(L.bytes||0)}`;
      $('#rcRecent').innerHTML=(L.recent||[]).slice(-12).reverse().map(f=>`<div class="item"><span class="p">${esc(f.name)}</span><span class="s">${fmtB(f.size)}</span><span class="s dim">@${(f.offset/512).toLocaleString()}</span></div>`).join('')});
  if(r)openModal('Carving complete',`<div class="kv"><div>Recovered</div><div>${r.files.toLocaleString()} files · ${fmtB(r.bytes)}</div><div>By type</div><div>${Object.entries(r.counts).map(([k,v])=>`<span class="badge b" style="margin:2px">${esc(k)} ${v}</span>`).join('')||'—'}</div><div>Folder</div><div class="mono">${esc(r.folder)}</div></div><div class="infobox">Carved files have generic names (f&lt;sector&gt;.ext). Original names and folders are not stored in file contents.</div><div class="toolbar"><button class="btn sm pri" id="rcOpen">${icon('folder')}Open folder</button></div>`),$('#rcOpen')&&($('#rcOpen').onclick=()=>api('/api/open',{path:r.folder}))}
/* ---- undo & external */
async function loadUndo(){try{const r=await api('/api/rec/undo');RC.undo=r.undo;$('#rcUndo').innerHTML=r.undo.length?`<div class="tbl-wrap" style="max-height:420px"><table class="t"><thead><tr><th>Date</th><th>Device</th><th>Offset</th><th>Bytes</th><th>Change</th><th></th></tr></thead><tbody>${r.undo.map((u,i)=>`<tr><td>${fmtDate(u.ts)}</td><td class="mono">${esc(u.path)}</td><td class="num">${u.offset.toLocaleString()}</td><td class="num">${u.length.toLocaleString()}</td><td>${esc(u.desc)}</td><td style="text-align:right"><button class="btn sm danger" data-u="${i}"${u.exists?'':' disabled'}>Revert</button></td></tr>`).join('')}</tbody></table></div>`:'<div class="empty">No sector writes yet.</div>'}catch(e){terr(e)}}
async function undoClick(e){const b=e.target.closest('[data-u]');if(!b)return;const u=RC.undo[+b.dataset.u];if(!await confirmBox('Revert change',`Write back the ${u.length.toLocaleString()} bytes that were at offset ${u.offset.toLocaleString()} on <span class="mono">${esc(u.path)}</span> before “${esc(u.desc)}”.`,'Revert',true,u.path))return;
  const r=await recRun('Reverting',api('/api/rec/undo/revert',{id:u.id,confirm:u.path}));if(r){toast('Reverted','ok');loadUndo()}}
function renderCli(){const T=RC.tools||{};$('#rcCli').innerHTML=`<p class="muted">MySysPilot implements the TestDisk and PhotoRec functions natively with progress bars. You can also open the original command-line tools in a terminal for anything not covered here (exotic filesystems, HFS+/APFS, Linux RAID).</p>
  <div class="toolbar"><button class="btn" data-x="testdisk"${T.testdisk?'':' disabled'}>${icon('term')}Open TestDisk on ${esc(RC.dev?RC.dev.path:'device')}</button><button class="btn" data-x="photorec"${T.photorec?'':' disabled'}>${icon('term')}Open PhotoRec on ${esc(RC.dev?RC.dev.path:'device')}</button></div>
  ${T.testdisk?'':'<div class="infobox">Install them with <span class="mono">sudo apt install testdisk</span> (the package contains both tools).</div>'}`;
  $$('#rcCli [data-x]').forEach(b=>b.onclick=async()=>{try{const r=await api('/api/rec/external',{tool:b.dataset.x,path:RC.dev.path});toast(esc(r.message),'ok')}catch(e){terr(e)}})}
SHOW.recovery=()=>{if(!RC.targets.length)loadTargets()};

/* ============================================================ settings / about */
function initSettings(){
  $('#stTheme').onclick=e=>{const b=e.target.closest('button');if(!b)return;SET.theme=b.dataset.v;lsSet('theme',SET.theme);applyTheme()};
  $('#stRefresh').value=SET.refresh;$('#stRefresh').onchange=e=>{SET.refresh=+e.target.value;lsSet('refresh',SET.refresh)};
  $('#stUnits').value=SET.units;$('#stUnits').onchange=e=>{SET.units=e.target.value;lsSet('units',SET.units)};
  $('#stGlow').value=SET.glow;$('#stGlow').oninput=e=>{SET.glow=+e.target.value;lsSet('glow',SET.glow);applyTheme()};
}
async function loadMeta(){try{const m=await api('/api/meta');const T=m.tools;const desc={'pkexec':'privilege prompts','systemctl':'services','journalctl':'journal cleanup','apt-get':'uninstall / autoremove','dpkg-query':'package list','snap':'Snap tab','flatpak':'Flatpak tab','smartctl':'detailed SMART','udisksctl':'attach images','gio':'move to Trash','xdg-open':'open folders','lsblk':'disk list','python3-gi':'Disk Utility actions'};
  $('#stTools').innerHTML=Object.entries(T).map(([k,v])=>`<div><span class="badge ${v?'g':'a'}">${v?'✓':'missing'}</span> <span class="mono">${esc(k)}</span></div><div class="muted">${esc(desc[k]||'')}</div>`).join('');
  $('#abVer').textContent='v'+m.version;$('#abBuild').innerHTML=`<div>Version</div><div class="mono">${esc(m.version)} (build ${esc(m.build)})</div><div>Server</div><div class="mono">127.0.0.1:${m.port} · PID ${m.pid}</div><div>Python</div><div class="mono">${esc(m.python)}</div><div>Data folder</div><div class="mono">${esc(m.data_dir)}</div><div>Running as</div><div>${m.euid===0?'<span class="badge r">root (not recommended)</span>':'regular user · elevation via polkit'}</div>`;
  if(SYS)$('#abKv').innerHTML=`<div>Host</div><div>${esc(SYS.hostname)}</div><div>OS</div><div>${esc(SYS.os)}</div><div>Kernel</div><div class="mono">${esc(SYS.kernel)} (${esc(SYS.arch)})</div><div>CPU</div><div>${esc(SYS.cpu_model)} · ${SYS.cpu_count} threads</div><div>Memory</div><div>${fmtB(SYS.mem_total)}</div>${SYS.gpu?`<div>Graphics</div><div>${esc(SYS.gpu)}</div>`:''}<div>Desktop</div><div>${esc(SYS.desktop||'—')} ${esc(SYS.session||'')}</div><div>Booted</div><div>${SYS.boot_time?fmtDate(SYS.boot_time):'—'}</div>`;
  return m}catch(e){terr(e)}}
$('#abQuit').onclick=async()=>{if(!await confirmBox('Quit MySysPilot','Stop the local server? Close this window afterwards; relaunch from the app menu or terminal.','Quit',true))return;try{await api('/api/quit',{})}catch(e){}document.body.innerHTML='<div class="empty" style="padding-top:30vh;font-size:16px">MySysPilot has stopped. You can close this window.</div>'};
SHOW.about=loadMeta;SHOW.settings=loadMeta;

/* ============================================================ boot */
INIT.processes=initProcesses;INIT.services=initServices;INIT.startup=initStartup;INIT.cleaner=initCleaner;INIT.packages=initPackages;INIT.repos=initRepos;INIT.disks=initDisks;INIT.dupes=initDupes;INIT.docker=initDocker;INIT.recovery=initRecovery;INIT.speed=initSpeed;INIT.settings=initSettings;INIT.resources=initResources;
SHOW.dashboard=()=>{if(LAST)renderDash(LAST)};SHOW.resources=()=>{if(LAST){renderSensors(LAST)};api('/api/history').then(h=>{HIST=h.points;renderResources()}).catch(()=>{})};
paintIcons();applyTheme();
if(!TOKEN){document.querySelector('.content').innerHTML='<div class="card warnbox" style="max-width:640px;margin:10vh auto">No session token. Launch MySysPilot from its launcher or terminal — it opens this page with a one-time token.</div>'}
else{loadSys();poll();setTimeout(checkInstances,1500);go(PAGES.some(p=>p.id===lsGet('page','dashboard'))?lsGet('page','dashboard'):'dashboard')}
</script>
</body>
</html>
'''

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
