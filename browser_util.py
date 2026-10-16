"""Real-browser helper for the QUREO / SPRIX solvers.

The portals only respond to an Egyptian IP, which comes from a browser VPN
extension (Urban VPN Proxy, 1VPN, ...). Playwright cannot load extensions
through its normal API (it always passes --disable-extensions) and the branded
Google Chrome build ignores --load-extension, but the Playwright-bundled
Chromium (Chrome for Testing) still honours --load-extension on every OS. So we:

  1. locate the VPN extension installed in Chrome/Edge/Brave (any OS);
  2. launch that Chromium ourselves, headed, with a dedicated persistent profile
     and the extension loaded, exposing the DevTools Protocol on a port;
  3. connect Playwright to the running browser over CDP and drive the real,
     extension-backed context, so every request goes through the VPN.

The window is left open between runs, so the VPN only has to be turned on once;
later runs reuse the same window and profile.

Override discovery with the environment variables:
    SOLVER_VPN_EXT      full path to an unpacked extension folder
    SOLVER_VPN_EXT_ID   a specific extension id to look for
"""
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "chrome_profile")
CDP_URL = "http://127.0.0.1:9222"

# Preferred extension ids (Urban VPN Proxy first, then Free VPN - 1VPN).
KNOWN_VPN_IDS = [
    "eppiocemhmnlbhjplcgkofciiegomcon",  # Urban VPN Proxy
    "akcocjjpkmlniicdeemdceeajlmoabhg",  # Free VPN - 1VPN
]
_VPN_WORDS = ("vpn", "proxy", "urban")


# ------------------------------------------------------------- discovery
def _user_data_roots():
    """Return the Chrome-family 'User Data' directories for this OS."""
    roots = []
    home = os.path.expanduser("~")
    if os.name == "nt":
        for env in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            for vendor in ("Google\\Chrome\\User Data",
                           "Microsoft\\Edge\\User Data",
                           "BraveSoftware\\Brave-Browser\\User Data",
                           "Chromium\\User Data"):
                roots.append(os.path.join(base, vendor))
    elif sys.platform == "darwin":
        support = os.path.join(home, "Library", "Application Support")
        for vendor in ("Google/Chrome", "Microsoft Edge",
                       "BraveSoftware/Brave-Browser", "Chromium"):
            roots.append(os.path.join(support, vendor))
    else:
        config = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config"))
        for vendor in ("google-chrome", "google-chrome-beta", "chromium",
                       "microsoft-edge", "BraveSoftware/Brave-Browser"):
            roots.append(os.path.join(config, vendor))
    return roots


def _localized(manifest, locale_dir, key):
    value = manifest.get(key) or ""
    if value.startswith("__MSG_"):
        msg = value[6:-2]
        for loc in ("en", "en_US", "en_GB"):
            path = os.path.join(locale_dir, loc, "messages.json")
            if os.path.exists(path):
                try:
                    data = json.load(open(path, encoding="utf-8"))
                    for k, v in data.items():
                        if k.lower() == msg.lower():
                            return v.get("message", "")
                except Exception:
                    pass
        return ""
    return value


def _extensions_found():
    """Yield (ext_id, name, path) for every unpacked extension on the machine."""
    for root in _user_data_roots():
        pattern = os.path.join(root, "*", "Extensions", "*", "*", "manifest.json")
        for manifest_path in glob.glob(pattern):
            version_dir = os.path.dirname(manifest_path)
            ext_id = os.path.basename(os.path.dirname(version_dir))
            try:
                manifest = json.load(open(manifest_path, encoding="utf-8"))
            except Exception:
                continue
            name = _localized(manifest, version_dir, "name")
            yield ext_id, name, version_dir, manifest


