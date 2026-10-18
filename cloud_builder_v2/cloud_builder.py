"""
APK Cloud Builder  -  builds your Kivy project into an APK on GitHub's servers, and when the build
fails it reads the error, fixes the project/workflow automatically and tries again.

Why: from Iran, Google's Android download servers are blocked, so building locally needs a VPN.
GitHub's build machines are outside Iran, so they download everything without problems.

Run:  double-click "Run Cloud Builder.bat"   (or: python cloud_builder.py)

v2 - fixes the "app opens for a second and closes" problem:
  * project doctor: reads your imports and adds the missing libraries / INTERNET permission / data files
  * stable build profile (python-for-android v2024.01.21, API 33, NDK 25b, arm64)
  * crash guard: if the app still fails on the phone it shows the error on screen instead of closing
  * the finished APK is checked for missing modules (and rebuilt automatically if some are missing)
"""
import ast
import base64
import hashlib
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timezone

API = "https://api.github.com"
WORKFLOW_FILE = "build.yml"
SKIP_DIRS = {".git", ".buildozer", "bin", "__pycache__", ".github", "venv", ".venv", "node_modules"}

# Every build step runs through run_logged: on failure it puts the end of the log into a GitHub
# "annotation", which we can read through the API (no blocked download servers involved).
WORKFLOW = r"""name: Build APK
on:
  workflow_dispatch:
permissions:
  contents: write
jobs:
  build:
    runs-on: ubuntu-22.04
    timeout-minutes: 120
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"
      - uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: "17"
      - name: Prepare logging helper
        run: |
          cat > /tmp/annot.sh <<'EOS'
          run_logged() {
            set +e
            bash -c "$1" 2>&1 | tee /tmp/step.log
            local rc=${PIPESTATUS[0]}
            if [ "$rc" -ne 0 ]; then
              local t
              t=$(tail -c 24000 /tmp/step.log | sed 's/%/%25/g' | awk '{printf "%s%%0A", $0}')
              echo "::error title=BUILD_LOG_TAIL::${t}"
            fi
            return $rc
          }
          EOS
      - name: Install system packages
        run: |
          source /tmp/annot.sh
          run_logged 'sudo apt-get update && sudo apt-get install -y git zip unzip autoconf libtool pkg-config zlib1g-dev libncurses5-dev libncursesw5-dev libtinfo5 cmake libffi-dev libssl-dev build-essential ccache'
      - name: Install Buildozer (latest)
        run: |
          source /tmp/annot.sh
          run_logged 'python -m pip install --upgrade pip && pip install "git+https://github.com/kivy/buildozer.git" "cython==0.29.36" setuptools virtualenv'
      - name: Build APK
        run: |
          source /tmp/annot.sh
          grep -q accept_sdk_license buildozer.spec || sed -i '/^\[app\]/a android.accept_sdk_license = True' buildozer.spec
          run_logged 'yes | buildozer -v android debug'
      - name: Check APK contents
        continue-on-error: true
        run: |
          cat > /tmp/verify.py <<'EOS'
          import glob, io, os, tarfile, zipfile
          try:
              need = []
              if os.path.exists(".cloud_builder_imports.txt"):
                  need = [l.strip() for l in open(".cloud_builder_imports.txt") if l.strip()]
              apk = sorted(glob.glob("bin/*.apk"))[-1]
              z = zipfile.ZipFile(apk)
              names = []
              for n in z.namelist():
                  if n.startswith("assets/") and "private" in n:
                      try:
                          t = tarfile.open(fileobj=io.BytesIO(z.read(n)), mode="r:*")
                          names += t.getnames()
                      except Exception as e:
                          print("could not read", n, e)
              print("APK:", apk, "| files in python bundle:", len(names))
              if len(names) < 100:
                  print("python bundle not readable - module check skipped")
              else:
                  low = ["/" + x.lower() for x in names]
                  missing = []
                  for m in need:
                      key = "/" + m.lower()
                      if not any((key + "/") in s or (key + ".") in s for s in low):
                          missing.append(m)
                  if missing:
                      print("::warning title=APK_CHECK::APK_MISSING_MODULES: " + ",".join(missing))
                  else:
                      print("module check OK:", ", ".join(need) or "(nothing to check)")
          except Exception as e:
              print("check skipped:", e)
          EOS
          python /tmp/verify.py
      - name: Save APK as artifact
        uses: actions/upload-artifact@v4
        with:
          name: apk
          path: bin/*.apk
      - name: Publish APK as release
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh release create "build-${{ github.run_number }}" bin/*.apk --title "APK build ${{ github.run_number }}" --notes "Built by APK Cloud Builder"
"""


class GHError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"GitHub {code}: {msg}")
        self.code = code


