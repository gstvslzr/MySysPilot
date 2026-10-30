#!/usr/bin/python3
"""Embed ui.html into backend.py -> single-file MySysPilot program.
usage: build.py [OUTPUT]   (default: ../MySysPilot_v<version>.py next to the source dir)"""
import os
import re
import sys
here = os.path.dirname(os.path.abspath(__file__))
b = open(os.path.join(here, "backend.py"), encoding="utf-8").read()
u = open(os.path.join(here, "ui.html"), encoding="utf-8").read()
assert "'''" not in u and not u.endswith("\\")
sys.path.insert(0, here)
import dkx_data  # noqa: E402  (Docker examples/templates data, generated into the page)
assert u.count("/*DKX-DATA*/") == 1
u = u.replace("/*DKX-DATA*/", dkx_data.js(), 1)
out = b.replace('UI_HTML = "__UI_HTML_PLACEHOLDER__"', "UI_HTML = r'''" + u + "'''", 1)
assert out != b, "placeholder not found"
ver = re.search(r'^APP_VERSION = "([^"]+)"', b, re.M).group(1)
dst = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(here), f"MySysPilot_v{ver}.py")
os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
open(dst, "w", encoding="utf-8").write(out)
print("built", dst, len(out))