def find_vpn_extension(verbose=False):
    """Return the path to the newest unpacked VPN extension, or None."""
    override = os.environ.get("SOLVER_VPN_EXT")
    if override and os.path.exists(os.path.join(override, "manifest.json")):
        return override

    wanted = [os.environ["SOLVER_VPN_EXT_ID"]] if os.environ.get("SOLVER_VPN_EXT_ID") \
        else KNOWN_VPN_IDS
    candidates = {}          # ext_id -> newest path
    for ext_id, name, path, _m in _extensions_found():
        is_vpn = ext_id in wanted or any(w in (name or "").lower() for w in _VPN_WORDS)
        if not is_vpn:
            continue
        if ext_id not in candidates or path > candidates[ext_id][0]:
            candidates[ext_id] = (path, name)

    if not candidates:
        return None
    # prefer a known id, otherwise any match
    for ext_id in wanted:
        if ext_id in candidates:
            return candidates[ext_id][0]
    ext_id = max(candidates, key=lambda k: candidates[k][0])
    if verbose:
        print(f"using VPN extension: {candidates[ext_id][1] or ext_id} ({ext_id})")
    return candidates[ext_id][0]


# ------------------------------------------------------------- browser
def is_running(url=CDP_URL):
    try:
        with urllib.request.urlopen(url + "/json/version", timeout=1):
            return True
    except Exception:
        return False


def _wait_port(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running(url):
            return True
        time.sleep(0.5)
    return False


def _clear_stale_locks():
    """Remove lock files left behind by a crashed/closed Chromium so a launch
    can always reuse the same profile."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie",
                 "lockfile"):
        path = os.path.join(PROFILE, name)
        try:
            if os.path.lexists(path):
                os.remove(path)
        except OSError:
            pass
    # on Windows the lock file lives in the profile root too
    for name in ("SingletonLock", "lockfile"):
        for sub in ("Default", ""):
            path = os.path.join(PROFILE, sub, name)
            try:
                if os.path.lexists(path):
                    os.remove(path)
            except OSError:
                pass


def launch(chrome, urls=(), url=CDP_URL):
    """Start the automation Chromium with the profile + VPN extension.

    Returns True when it was the first launch of this profile."""
    if not chrome or not os.path.exists(chrome):
        sys.exit("Playwright's Chromium is missing; run: "
                 "python3 -m playwright install chromium")
    port = url.rsplit(":", 1)[-1]
    os.makedirs(PROFILE, exist_ok=True)
    first_run = not os.path.exists(os.path.join(PROFILE, "Default"))
    args = [
        chrome,
        f"--user-data-dir={PROFILE}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--start-maximized",
        "--window-size=1280,900",
    ]
    ext = find_vpn_extension()
    if ext:
        args += [f"--load-extension={ext}", f"--disable-extensions-except={ext}"]
        print(f"VPN extension loaded from: {ext}")
    else:
        print("!! No VPN extension found. Install Urban VPN Proxy (or set "
              "SOLVER_VPN_EXT to its folder); the Egyptian VPN will be "
              "unavailable.", file=sys.stderr)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")
    args += list(urls)

    kwargs = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    log_path = os.path.join(HERE, "browser.log")
    for attempt in (1, 2):
        _clear_stale_locks()
        log = open(log_path, "ab")
        subprocess.Popen(args, stdout=log, stderr=log, **kwargs)
        if _wait_port(url, timeout=30):
            return first_run
        print(f"browser did not start (attempt {attempt}); see {log_path}",
              file=sys.stderr)
        time.sleep(2)
    sys.exit(f"the browser never exposed its debugging port.\n"
             f"Check the log: {log_path}\n"
             f"If it says the profile is in use, close other windows using "
             f"{PROFILE} or delete that folder and retry.")


def _remind(first_run):
    print("\n" + "=" * 64)
    if first_run:
        print("A Chrome window was opened for the solvers.")
        print("Open the VPN extension, pick an EGYPT location and turn it ON.")
        print("This is saved, so you only have to do it once.")
    else:
        print("Chrome window is open - make sure the VPN is ON and set to EGYPT.")
    print("=" * 64)
    if sys.stdin.isatty():
        try:
            input("Press Enter once the Egypt VPN is connected... ")
        except EOFError:
            pass


def connect(pw, urls=(), url=CDP_URL):
    """Attach Playwright to the extension-backed browser, launching it if needed.

    Returns (browser, context) where context is the real browser context that
    carries the VPN extension.
    """
    if not is_running(url):
        first_run = launch(pw.chromium.executable_path, urls, url)
        _remind(first_run)
    browser = pw.chromium.connect_over_cdp(url)
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    return browser, ctx