class Stopped(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def parse_time(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def collect_files(proj):
    files = {}
    for root, dirs, names in os.walk(proj):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            full = os.path.join(root, n)
            if os.path.getsize(full) > 40 * 1024 * 1024:
                continue
            files[os.path.relpath(full, proj).replace(os.sep, "/")] = full
    return files


# ===================================================================== auto-fix engine
def spec_set(spec, key, value):
    """Set `key = value` in buildozer.spec (edits the active line, else a commented one, else adds it)."""
    line = "%s = %s" % (key, value)
    k = re.escape(key)
    for pat in (r"(?m)^%s\s*=.*$" % k, r"(?m)^[#;]\s*%s\s*=.*$" % k):
        if re.search(pat, spec):
            return re.sub(pat, lambda m: line, spec, count=1)
    return re.sub(r"(?m)^\[app\]\s*$", lambda m: "[app]\n" + line, spec, count=1)


def spec_get(spec, key):
    m = re.search(r"(?m)^%s\s*=\s*(.*?)\s*$" % re.escape(key), spec)
    return m.group(1) if m else None


ENV_KEYS = ["android.api", "android.minapi", "android.ndk", "android.ndk_api",
            "android.build_tools_version", "p4a.branch", "android.accept_sdk_license"]


def merge_good_settings(spec, good_spec):
    """Copy the build-environment keys (API, NDK, archs, p4a branch...) of a spec that built successfully."""
    changed = []
    for key in ENV_KEYS:
        v = spec_get(good_spec, key)
        if v is not None and spec_get(spec, key) != v:
            spec = spec_set(spec, key, v)
            changed.append("%s = %s" % (key, v))
    return spec, changed


# ===================================================================== project doctor
# Most "the app flashes and closes" problems are decided BEFORE the build: the APK is built
# without a library the code imports, without data files (fonts/json...), or without the
# INTERNET permission.  The doctor reads the project and repairs buildozer.spec first.
STDLIB_FALLBACK = set("""abc argparse array ast asyncio base64 binascii bisect builtins calendar cmath
collections concurrent configparser contextlib copy csv ctypes dataclasses datetime decimal difflib
email enum errno fnmatch fractions ftplib functools gc getpass glob gzip hashlib heapq hmac html http
importlib inspect io ipaddress itertools json logging math mimetypes multiprocessing numbers operator
os pathlib pickle platform pprint queue random re runpy secrets select shlex shutil signal smtplib
socket sqlite3 ssl stat statistics string struct subprocess sys tarfile tempfile textwrap threading
time timeit traceback types typing unicodedata unittest urllib uuid warnings weakref webbrowser
xml zipfile zlib zoneinfo""".split())
STDLIB = set(getattr(sys, "stdlib_module_names", ())) or STDLIB_FALLBACK
PROVIDED = {"kivy", "android", "jnius", "kivy_deps", "__future__"}      # supplied by Kivy / python-for-android
NET_MODULES = {"requests", "urllib", "urllib3", "http", "socket", "httpx", "aiohttp", "websocket",
               "websockets", "ssl", "ftplib", "smtplib", "xmlrpc", "socks", "sockshandler"}

# import name -> python-for-android requirement(s) (including the hidden dependencies)
PKG_MAP = {
    "requests": ["requests", "urllib3", "idna", "charset-normalizer", "certifi"],
    "urllib3": ["urllib3"], "idna": ["idna"], "certifi": ["certifi"],
    "charset_normalizer": ["charset-normalizer"],
    "bs4": ["beautifulsoup4", "soupsieve"], "soupsieve": ["soupsieve"],
    "PIL": ["pillow"], "yaml": ["pyyaml"], "six": ["six"],
    "dateutil": ["python-dateutil", "six"], "pytz": ["pytz"],
    "cv2": ["opencv"], "numpy": ["numpy"], "lxml": ["lxml"], "plyer": ["plyer"],
    "httpx": ["httpx", "httpcore", "h11", "anyio", "sniffio", "certifi", "idna"],
    "websocket": ["websocket-client"], "socks": ["pysocks"], "sockshandler": ["pysocks"],
    "arabic_reshaper": ["arabic-reshaper"], "bidi": ["python-bidi"],
    "pygments": ["pygments"], "jwt": ["pyjwt"], "tqdm": ["tqdm"], "colorama": ["colorama"],
    "kivymd": ["kivymd"], "pandas": ["pandas"], "matplotlib": ["matplotlib"],
}

STABLE_SPEC = [("android.api", "33"), ("android.minapi", "24"), ("android.ndk", "25b"),
               ("android.archs", "arm64-v8a"), ("p4a.branch", "v2024.01.21"),
               ("android.accept_sdk_license", "True")]
IGNORE_EXT = {"pyc", "pyo", "spec", "bak", "bat", "zip", "apk", "aab", "exe", "log", "lock", "tmp", "swp",
              "pyd", "dll", "so", "gitignore", "ds_store", "txt~"}


def norm_pkg(n):
    return re.split(r"[=<>!~\[;]", n.strip())[0].strip().lower().replace("_", "-")


def req_list(spec):
    cur = spec_get(spec, "requirements") or "python3,kivy"
    return [r.strip() for r in cur.split(",") if r.strip()]


class _Imports(ast.NodeVisitor):
    def __init__(self):
        self.found, self.optional, self.full, self.depth = set(), set(), set(), 0

    def _add(self, name):
        (self.optional if self.depth else self.found).add(name.split(".")[0])
        self.full.add(name)

    def visit_Import(self, node):
        for a in node.names:
            self._add(a.name)

    def visit_ImportFrom(self, node):
        if node.level == 0 and node.module:
            self._add(node.module)

    def visit_Try(self, node):
        catches = any(h.type is None or any(k in ast.dump(h.type) for k in
                      ("ImportError", "ModuleNotFoundError", "'Exception'")) for h in node.handlers)
        if catches:                                   # try: import x / except ImportError -> optional
            self.depth += 1
            for n in node.body:
                self.visit(n)
            self.depth -= 1
            for n in node.handlers + node.orelse + node.finalbody:
                self.visit(n)
        else:
            self.generic_visit(node)


def scan_project(files):
    imp = _Imports()
    exts = set()
    for rel, full in files.items():
        ext = os.path.splitext(rel)[1].lower().lstrip(".")
        if ext:
            exts.add(ext)
        if ext == "py":
            try:
                imp.visit(ast.parse(open(full, encoding="utf-8", errors="ignore").read()))
            except (SyntaxError, ValueError):
                pass
    return imp, exts


def doctor(files, spec, profile):
    """Repair buildozer.spec. Returns (spec, notes, third_party_import_names)."""
    notes = []
    imp, exts = scan_project(files)
    local = {os.path.splitext(rel.split("/")[0])[0] for rel in files}
    third = sorted(n for n in imp.found if n not in STDLIB and n not in PROVIDED and n not in local)

    reqs = req_list(spec)
    have = {norm_pkg(r) for r in reqs}
    if "python3" not in have:
        reqs.insert(0, "python3")
        have.add("python3")
        notes.append("added 'python3' to requirements")
    if "kivy" in imp.found and "kivy" not in have:
        reqs.append("kivy==2.3.0" if profile == "stable" else "kivy")
        have.add("kivy")
        notes.append("added 'kivy' to requirements")
    needed = {"python3", "kivy", "hostpython3"}
    for name in third:
        pkgs = PKG_MAP.get(name)
        for p in (pkgs if pkgs is not None else [name]):
            needed.add(norm_pkg(p))
            if norm_pkg(p) not in have:
                reqs.append(p)
                have.add(norm_pkg(p))
                notes.append("added '%s' to requirements (your code imports '%s')%s" % (
                    p, name, "  <- guessed name, check it" if pkgs is None else ""))
    PROTECTED.update(needed)                          # never let an auto-fix drop a library the code needs
    spec = spec_set(spec, "requirements", ",".join(reqs))

    if NET_MODULES & (imp.found | imp.optional) or any(f.startswith("kivy.network") for f in imp.full):
        perms = [p.strip() for p in (spec_get(spec, "android.permissions") or "").split(",") if p.strip()]
        if not any("INTERNET" in p for p in perms):
            perms.append("INTERNET")
            spec = spec_set(spec, "android.permissions", ",".join(perms))
            notes.append("added the INTERNET permission (your code uses the network)")

    cur = [e.strip().lstrip(".") for e in (spec_get(spec, "source.include_exts") or
                                           "py,png,jpg,kv,atlas").split(",") if e.strip()]
    add = sorted(e for e in exts if e not in IGNORE_EXT and e not in cur)
    if add:
        spec = spec_set(spec, "source.include_exts", ",".join(cur + add))
        notes.append("packaged more file types into the APK: " + ", ".join(add))

    if spec_get(spec, "source.dir") != ".":
        spec = spec_set(spec, "source.dir", ".")
        notes.append("source.dir = .")
    if profile == "stable":
        for k, v in STABLE_SPEC:
            if spec_get(spec, k) != v:
                spec = spec_set(spec, k, v)
                notes.append("%s = %s  (known-good setting)" % (k, v))
    else:
        spec = spec_set(spec, "android.accept_sdk_license", "True")
    return spec, notes, third


def merge_workflow(new_wf, old_wf):
    """Take only the CI-environment tweaks (Java / Cython / setuptools) from the last good workflow."""
    notes = []
    m = re.search(r'java-version: "(\d+)"', old_wf)
    if m and 'java-version: "%s"' % m.group(1) not in new_wf:
        new_wf = re.sub(r'java-version: "\d+"', 'java-version: "%s"' % m.group(1), new_wf)
        notes.append("Java %s" % m.group(1))
    m = re.search(r'"(cython[^"]*)"', old_wf)
    cur = re.search(r'"(cython[^"]*)"', new_wf)
    if m and cur and m.group(1) != cur.group(1):
        new_wf = new_wf.replace('"%s"' % cur.group(1), '"%s"' % m.group(1))
        notes.append(m.group(1))
    if '"setuptools<70"' in old_wf and "setuptools virtualenv" in new_wf:
        new_wf = new_wf.replace("setuptools virtualenv", '"setuptools<70" virtualenv')
        notes.append("setuptools<70")
    return new_wf, notes


# ---- crash guard: written into the APK as main.py; your own main.py becomes real_main.py
GUARD_MARK = "APK Cloud Builder crash guard"
BOOTSTRAP = r'''# ---- APK Cloud Builder crash guard (generated) ----
# Shows errors on the phone screen instead of closing silently. Safe to delete.
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REAL = os.path.join(_HERE, "__REAL__")
_shown = []


def _save(text):
    try:
        try:
            from android.storage import app_storage_path
            base = app_storage_path()
        except Exception:
            base = _HERE
        with open(os.path.join(base, "crash_log.txt"), "a", encoding="utf-8") as f:
            f.write(text + "\n" + "-" * 40 + "\n")
    except Exception:
        pass


def _view(text):
    from kivy.graphics import Color, Rectangle
    from kivy.metrics import dp, sp
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.label import Label
    from kivy.uix.scrollview import ScrollView
    from kivy.core.clipboard import Clipboard
    root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
    with root.canvas.before:
        Color(1, 1, 1, 1)
        rect = Rectangle(pos=root.pos, size=root.size)
    root.bind(pos=lambda w, v: setattr(rect, "pos", v), size=lambda w, v: setattr(rect, "size", v))
    root.add_widget(Label(text="The app hit an error (send a screenshot of this page)", bold=True,
                          size_hint_y=None, height=dp(44), font_size=sp(15), color=(0.8, 0.1, 0.1, 1)))
    sv = ScrollView()
    body = Label(text=text[-6000:], size_hint_y=None, color=(0.1, 0.1, 0.1, 1), font_size=sp(11),
                 halign="left", valign="top")
    body.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
    body.bind(texture_size=lambda w, s: setattr(w, "height", s[1]))
    sv.add_widget(body)
    root.add_widget(sv)
    row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
    cp = Button(text="Copy error")
    cp.bind(on_release=lambda *a: Clipboard.copy(text))
    row.add_widget(cp)
    root.add_widget(row)
    return root, row


def _popup(text):
    try:
        from kivy.uix.button import Button
        from kivy.uix.popup import Popup
        root, row = _view(text)
        pop = Popup(title="Error (the app keeps running)", content=root, size_hint=(0.96, 0.92))
        close = Button(text="Close")
        close.bind(on_release=pop.dismiss)
        row.add_widget(close)
        pop.open()
    except Exception:
        traceback.print_exc()


def _fallback(text):
    try:
        from kivy.app import App
        from kivy.core.window import Window
        from kivy.uix.button import Button
        Window.clearcolor = (1, 1, 1, 1)

        class ErrorApp(App):
            def build(self):
                root, row = _view(text)
                close = Button(text="Close")
                close.bind(on_release=lambda *a: self.stop())
                row.add_widget(close)
                return root
        ErrorApp().run()
    except Exception:
        traceback.print_exc()


def _install_handler():
    try:
        from kivy.base import ExceptionHandler, ExceptionManager
        from kivy.clock import Clock

        class Guard(ExceptionHandler):
            def handle_exception(self, inst):
                if isinstance(inst, (SystemExit, KeyboardInterrupt)):
                    return ExceptionManager.RAISE
                text = "".join(traceback.format_exception(type(inst), inst, inst.__traceback__))
                _save(text)
                if len(_shown) < 3:
                    _shown.append(text)
                    Clock.schedule_once(lambda dt, t=text: _popup(t), 0)
                return ExceptionManager.PASS
        ExceptionManager.add_handler(Guard())
    except Exception:
        pass


def _main():
    _install_handler()
    try:
        import runpy
        if not os.path.exists(_REAL):
            raise FileNotFoundError("__REAL__ is missing from the APK")
        runpy.run_path(_REAL, run_name="__main__")
    except Exception:
        text = traceback.format_exc()
        _save(text)
        _fallback(text)


_main()
'''


def inject_guard(others):
    """Return (files, real_name). Wraps main.py unless it is already wrapped."""
    main = others.get("main.py")
    if main is None or GUARD_MARK.encode() in main:
        return others, None
    name = "real_main.py"
    while name in others:
        name = "_" + name
    out = dict(others)
    out[name] = main
    out["main.py"] = BOOTSTRAP.replace("__REAL__", name).encode("utf-8")
    return out, name


PROTECTED = {"python3", "kivy", "hostpython3"}


def fix_network(text, spec, wf):
    return spec, wf                                   # same files, just run again


def fix_cython(text, spec, wf):
    pins = ["cython==0.29.36", "cython>=3.0.8,<3.1", "cython<3.0"]
    for i, p in enumerate(pins):
        if '"%s"' % p in wf and i + 1 < len(pins):
            return spec, wf.replace('"%s"' % p, '"%s"' % pins[i + 1])
    return None


def fix_setuptools(text, spec, wf):
    if "setuptools virtualenv" in wf:
        return spec, wf.replace("setuptools virtualenv", '"setuptools<70" virtualenv')
    return None


def fix_apt_missing(text, spec, wf):
    m = (re.search(r"Unable to locate package (\S+)", text) or
         re.search(r"Package '([^']+)' has no installation candidate", text))
    if m and (" " + m.group(1)) in wf:
        return spec, wf.replace(" " + m.group(1), "", 1)
    return None


def fix_req_drop(text, spec, wf):
    names = re.findall(r"No matching distribution found for ([A-Za-z0-9_.\-]+)", text)
    names += re.findall(r"Could not find a version that satisfies the requirement ([A-Za-z0-9_.\-]+)", text)
    names += re.findall(r"Failed building wheel for ([A-Za-z0-9_.\-]+)", text)
    names += re.findall(r"ERROR: Failed to build '?([A-Za-z0-9_.\-]+)'?", text)
    cur = spec_get(spec, "requirements")
    if not cur:
        return None
    reqs = [r.strip() for r in cur.split(",") if r.strip()]
    for n in names:
        base = re.split(r"[=<>!~]", n)[0].lower().replace("_", "-")
        for r in reqs:
            rb = re.split(r"[=<>!~]", r)[0].lower().replace("_", "-")
            if rb == base and rb not in PROTECTED:
                return spec_set(spec, "requirements", ",".join(x for x in reqs if x != r)), wf
    return None


def fix_java(text, spec, wf):
    if 'java-version: "17"' in wf:
        return spec, wf.replace('java-version: "17"', 'java-version: "11"')
    return None


def fix_ndk(text, spec, wf):
    if spec_get(spec, "android.ndk") != "25b":
        return spec_set(spec, "android.ndk", "25b"), wf
    return None


def fix_buildtools(text, spec, wf):
    if spec_get(spec, "android.build_tools_version") != "34.0.0" or spec_get(spec, "android.api") != "33":
        s = spec_set(spec, "android.build_tools_version", "34.0.0")
        return spec_set(s, "android.api", "33"), wf
    return None


def fix_license(text, spec, wf):
    if spec_get(spec, "android.accept_sdk_license") != "True":
        return spec_set(spec, "android.accept_sdk_license", "True"), wf
    return None


def fix_arm64(text, spec, wf):
    if spec_get(spec, "android.archs") != "arm64-v8a":
        return spec_set(spec, "android.archs", "arm64-v8a"), wf
    return None


def fix_p4a_stable(text, spec, wf):
    if spec_get(spec, "p4a.branch") != "v2024.01.21":
        return spec_set(spec, "p4a.branch", "v2024.01.21"), wf
    return None


def fix_pinned_api(text, spec, wf):
    s = spec
    for k, v in (("android.api", "33"), ("android.minapi", "24"), ("android.ndk", "25b")):
        s = spec_set(s, k, v)
    return (s, wf) if s != spec else None


def fix_p4a_latest(text, spec, wf):
    if spec_get(spec, "p4a.branch") != "master":
        return spec_set(spec, "p4a.branch", "master"), wf
    return None


def fix_missing_modules(missing, spec):
    """The built APK lacks these modules -> add the matching requirements."""
    reqs = req_list(spec)
    have = {norm_pkg(r) for r in reqs}
    added = False
    for name in missing:
        for p in PKG_MAP.get(name, [name]):
            if norm_pkg(p) not in have:
                reqs.append(p)
                have.add(norm_pkg(p))
                added = True
    return spec_set(spec, "requirements", ",".join(reqs)) if added else None


# (name, regex or None for last-resort guesses, function, max uses)
FIXES = [
    ("retry (temporary network problem)",
     r"timed out|Connection (?:reset|refused|aborted)|Could not (?:GET|resolve|download)|Read timed out|"
     r"Temporary failure|Network is unreachable|HTTP/1\.1 5\d\d|Remote end closed|IncompleteRead|early EOF", fix_network, 2),
    ("package missing in apt list", r"Unable to locate package|has no installation candidate", fix_apt_missing, 2),
    ("accept the Android SDK license", r"licen[cs]e[^\n]*(?:not|accept)|accept[^\n]*licen[cs]e", fix_license, 1),
    ("switch Java version", r"Unsupported class file major version|requires Java|UnsupportedClassVersionError|"
                            r"invalid source release|Java \d+ is required", fix_java, 1),
    ("change Cython version", r"Cython[^\n]*(?:version|[Ee]rror)|cython[^\n]*not found|Cython\.Compiler", fix_cython, 2),
    ("pin setuptools", r"pkg_resources|No module named 'distutils'|setuptools[^\n]*(?:error|deprecated)", fix_setuptools, 1),
    ("drop a Python package that cannot be built",
     r"No matching distribution found|Failed building wheel|Could not find a version that satisfies|Failed to build", fix_req_drop, 3),
    ("pin Android build-tools/API", r"[Aa]idl not found|build-tools", fix_buildtools, 1),
    ("pin NDK 25b", r"[Uu]nsupported NDK|NDK[^\n]*(?:not supported|error|failed)|ndk-build", fix_ndk, 1),
    # last-resort guesses, tried in this order when nothing above recognises the error
    ("build only arm64-v8a (fewer things to compile)", None, fix_arm64, 1),
    ("use the stable python-for-android release", None, fix_p4a_stable, 1),
    ("pin Android API 33 / NDK 25b", None, fix_pinned_api, 1),
    ("try another Cython version", None, fix_cython, 2),
    ("use the latest python-for-android", None, fix_p4a_latest, 1),
]


def choose_fix(text, spec, wf, used):
    """Return (description, new_spec, new_wf) for the first applicable untried fix, or None."""
    for guess in (False, True):
        for name, pat, fn, limit in FIXES:
            if (pat is None) != guess or used.get(name, 0) >= limit:
                continue
            if pat is not None and not re.search(pat, text):
                continue
            res = fn(text, spec, wf)
            if res is None:
                continue
            used[name] = used.get(name, 0) + 1
            return name, res[0], res[1]
    return None


# ======================================================================= GitHub client
class CloudBuild:
    def __init__(self, token, repo, proj, out, proxy="", api=API, log=print,
                 progress=lambda v, t: None, poll=10, should_stop=lambda: False, max_fixes=5, reuse=True,
                 profile="stable", guard=True):
        self.token, self.repo, self.proj, self.out = token.strip(), repo.strip(), proj, out
        self.api, self.log, self.progress, self.poll, self.should_stop = api, log, progress, poll, should_stop
        self.max_fixes = max_fixes
        self.reuse = reuse
        self.profile, self.guard = profile, guard
        handlers = []
        if proxy.strip():
            p = "http://" + proxy.strip().replace("http://", "")
            handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
        self.opener = urllib.request.build_opener(*handlers)
        self.noredir = urllib.request.build_opener(_NoRedirect, *handlers)
        self.owner = None
        self.run_id = None
        self.attempt = 1
        self.fixes_applied = []
        self.final_spec = None

    # ---- http
    def _headers(self, extra=None):
        h = {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "apk-cloud-builder"}
        h.update(extra or {})
        return h

    def call(self, method, path, body=None):
        import json
        url = path if path.startswith("http") else self.api + path
        data = json.dumps(body).encode() if body is not None else None
        last = None
        for attempt in range(4):
            if self.should_stop():
                raise Stopped()
            req = urllib.request.Request(url, data=data, method=method, headers=self._headers(
                {"Content-Type": "application/json"} if data else None))
            try:
                with self.opener.open(req, timeout=45) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "ignore")[:300]
                if e.code in (500, 502, 503, 504) and attempt < 3:
                    last = GHError(e.code, msg)
                    time.sleep(3 * (attempt + 1))
                    continue
                raise GHError(e.code, msg)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = e
                time.sleep(3 * (attempt + 1))
        raise RuntimeError("Cannot reach GitHub (%s). Check your internet, or fill in the optional "
                           "proxy field / turn on a VPN." % (last,))

    def redirect_target(self, url, accept="application/vnd.github+json"):
        """GitHub answers log/artifact/asset requests with a redirect to a signed URL."""
        req = urllib.request.Request(url, headers=self._headers({"Accept": accept}))
        try:
            with self.noredir.open(req, timeout=45) as r:
                return r.geturl(), r.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return e.headers["Location"], None
            raise GHError(e.code, e.read().decode("utf-8", "ignore")[:200])

    def download(self, url, dest, tick=lambda done, total: None):
        req = urllib.request.Request(url, headers={"User-Agent": "apk-cloud-builder"})
        with self.opener.open(req, timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                tick(done, total)

    def P(self, v, text):
        self.progress(v, "Attempt %d: %s" % (self.attempt, text) if self.attempt > 1 else text)

    # ---- steps
    def put_file(self, branch, path, data):
        url = "/repos/%s/%s/contents/%s" % (self.owner, self.repo, urllib.parse.quote(path))
        sha = None
        try:
            sha = self.call("GET", url + "?ref=" + branch).get("sha")
        except GHError as e:
            if e.code != 404:
                raise
        if sha and sha == git_blob_sha(data):
            return False
        body = {"message": "Update " + path, "content": base64.b64encode(data).decode(), "branch": branch}
        if sha:
            body["sha"] = sha
        self.call("PUT", url, body)
        return True

    def _get_text(self, path, ref):
        d = self.call("GET", "/repos/%s/%s/contents/%s?ref=%s" % (
            self.owner, self.repo, urllib.parse.quote(path), ref))
        return base64.b64decode(d["content"]).decode("utf-8")

    def find_last_good(self):
        """Settings (workflow + spec) of the newest successful build, read from the commit it ran on."""
        try:
            runs = self.call("GET", "/repos/%s/%s/actions/workflows/%s/runs?status=success&"
                                    "event=workflow_dispatch&per_page=5" % (self.owner, self.repo, WORKFLOW_FILE))
        except GHError:
            return None
        for run in runs.get("workflow_runs", []):
            sha = run.get("head_sha")
            if not sha:
                continue
            try:
                return (run.get("run_number"), self._get_text(".github/workflows/" + WORKFLOW_FILE, sha),
                        self._get_text("buildozer.spec", sha))
            except GHError:
                continue
        return None

    def upload(self, branch, files):
        changed = 0
        for i, (path, data) in enumerate(sorted(files.items()), 1):
            self.P(0.08 + 0.15 * i / len(files), "Uploading %s" % path)
            if self.put_file(branch, path, data):
                changed += 1
                self.log("uploaded " + path)
        self.log("%d file(s) uploaded, %d already up to date" % (changed, len(files) - changed))

    def trigger_and_wait(self, branch):
        t0 = time.time()
        self.P(0.25, "Starting the cloud build")
        for attempt in range(10):
            try:
                self.call("POST", "/repos/%s/%s/actions/workflows/%s/dispatches" % (
                    self.owner, self.repo, WORKFLOW_FILE), {"ref": branch})
                break
            except GHError as e:
                if e.code == 404 and attempt < 9:
                    time.sleep(5)               # workflow not indexed yet
                    continue
                if e.code in (403, 404):
                    raise RuntimeError("GitHub refused to start the build. Make sure the token has the "
                                       "'repo' and 'workflow' permissions. (%s)" % e)
                raise
        run = None
        for _ in range(30):
            if self.should_stop():
                raise Stopped()
            runs = self.call("GET", "/repos/%s/%s/actions/workflows/%s/runs?event=workflow_dispatch"
                                    "&per_page=5" % (self.owner, self.repo, WORKFLOW_FILE))
            for r in runs.get("workflow_runs", []):
                if parse_time(r["created_at"]).timestamp() >= t0 - 60 and r["id"] != self.run_id:
                    run = r
                    break
            if run:
                break
            time.sleep(4)
        if not run:
            raise RuntimeError("The build did not start. Open github.com/%s/%s/actions to check." % (
                self.owner, self.repo))
        self.run_id = run["id"]
        self.log("Build started: https://github.com/%s/%s/actions/runs/%s" % (
            self.owner, self.repo, self.run_id))
        last_step = ""
        while True:
            if self.should_stop():
                raise Stopped()
            run = self.call("GET", "/repos/%s/%s/actions/runs/%s" % (self.owner, self.repo, self.run_id))
            jobs = self.call("GET", "/repos/%s/%s/actions/runs/%s/jobs" % (
                self.owner, self.repo, self.run_id)).get("jobs", [])
            steps = [s for j in jobs for s in j.get("steps", [])]
            if steps:
                done = sum(1 for s in steps if s["status"] == "completed")
                cur = next((s["name"] for s in steps if s["status"] == "in_progress"), last_step)
                last_step = cur
                self.P(0.30 + 0.60 * done / len(steps), "Cloud build: %s" % cur)
            else:
                self.P(0.30, "Waiting for a GitHub build machine…")
            if run["status"] == "completed":
                return run, jobs
            time.sleep(self.poll)

    def diagnose(self, jobs):
        """Return text describing why the build failed (end of the failing step's log)."""
        bad_step, text = "?", ""
        for j in jobs:
            if j.get("conclusion") != "failure":
                continue
            bad_step = next((s["name"] for s in j.get("steps", []) if s.get("conclusion") == "failure"), "?")
            try:                                            # 1) annotation written by run_logged
                anns = self.call("GET", "/repos/%s/%s/check-runs/%s/annotations?per_page=50" % (
                    self.owner, self.repo, j["id"]))
                for a in anns if isinstance(anns, list) else []:
                    if "BUILD_LOG_TAIL" in str(a.get("title")):
                        text = a.get("message") or ""
            except Exception:
                pass
            if not text:                                    # 2) full log download (may be blocked)
                try:
                    loc, _ = self.redirect_target("%s/repos/%s/%s/actions/jobs/%s/logs" % (
                        self.api, self.owner, self.repo, j["id"]))
                    req = urllib.request.Request(loc, headers={"User-Agent": "apk-cloud-builder"})
                    text = self.opener.open(req, timeout=60).read().decode("utf-8", "ignore")[-24000:]
                except Exception as e:
                    self.log("(could not fetch the log: %s)" % e)
            break
        text = "[failed step: %s]\n%s" % (bad_step, text)
        self.log("✘ %s" % text.splitlines()[0])
        for line in text.splitlines()[-45:]:
            self.log(line)
        try:
            os.makedirs(self.out, exist_ok=True)
            path = os.path.join(self.out, "cloud_failure_%d.txt" % self.attempt)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.log("Full error text saved to: %s" % path)
        except Exception:
            pass
        return text

    def apk_warnings(self, jobs):
        """Warnings written by the 'Check APK contents' step of a successful build."""
        out = []
        for j in jobs:
            try:
                anns = self.call("GET", "/repos/%s/%s/check-runs/%s/annotations?per_page=50" % (
                    self.owner, self.repo, j["id"]))
            except Exception:
                continue
            for a in anns if isinstance(anns, list) else []:
                if "APK_CHECK" in str(a.get("title")):
                    out.append(a.get("message") or "")
        return out

    def run(self):
        self.P(0.03, "Checking your GitHub token")
        self.owner = self.call("GET", "/user")["login"]
        self.log("Signed in to GitHub as %s" % self.owner)

        self.P(0.07, "Preparing the repository")
        try:
            info = self.call("GET", "/repos/%s/%s" % (self.owner, self.repo))
        except GHError as e:
            if e.code != 404:
                raise
            info = self.call("POST", "/user/repos", {"name": self.repo, "auto_init": True, "private": False,
                                                      "description": "APK builds (APK Cloud Builder)"})
            self.log("Created repository %s/%s" % (self.owner, self.repo))
            time.sleep(3)
        branch = info.get("default_branch") or "main"

        files = collect_files(self.proj)
        if "main.py" not in files or "buildozer.spec" not in files:
            raise RuntimeError("The project folder must contain main.py and buildozer.spec.")
        spec = open(files["buildozer.spec"], encoding="utf-8").read()
        original_spec = spec
        wf = WORKFLOW
        if self.reuse:
            good = self.find_last_good()
            if good:
                num, gwf, gspec = good
                notes = []
                if self.profile == "latest":
                    spec, changed = merge_good_settings(spec, gspec)
                    notes += changed
                if "BUILD_LOG_TAIL" in gwf:
                    wf, wnotes = merge_workflow(wf, gwf)
                    notes += wnotes
                self.log("Reusing the CI settings of your last successful build (#%s)." % num)
                for n in notes:
                    self.log("   • " + n)
            else:
                self.log("No earlier successful build found - starting with the default settings.")

        spec, notes, check_names = doctor(files, spec, self.profile)
        self.log("Project doctor (%s profile): %s" % (self.profile, "nothing to repair" if not notes else ""))
        for n in notes:
            self.log("   • " + n)

        others = {p: open(f, "rb").read() for p, f in files.items() if p != "buildozer.spec"}
        others[".cloud_builder_imports.txt"] = "\n".join(check_names).encode("utf-8")
        if self.guard:
            others, real = inject_guard(others)
            if real:
                self.log("Crash guard added: errors will be shown on the phone screen (your main.py -> %s)" % real)
        used = {}
        total = self.max_fixes + 1
        for self.attempt in range(1, total + 1):
            if self.attempt > 1:
                self.log("── Attempt %d of %d ──" % (self.attempt, total))
            allf = dict(others)
            allf["buildozer.spec"] = spec.encode("utf-8")
            allf[".github/workflows/" + WORKFLOW_FILE] = wf.encode("utf-8")
            self.upload(branch, allf)
            run, jobs = self.trigger_and_wait(branch)
            if run["conclusion"] == "success":
                missing = []
                for w in self.apk_warnings(jobs):
                    m = re.search(r"APK_MISSING_MODULES:\s*([\w.,\- ]+)", w)
                    if m:
                        missing += [x.strip() for x in m.group(1).split(",") if x.strip()]
                if missing and self.attempt < total:
                    fixed = fix_missing_modules(missing, spec)
                    if fixed:
                        spec = fixed
                        self.fixes_applied.append("add missing modules: " + ", ".join(missing))
                        self.log("✘ The APK is missing %s - they would make the app close at start. "
                                 "Adding them and building again…" % ", ".join(missing))
                        continue
                self.P(0.92, "Downloading the APK")
                res = self.fetch_apk(run)
                if spec != original_spec:
                    self.final_spec = spec
                    res["spec_changed"] = True
                res["fixes"] = list(self.fixes_applied)
                res["warnings"] = ([("These modules are not inside the APK: %s - the app may close at start."
                                     % ", ".join(missing))] if missing else [])
                for w in res["warnings"]:
                    self.log("⚠ " + w)
                return res
            text = self.diagnose(jobs)
            if self.attempt >= total:
                break
            fix = choose_fix(text, spec, wf, used)
            if not fix:
                self.log("I do not know an automatic fix for this error. Send me the saved error file.")
                break
            name, spec, wf = fix
            self.fixes_applied.append(name)
            self.log("🔧 Automatic fix: %s - trying again…" % name)
            self.progress(0.0, "Applying fix: %s" % name)
        raise RuntimeError("The cloud build kept failing after %d attempt(s). The error text was saved "
                           "as cloud_failure_N.txt in your output folder - send me that file." % self.attempt)

    def fetch_apk(self, run):
        base = "/repos/%s/%s" % (self.owner, self.repo)
        os.makedirs(self.out, exist_ok=True)
        dest = os.path.join(self.out, "app-build-%s.apk" % run["run_number"])
        tick = lambda d, t: self.progress(0.92 + 0.07 * (d / t if t else 0), "Downloading the APK")
        link = "https://github.com/%s/%s/releases" % (self.owner, self.repo)
        try:                                              # 1) release asset
            for rel in self.call("GET", base + "/releases?per_page=10"):
                if rel.get("tag_name") == "build-%s" % run["run_number"] and rel.get("assets"):
                    loc, _ = self.redirect_target(rel["assets"][0]["url"], "application/octet-stream")
                    self.download(loc, dest, tick)
                    return {"file": dest, "link": link}
        except Exception as e:
            self.log("release download failed (%s), trying the artifact…" % e)
        try:                                              # 2) workflow artifact (zip with the apk)
            arts = self.call("GET", base + "/actions/runs/%s/artifacts" % self.run_id).get("artifacts", [])
            loc, _ = self.redirect_target(arts[0]["archive_download_url"])
            tmp = dest + ".zip"
            self.download(loc, tmp, tick)
            with zipfile.ZipFile(tmp) as z:
                name = next(n for n in z.namelist() if n.lower().endswith(".apk"))
                with open(dest, "wb") as f:
                    f.write(z.read(name))
            os.remove(tmp)
            return {"file": dest, "link": link}
        except Exception as e:
            self.log("artifact download failed (%s)" % e)
        return {"file": None, "link": link}


# ======================================================================= GUI
def main():
    try:
        import customtkinter as ctk
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "customtkinter"])
        import customtkinter as ctk
    from tkinter import filedialog, messagebox

    BG, CARD, ACCENT, ACCENT_H = "#F4F6FB", "#FFFFFF", "#4F46E5", "#4338CA"
    GREEN, RED, AMBER, TEXT, MUTED = "#16A34A", "#DC2626", "#D97706", "#1F2937", "#6B7280"

    class App(ctk.CTk):
        def __init__(self):
            super().__init__()
            ctk.set_appearance_mode("light")
            self.title("APK Cloud Builder")
            self.geometry("940x860")
            self.minsize(820, 720)
            self.configure(fg_color=BG)
            self.q, self.stop_flag, self.running = queue.Queue(), threading.Event(), False
            self.tmp = tempfile.mkdtemp(prefix="cloudbuilder_")
            self.last_out = ""
            self.ui()
            self.after(150, self.poll)

        def btn(self, parent, text, cmd, color=ACCENT, hover=ACCENT_H, dark=False, **kw):
            o = dict(height=38, corner_radius=12, fg_color=color, hover_color=hover,
                     font=("Segoe UI", 13, "bold"), text_color=TEXT if dark else "white")
            o.update(kw)
            return ctk.CTkButton(parent, text=text, command=cmd, **o)

        def card(self):
            c = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
            c.pack(fill="x", padx=28, pady=6)
            return c

        def field(self, parent, label, row, var, show=None, width=None):
            ctk.CTkLabel(parent, text=label, font=("Segoe UI", 12), text_color=MUTED
                         ).grid(row=row, column=0, sticky="w", padx=16, pady=(10, 0))
            e = ctk.CTkEntry(parent, textvariable=var, height=34, corner_radius=10, show=show)
            e.grid(row=row + 1, column=0, sticky="ew", padx=(16, 8), pady=(0, 4))
            return e

        def ui(self):
            head = ctk.CTkFrame(self, fg_color="transparent")
            head.pack(fill="x", padx=28, pady=(18, 4))
            ctk.CTkLabel(head, text="APK Cloud Builder", font=("Segoe UI", 28, "bold"),
                         text_color=TEXT).pack(side="left")
            ctk.CTkLabel(head, text="  builds on GitHub's servers - no VPN needed for downloads",
                         font=("Segoe UI", 13), text_color=MUTED).pack(side="left", pady=(10, 0))

            c = self.card()
            c.grid_columnconfigure(0, weight=1)
            self.token = ctk.StringVar()
            self.repo = ctk.StringVar(value="apk-cloud-builds")
            self.proj = ctk.StringVar()
            self.out = ctk.StringVar(value=self.default_out())
            self.proxy = ctk.StringVar()
            here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "project")
            if os.path.isfile(os.path.join(here, "main.py")):
                self.proj.set(here)
            self.field(c, "GitHub token (kept only in memory)", 0, self.token, show="•")
            self.btn(c, "Create token…", self.open_token, "#E5E7EB", "#D1D5DB", dark=True, width=130
                     ).grid(row=1, column=1, padx=(0, 16), pady=(0, 4))
            self.field(c, "Repository name (created automatically)", 2, self.repo)
            self.field(c, "Project folder (main.py + buildozer.spec)", 4, self.proj)
            bb = ctk.CTkFrame(c, fg_color="transparent")
            bb.grid(row=5, column=1, padx=(0, 16), pady=(0, 4))
            self.btn(bb, "Folder…", self.pick_folder, width=70).pack(side="left")
            self.btn(bb, "ZIP…", self.pick_zip, "#E5E7EB", "#D1D5DB", dark=True, width=56
                     ).pack(side="left", padx=(6, 0))
            self.field(c, "Save APK to", 6, self.out)
            self.btn(c, "Browse…", self.pick_out, "#E5E7EB", "#D1D5DB", dark=True, width=130
                     ).grid(row=7, column=1, padx=(0, 16), pady=(0, 4))
            self.field(c, "Optional: proxy for reaching github.com (ip:port) - leave empty normally", 8, self.proxy)
            fx = ctk.CTkFrame(c, fg_color="transparent")
            fx.grid(row=10, column=0, sticky="w", padx=16, pady=(6, 12))
            ctk.CTkLabel(fx, text="Auto-fix attempts after a failed build", font=("Segoe UI", 12),
                         text_color=MUTED).pack(side="left")
            self.fixes = ctk.CTkOptionMenu(fx, values=["0", "2", "4", "5", "6", "8"], height=32, width=70,
                                           fg_color="#EEF0FA", button_color=ACCENT,
                                           button_hover_color=ACCENT_H, text_color=TEXT)
            self.fixes.set("5")
            self.fixes.pack(side="left", padx=8)
            self.reuse = ctk.CTkCheckBox(c, text="Reuse the settings of my last successful build "
                                                 "(skips the failed attempts)", fg_color=ACCENT,
                                         text_color=TEXT, font=("Segoe UI", 12))
            self.reuse.select()
            self.reuse.grid(row=11, column=0, sticky="w", padx=16, pady=(0, 8))
            pf = ctk.CTkFrame(c, fg_color="transparent")
            pf.grid(row=12, column=0, sticky="w", padx=16, pady=(0, 6))
            ctk.CTkLabel(pf, text="Build profile", font=("Segoe UI", 12), text_color=MUTED).pack(side="left")
            self.profile = ctk.CTkOptionMenu(pf, values=["Stable (recommended)", "Latest"], height=32, width=190,
                                             fg_color="#EEF0FA", button_color=ACCENT,
                                             button_hover_color=ACCENT_H, text_color=TEXT)
            self.profile.set("Stable (recommended)")
            self.profile.pack(side="left", padx=8)
            self.guard = ctk.CTkCheckBox(c, text="Crash guard: show the error on the phone screen instead of "
                                                 "closing silently", fg_color=ACCENT, text_color=TEXT,
                                         font=("Segoe UI", 12))
            self.guard.select()
            self.guard.grid(row=13, column=0, sticky="w", padx=16, pady=(0, 12))

            bar = ctk.CTkFrame(self, fg_color="transparent")
            bar.pack(fill="x", padx=28, pady=(8, 2))
            self.go = self.btn(bar, "☁  Build in the cloud", self.start, height=46, width=240,
                               font=("Segoe UI", 15, "bold"))
            self.go.pack(side="left")
            self.stop_btn = self.btn(bar, "■ Stop", self.stop, RED, "#B91C1C", width=90, state="disabled")
            self.stop_btn.pack(side="left", padx=8)
            self.open_btn = self.btn(bar, "Open folder", self.open_out, "#E5E7EB", "#D1D5DB", dark=True, width=120)
            self.open_btn.pack(side="left")
            self.timer = ctk.CTkLabel(bar, text="00:00", font=("Segoe UI", 16, "bold"), text_color=MUTED)
            self.timer.pack(side="right")

            self.bar = ctk.CTkProgressBar(self, height=10, progress_color=ACCENT)
            self.bar.set(0)
            self.bar.pack(fill="x", padx=30, pady=(8, 2))
            self.stage = ctk.CTkLabel(self, text="Ready.", font=("Segoe UI", 13), text_color=MUTED)
            self.stage.pack(anchor="w", padx=32)
            self.banner = ctk.CTkLabel(self, text="", font=("Segoe UI", 12, "bold"), wraplength=860,
                                       justify="left", corner_radius=10)
            self.logbox = ctk.CTkTextbox(self, fg_color="#FFFFFF", text_color="#111827",
                                         font=("Consolas", 11), corner_radius=16, wrap="word")
            self.logbox.pack(fill="both", expand=True, padx=28, pady=(8, 16))
            self.logbox.configure(state="disabled")

        def default_out(self):
            home = os.path.expanduser("~")
            for p in (os.path.join(home, "OneDrive", "Desktop"), os.path.join(home, "Desktop"), home):
                if os.path.isdir(p):
                    return p
            return home

        def open_token(self):
            webbrowser.open("https://github.com/settings/tokens/new?scopes=repo,workflow"
                            "&description=APK%20Cloud%20Builder")
            self.show("On the GitHub page: set Expiration (e.g. 30 days), click 'Generate token', copy it "
                      "and paste it here. Never share it with anyone.", AMBER)

        def pick_folder(self):
            d = filedialog.askdirectory(title="Project folder")
            if d:
                self.proj.set(d)

        def pick_zip(self):
            z = filedialog.askopenfilename(filetypes=[("ZIP", "*.zip")])
            if not z:
                return
            dest = os.path.join(self.tmp, "zip_%d" % int(time.time()))
            with zipfile.ZipFile(z) as zf:
                zf.extractall(dest)
            for root, _d, files in os.walk(dest):
                if "buildozer.spec" in files:
                    self.proj.set(root)
                    return
            messagebox.showwarning("Not found", "No buildozer.spec inside this ZIP.")

        def pick_out(self):
            d = filedialog.askdirectory()
            if d:
                self.out.set(d)

        def open_out(self):
            if os.path.isdir(self.out.get()):
                os.startfile(self.out.get())

        def show(self, text, color):
            self.banner.configure(text=text, text_color="white", fg_color=color)
            self.banner.pack(fill="x", padx=30, pady=(6, 0), before=self.logbox)

        def start(self):
            if self.running:
                return
            if not self.token.get().strip():
                return messagebox.showinfo("Token needed", "Press 'Create token…', generate a token on "
                                                          "GitHub and paste it in the first box.")
            if not os.path.isfile(os.path.join(self.proj.get(), "buildozer.spec")):
                return messagebox.showwarning("Project", "Choose a folder that contains buildozer.spec and main.py.")
            self.running = True
            self.stop_flag.clear()
            self.banner.pack_forget()
            self.logbox.configure(state="normal")
            self.logbox.delete("1.0", "end")
            self.logbox.configure(state="disabled")
            self.bar.set(0)
            self.t0 = time.time()
            self.go.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            args = (self.token.get(), self.repo.get(), self.proj.get(), self.out.get(), self.proxy.get(),
                    int(self.fixes.get()), bool(self.reuse.get()),
                    "latest" if self.profile.get() == "Latest" else "stable", bool(self.guard.get()))
            threading.Thread(target=self.work, args=args, daemon=True).start()

        def stop(self):
            self.stop_flag.set()
            self.stage.configure(text="Stopping…")

        def work(self, token, repo, proj, out, proxy, fixes, reuse, profile="stable", guard=True):
            cb = CloudBuild(token, repo, proj, out, proxy, log=lambda m: self.q.put(("log", m)),
                            progress=lambda v, t: self.q.put(("p", v, t)), should_stop=self.stop_flag.is_set, max_fixes=fixes, reuse=reuse,
                            profile=profile, guard=guard)
            try:
                res = cb.run()
                if res.get("spec_changed") and cb.final_spec:        # keep the working settings locally
                    try:
                        spec_path = os.path.join(proj, "buildozer.spec")
                        if not os.path.exists(spec_path + ".bak"):
                            os.replace(spec_path, spec_path + ".bak")
                        with open(spec_path, "w", encoding="utf-8") as f:
                            f.write(cb.final_spec)
                        self.q.put(("log", "Saved the fixed buildozer.spec to your project (backup: buildozer.spec.bak)"))
                    except Exception:
                        pass
                self.q.put(("done", res))
            except Stopped:
                try:
                    if cb.run_id:
                        cb.call("POST", "/repos/%s/%s/actions/runs/%s/cancel" % (cb.owner, cb.repo, cb.run_id))
                except Exception:
                    pass
                self.q.put(("err", "Stopped."))
            except Exception as e:
                self.q.put(("err", str(e)))

        def poll(self):
            lines = []
            for _ in range(300):
                try:
                    m = self.q.get_nowait()
                except queue.Empty:
                    break
                if m[0] == "log":
                    lines.append(m[1])
                elif m[0] == "p":
                    self.bar.set(m[1])
                    self.stage.configure(text="%s  ·  %d%%" % (m[2], int(m[1] * 100)))
                elif m[0] == "err":
                    self.finish()
                    self.show(m[1], RED)
                elif m[0] == "done":
                    self.finish()
                    f = m[1]["file"]
                    if f:
                        self.bar.set(1)
                        self.stage.configure(text="Finished  ·  100%")
                        w = m[1].get("warnings") or []
                        if w:
                            self.show("✔ APK ready: %s\n⚠ %s" % (f, " ".join(w)), AMBER)
                        else:
                            self.show("✔ APK ready: %s\nIf the app still fails on the phone it will now show the "
                                      "error on screen - send me a screenshot of it." % f, GREEN)
                    else:
                        self.show("The build succeeded but the download was blocked. Open %s in your browser "
                                  "(a VPN may be needed) and download the .apk there." % m[1]["link"], AMBER)
            if lines:
                self.logbox.configure(state="normal")
                self.logbox.insert("end", "\n".join(lines) + "\n")
                self.logbox.see("end")
                self.logbox.configure(state="disabled")
            if self.running:
                s = int(time.time() - self.t0)
                self.timer.configure(text="%02d:%02d" % (s // 60, s % 60))
            self.after(150, self.poll)

        def finish(self):
            self.running = False
            self.go.configure(state="normal")
            self.stop_btn.configure(state="disabled")

    App().mainloop()


if __name__ == "__main__":
    main()
