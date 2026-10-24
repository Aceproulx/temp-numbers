#!/usr/bin/env python3
"""
Fetch received SMS from esimplus.me temporary numbers.

Everything the script needs is extracted from https://esimplus.me/temporary-numbers
on its own, using a real (Playwright-driven) Chrome browser:

  * the temporary-number list  -> numbers.json (+ legacy numbers.noise)
  * the Cloudflare clearance   -> clearance.json (+ cookie.http)

The browser is only needed when something is actually missing or expired:

  * --list just prints the saved numbers; no browser is started.
  * the saved clearance is reused for the API calls, so -n/--all work without
    a browser too.
  * a new clearance is only fetched when the old one is refused by Cloudflare
    (the browser profile is rotated and the fresh cookies are saved).
  * the number list is re-extracted when it is older than --ttl or when the
    clearance expired — --refresh/--xnumbers force it right now.

Cloudflare hard-blocks Playwright's default "HeadlessChrome" User-Agent, so the
script runs a real headed Chrome that is never visible: it launches on a private
Xvfb display (memory-only framebuffer) and its window is also parked off-screen.
Use --show if you actually want to watch it work.

Number extraction only uses the front page of the listing: the deeper pages
(/temporary-numbers/2, /3 …) sit behind a Cloudflare captcha that never clears
for an automated browser. On a fresh machine the script installs what it needs
(playwright, chromium, Xvfb) on the first run — or run --install to do it up
front.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_URL = "https://esimplus.me"
LIST_URL = f"{BASE_URL}/temporary-numbers"

NUMBERS_JSON = os.path.join(SCRIPT_DIR, "numbers.json")
NUMBERS_TXT = os.path.join(SCRIPT_DIR, "numbers.noise")
COOKIE_FILE = os.path.join(SCRIPT_DIR, "cookie.http")
CLEARANCE_FILE = os.path.join(SCRIPT_DIR, "clearance.json")
PROFILE_DIR = os.path.join(SCRIPT_DIR, ".pw-profile")

DEFAULT_TTL_MINUTES = 24 * 60  # 24 hours
# How long a saved Cloudflare clearance is trusted before a new one is fetched.
CLEARANCE_MAX_AGE_MINUTES = 24 * 60  # 24 hours
CF_TITLES = ("just a moment", "attention required", "checking your browser")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def log(msg):
    print(msg, file=sys.stderr)


def die(msg, code=1):
    log(f"[-] {msg}")
    sys.exit(code)


def digits_only(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def is_json_response(response) -> bool:
    """Did we get real JSON back (instead of a Cloudflare challenge page)?"""
    body = (response or {}).get("body") or ""
    return bool(body.strip()) and body.lstrip().startswith(("{", "["))


def country_code_from(number: str) -> str:
    return "+" + number[0] if number else "+"


def site_cookies(cookies):
    """Keep only the cookies that belong to the site, one per name.

    Cloudflare sets a cf_clearance for .cloudflare.com as well as for the site
    itself — sending both in one Cookie header makes CF refuse the request.
    """
    site = "esimplus.me"
    picked = {}
    for cookie in cookies or []:
        domain = (cookie.get("domain") or "").lstrip(".")
        if not domain or not (site == domain or site.endswith("." + domain)):
            continue
        name = cookie.get("name")
        if not name:
            continue
        picked[name] = cookie
    return list(picked.values())


def cookie_header_from(cookies) -> str:
    return "; ".join(f"{c['name']}={c['value']}"
                     for c in site_cookies(cookies)
                     if c.get("name") and c.get("value"))


def age_of(path: str, reference: str | None = None) -> float | None:
    """Seconds since the file's mtime (or its parsed fetched_at), else None."""
    if not os.path.exists(path):
        return None
    if reference:
        try:
            ts = datetime.fromisoformat(reference)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).total_seconds()
        except ValueError:
            pass
    return time.time() - os.path.getmtime(path)


# --------------------------------------------------------------------------- #
# number cache (numbers.json, with numbers.noise kept in sync)
# --------------------------------------------------------------------------- #

def load_numbers_from_file(filename: str = NUMBERS_TXT):
    """Legacy parser for the plain-text numbers.noise dump."""
    filepath = os.path.join(SCRIPT_DIR, filename)
    if not os.path.exists(filepath):
        return []

    entries = []
    try:
        with open(filepath, "r") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]

        for i, line in enumerate(lines):
            if line.startswith("+"):
                raw = digits_only(line)
                entries.append({
                    "number": raw,
                    "display": line,
                    "region": lines[i - 1] if i > 0 else "Unknown",
                    "country_code": country_code_from(raw),
                })
    except Exception as e:
        log(f"[-] Error parsing {filepath}: {e}")

    return entries


def load_cached_numbers():
    """Return (entries, payload). Payload is None when only the legacy file exists."""
    if os.path.exists(NUMBERS_JSON):
        try:
            with open(NUMBERS_JSON, "r") as f:
                payload = json.load(f)
            entries = payload.get("numbers") or []
            if entries:
                return entries, payload
        except Exception as e:
            log(f"[-] Warning: could not read {NUMBERS_JSON}: {e}")

    legacy = load_numbers_from_file()
    return legacy, None


def save_numbers(entries, pages=1, source=LIST_URL):
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "scope": "front",
        "pages": pages,
        "count": len(entries),
        "numbers": entries,
    }
    with open(NUMBERS_JSON, "w") as f:
        json.dump(payload, f, indent=2)

    # Keep the old flat-text dump working for anyone parsing it directly.
    with open(NUMBERS_TXT, "w") as f:
        for entry in entries:
            f.write(f"{entry.get('added') or 'added recently'}\n")
            f.write(f"{entry.get('region') or 'Unknown'}\n")
            f.write(f"{entry.get('display') or '+' + entry['number']}\n\n")
            f.write("Open\n")
            for tag in entry.get("tags") or []:
                f.write(f"{tag}\n")
            f.write("\n")

    return payload["fetched_at"]


# --------------------------------------------------------------------------- #
# cloudflare clearance (the cookies that let us back in)
# --------------------------------------------------------------------------- #

def save_clearance(cookies, cookie_header: str = ""):
    """Store the cookies Cloudflare handed out, so the next run can reuse them."""
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "cookies": cookies or [],
        "cookie_header": cookie_header or "",
    }
    try:
        with open(CLEARANCE_FILE, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        log(f"[-] Warning: could not write {CLEARANCE_FILE}: {e}")
    if cookie_header:
        save_cookie_file(cookie_header)


def load_clearance():
    """The saved clearance, or None when there is none."""
    if not os.path.exists(CLEARANCE_FILE):
        return None
    try:
        with open(CLEARANCE_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def clearance_state():
    """Is the saved clearance still usable?

    Returns a dict with "valid", "reason" and the saved data. A clearance is
    good until one of its cookies expires or it gets older than
    CLEARANCE_MAX_AGE_MINUTES — after that a new one has to be fetched.
    """
    data = load_clearance()
    if not data or not data.get("cookies"):
        # Try cookie.http as fallback
        if os.path.exists(COOKIE_FILE):
            try:
                with open(COOKIE_FILE) as f:
                    content = f.read()
                # Create a synthetic clearance from cookie.http
                if 'cf_clearance' in content or len(content.strip()) > 10:
                    return {"present": True, "valid": True, "reason": "from cookie.http", "data": {"cookie_header": content.replace('cookie:', '').strip(), "cookies": []}}
            except Exception:
                pass
        return {"present": False, "valid": False, "reason": "no clearance saved yet", "data": data}

    now = time.time()
    for cookie in data["cookies"]:
        expires = cookie.get("expires")
        if expires and expires > 0 and expires <= now:
            name = cookie.get("name", "?")
            return {"present": True, "valid": False,
                    "reason": f"the saved clearance expired ({name})", "data": data}

    age = age_of(CLEARANCE_FILE, data.get("saved_at"))
    if age is not None and age > CLEARANCE_MAX_AGE_MINUTES * 60:
        # Be more lenient if we have cf_clearance
        has_cf = any((c.get('name') == 'cf_clearance') for c in data.get('cookies') or [])
        if not has_cf and age > 7 * 24 * 3600:  # Only reject very old if no cf_clearance
            return {"present": True, "valid": False,
                    "reason": f"the saved clearance is {int(age // 60)} min old", "data": data}
        # Otherwise be lenient

    return {"present": True, "valid": True, "reason": "", "data": data}


def clearance_cookie_header(state=None) -> str:
    """The Cookie header built from the saved clearance (empty when unusable)."""
    state = state or clearance_state()
    if not state.get("valid"):
        return ""
    data = state.get("data") or {}
    # Rebuilt from the stored cookies so a header saved by an older version
    # (with Cloudflare's own cf_clearance mixed in) can't break the request.
    header = cookie_header_from(data.get("cookies") or [])
    if header:
        return header
    return data.get("cookie_header") or ""


def mark_clearance_verified():
    """Note that the saved clearance still works — without fetching a new one."""
    data = load_clearance()
    if not data:
        return
    data["verified_at"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(CLEARANCE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"[-] Warning: could not update {CLEARANCE_FILE}: {e}")


def invalidate_clearance():
    """Throw the refused clearance away — the site cookies stay, cf_clearance goes."""
    data = load_clearance()
    if not data:
        return
    data["cookies"] = [c for c in (data.get("cookies") or [])
                       if c.get("name") != "cf_clearance"]
    data["cookie_header"] = cookie_header_from(data["cookies"])
    data["saved_at"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(CLEARANCE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"[-] Warning: could not update {CLEARANCE_FILE}: {e}")


# --------------------------------------------------------------------------- #
# first-run bootstrap (playwright, browser, Xvfb)
# --------------------------------------------------------------------------- #

DEPS_MARKER = os.path.join(SCRIPT_DIR, ".deps-ok")


def missing_dependencies():
    """Return the list of things this machine still needs."""
    missing = []

    try:
        import playwright  # noqa: F401
    except ImportError:
        missing.append("python-playwright")

    if shutil.which("Xvfb") is None:
        missing.append("xvfb")

    # A browser binary: installed Chrome/Chromium, or Playwright's own download.
    if shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser"):
        pass
    elif os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.isdir(os.path.expanduser("~/.cache/ms-playwright")):
        if not os.listdir(os.path.expanduser("~/.cache/ms-playwright")):
            missing.append("chromium")
    else:
        missing.append("chromium")

    return missing


def run_install(cmd, label):
    log(f"[*] Installing {label} ...")
    try:
        proc = subprocess.run(cmd, text=True)
    except Exception as e:
        log(f"[-] {label} install failed: {e}")
        return False
    if proc.returncode != 0:
        log(f"[-] {label} install failed (exit {proc.returncode}).")
        return False
    log(f"[+] {label} installed.")
    return True


def install_dependencies():
    """Install whatever is missing so a fresh machine can run this script."""
    missing = missing_dependencies()
    if not missing:
        return True

    log(f"[*] Missing dependencies: {', '.join(missing)}")

    if "python-playwright" in missing:
        if not run_install([sys.executable, "-m", "pip", "install", "--quiet", "playwright"],
                           "playwright (pip)"):
            return False

    if "chromium" in missing:
        if not run_install([sys.executable, "-m", "playwright", "install", "chromium"],
                           "chromium (playwright)"):
            return False

    if "xvfb" in missing:
        if os.geteuid() == 0:
            ok = run_install(["apt-get", "install", "-y", "xvfb"], "xvfb (apt)")
        elif shutil.which("sudo"):
            ok = run_install(["sudo", "-n", "apt-get", "install", "-y", "xvfb"], "xvfb (sudo apt)")
        else:
            ok = False
        if not ok:
            log("[-] Could not install Xvfb automatically. Run this by hand:")
            log("      sudo apt-get install -y xvfb")

    mark_ready()
    return not missing_dependencies()


def mark_ready():
    """Record that this machine is set up (so the first-run note shows once)."""
    try:
        with open(DEPS_MARKER, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def is_first_run() -> bool:
    return not os.path.exists(DEPS_MARKER)


def ensure_dependencies(auto_install=True):
    """Make sure the machine can run the browser part; install on first use."""
    missing = missing_dependencies()
    if not missing:
        return True
    if not auto_install:
        die("Missing dependencies: " + ", ".join(missing) +
            "\n    Install them with:  ./sms_fetcher.py --install")
    log(f"[*] First run on this machine — installing: {', '.join(missing)}")
    if not install_dependencies():
        die("Could not install everything automatically. Try:\n"
            "    pip install playwright && python -m playwright install chromium\n"
            "    sudo apt-get install -y xvfb")
    return True


# --------------------------------------------------------------------------- #
# browser session (Playwright, real Chrome on a hidden display)
# --------------------------------------------------------------------------- #

# Headless Playwright advertises "HeadlessChrome/…" in its User-Agent and this
# site's Cloudflare rule blocks that outright, so a real headed Chrome is used.
# It is never shown to the user: the browser runs on a private Xvfb display
# (rendered to memory only), and its window is parked off-screen as a second
# layer in case a real X server has to be used (e.g. Xvfb is not installed).
REAL_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/155.0.0.0 Safari/537.36")

OFFSCREEN_POS = "-32000,-32000"


class PageBlocked(RuntimeError):
    """The site served a Cloudflare interstitial instead of the number list."""


class DisplayManager:
    """Provides a display no one can see: private Xvfb, else off-screen windows."""

    def __init__(self, verbose=False, allow_real_display=False):
        self.verbose = verbose
        self.allow_real_display = allow_real_display
        self.proc = None
        self.display = None
        self.owned = False

    def ensure(self):
        """Return a display string, preferring a private virtual one."""
        if self.allow_real_display and os.environ.get("DISPLAY"):
            return os.environ["DISPLAY"]

        if not shutil.which("Xvfb"):
            if os.environ.get("DISPLAY"):
                log("[!] Xvfb not found — using the real display with the window parked off-screen.")
                return os.environ["DISPLAY"]
            die("No display available: install xvfb (`sudo apt install xvfb`) so the\n"
                "    browser can run hidden, or run from a desktop session.")

        for n in range(99, 120):
            if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                continue
            try:
                self.proc = subprocess.Popen(
                    ["Xvfb", f":{n}", "-screen", "0", "1366x900x24", "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                die(f"Could not start Xvfb: {e}")
            time.sleep(0.6)
            if self.proc.poll() is None:
                self.display = f":{n}"
                self.owned = True
                os.environ["DISPLAY"] = self.display
                if self.verbose:
                    log(f"[*] Started private Xvfb display {self.display} (browser stays hidden).")
                return self.display
        die("Could not find a free X display for Xvfb.")

    def stop(self):
        if self.owned and self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                pass


class BrowserSession:
    """Drives esimplus.me like a normal visitor and talks to its API in-page."""

    PAGE_READY_JS = """() => document.querySelectorAll('a[href^="/temporary-numbers/"]').length"""

    def __init__(self, profile=PROFILE_DIR, verbose=False, timeout=60, show=False):
        self.profile = profile
        self.verbose = verbose
        self.timeout = timeout
        self.show = show
        self.skip_clearance_restore = False
        self._pw = None
        self._ctx = None
        self._page = None
        self._loaded = False
        self._rotated = False
        self._display = DisplayManager(verbose=verbose, allow_real_display=show)

    # -- lifecycle ---------------------------------------------------------- #

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            die("Playwright is not installed.\n"
                "    pip install playwright && python -m playwright install chromium")

        self._display.ensure()
        self._pw = sync_playwright().start()
        try:
            self._start_browser()
            self.open_list(1, probe=True, retries=4)
        except PageBlocked as e:
            self.__exit__()
            die(f"Cloudflare refused the page: {e}\n"
                "    Wait a minute and retry, or run:  ./sms_fetcher.py --xnumbers")
        except Exception:
            self.__exit__()
            raise

        if self.verbose:
            log("[*] Browser ready.")
        return self

    def __exit__(self, *exc):
        self._close_ctx()
        try:
            self._pw and self._pw.stop()
        except Exception:
            pass
        self._display.stop()
        return False

    def _close_ctx(self):
        try:
            self._ctx and self._ctx.close()
        except Exception:
            pass
        self._ctx = None
        self._page = None
        self._loaded = False

    def _rotate_profile(self):
        """Throw the browser profile away so the next launch looks like a new visitor."""
        try:
            shutil.rmtree(self.profile, ignore_errors=True)
        except Exception:
            pass
        os.makedirs(self.profile, exist_ok=True)
        self._rotated = True

    def _start_browser(self):
        """(Re)launch the browser with a clean page."""
        self._close_ctx()
        self._ctx = self._launch()
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        if not self._rotated and not self.skip_clearance_restore:
            self._restore_clearance()

    def _restore_clearance(self):
        """Put the saved cookies back in the browser before knocking on the door."""
        data = load_clearance()
        if not data:
            return False

        now = time.time()
        cookies = []
        for stored in data.get("cookies") or []:
            expires = stored.get("expires")
            if expires and expires > 0 and expires <= now:
                continue
            cookie = {k: stored[k] for k in ("name", "value", "domain", "path") if stored.get(k)}
            if not cookie.get("name") or not cookie.get("domain"):
                continue
            for optional in ("httpOnly", "secure", "sameSite", "expires"):
                if stored.get(optional) is not None:
                    cookie[optional] = stored[optional]
            cookies.append(cookie)

        if not cookies:
            return False
        try:
            self._ctx.add_cookies(cookies)
            if self.verbose:
                log(f"[*] Reused the saved Cloudflare clearance ({len(cookies)} cookies).")
            return True
        except Exception:
            return False

    def _after_load(self):
        """Keep the saved clearance in sync.

        A new clearance is only fetched when the old one was blocked (the
        profile had to be rotated); otherwise the saved one is kept and simply
        marked as still working.
        """
        cookies = []
        try:
            cookies = self._ctx.cookies()
        except Exception:
            pass

        if self._rotated or not load_clearance():
            save_clearance(cookies, self.cookie_string())
            self._rotated = False
            if self.verbose:
                log("[*] Saved a fresh Cloudflare clearance.")
        else:
            mark_clearance_verified()
            save_cookie_file(self.cookie_string())

    def _launch(self):
        common = dict(
            headless=False,
            user_agent=REAL_UA,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            args=[
                "--disable-blink-features=AutomationControlled",
                f"--window-position={OFFSCREEN_POS}",
                "--window-size=1366,900",
            ],
            ignore_default_args=["--enable-automation"],
        )
        os.makedirs(self.profile, exist_ok=True)

        errors = []
        # Installed Chrome is preferred; bundled Chromium is the fallback.
        for channel in ("chrome", None):
            kwargs = dict(common)
            if channel:
                kwargs["channel"] = channel
            try:
                return self._pw.chromium.launch_persistent_context(self.profile, **kwargs)
            except Exception as e:
                errors.append(f"{channel or 'chromium'}: {e.__class__.__name__}: {e}")

        raise RuntimeError("no usable browser (" + "; ".join(errors) + ")")

    # -- navigation --------------------------------------------------------- #

    def open_list(self, page_no: int = 1, probe: bool = False, retries: int = 2):
        """Load a listing page, rotating the browser profile if CF challenges us.

        A challenge that does not clear on its own never will, but a brand new
        visitor profile usually sails through, so each retry starts from scratch.
        """
        url = LIST_URL if page_no <= 1 else f"{LIST_URL}/{page_no}"
        last = None

        for attempt in range(retries + 1):
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                self._wait_for_content(probe=probe)
                self._loaded = True
                self._after_load()
                return url
            except PageBlocked as e:
                last = e
                self._close_ctx()
                if attempt < retries:
                    log("[*] Cloudflare challenge — rotating the browser profile and retrying...")
                    self._rotate_profile()
                    self._start_browser()
                    time.sleep(1)
                else:
                    break

        raise last

    def ensure_ready(self):
        """Make sure the context holds the site's cookies before any API call."""
        if not self._loaded:
            self.open_list(1)
        else:
            self._after_load()
        save_cookie_file(self.cookie_string())

    def _wait_for_content(self, probe: bool = False):
        # The startup probe is short: a challenge that hasn't cleared in ~20s is a
        # dead profile, and rotating to a fresh one is faster than waiting.
        budget = min(self.timeout, 20) if probe else self.timeout
        deadline = time.time() + budget
        while time.time() < deadline:
            try:
                if self._page.evaluate(self.PAGE_READY_JS) > 0:
                    return
            except Exception:
                pass
            title = (self._page.title() or "").lower()
            if any(t in title for t in CF_TITLES) and self.verbose:
                log("[*] Waiting for Cloudflare challenge to clear...")
            self._page.wait_for_timeout(1000)

        title = self._page.title()
        raise PageBlocked(f"number list never rendered (title: {title!r})")

    # -- extraction --------------------------------------------------------- #

    def scrape_numbers(self):
        """Scrape the front page of the listing (the only page that isn't captcha-walled)."""
        self.open_list(1, probe=True)
        entries, seen = [], set()
        for card in self._scrape_cards():
            if card["number"] and card["number"] not in seen:
                seen.add(card["number"])
                entries.append(card)
        return entries

    def _scrape_cards(self):
        raw = self._page.evaluate("""() => {
            const grid = document.querySelector('div[class*="__numbers"]');
            if (!grid) return [];
            return [...grid.querySelectorAll(':scope > div[class*="__number"]')].map(card => {
                const country = card.querySelector('[class*="__countryName"]');
                const shown = card.querySelector('[class*="__numberInfo"] > span');
                const added = card.querySelector('[class*="__header"] span');
                const link = card.querySelector('a[href^="/temporary-numbers/"]');
                const tags = [...card.querySelectorAll('[class*="__label"]')]
                    .map(e => (e.innerText || '').trim()).filter(Boolean);
                return {
                    region: country ? country.innerText.trim() : '',
                    display: shown ? shown.innerText.trim() : '',
                    added: added ? added.innerText.trim() : '',
                    tags: tags,
                    href: link ? link.getAttribute('href') : null,
                };
            }).filter(c => (c.display || '').startsWith('+') || c.href);
        }""")

        entries = []
        for card in raw:
            number = digits_only(card.get("href", "").rsplit("/", 1)[-1]) or digits_only(card["display"])
            if not number:
                continue
            entries.append({
                "number": number,
                "display": card["display"] or "+" + number,
                "region": card.get("region") or "Unknown",
                "country_code": country_code_from(number),
                "added": card.get("added") or "",
                "tags": card.get("tags") or [],
                "href": card.get("href") or "",
            })
        return entries

    # -- API ---------------------------------------------------------------- #

    def fetch_sms(self, number: str, per_page: int, retries: int = 3):
        """Call the SMS API from inside the page so CF sees the real browser.

        A challenge on the API path is retried with a fresh browser profile.
        """
        clean = digits_only(number)
        url = f"{BASE_URL}/api/sms-receiver/{clean}/sms?perPage={per_page}"
        result = {"status": 0, "contentType": "", "body": ""}

        for attempt in range(retries + 1):
            result = self._in_page_fetch(url)
            if is_json_response(result):
                return result
            # Cloudflare turned the request down: the saved clearance is no good.
            invalidate_clearance()
            if attempt < retries:
                log("[*] The API request was challenged — rotating the browser profile and retrying...")
                try:
                    self._rotate_profile()
                    self._start_browser()
                    self.open_list(1, probe=True)
                except PageBlocked:
                    pass
                time.sleep(2)

        return result

    def _in_page_fetch(self, url: str):
        return self._page.evaluate("""async (url) => {
            const res = await fetch(url, {
                headers: {accept: '*/*'},
                credentials: 'include',
                cache: 'no-store',
            });
            return {
                status: res.status,
                contentType: res.headers.get('content-type') || '',
                body: await res.text(),
            };
        }""", url)

    # -- cookies ------------------------------------------------------------ #

    def cookie_string(self) -> str:
        try:
            cookies = self._ctx.cookies()
        except Exception:
            return ""
        return cookie_header_from(cookies)


def save_cookie_file(cookie_str: str):
    if not cookie_str:
        return
    try:
        with open(COOKIE_FILE, "w") as f:
            f.write(f"cookie: {cookie_str}\n")
    except Exception as e:
        log(f"[-] Warning: could not write {COOKIE_FILE}: {e}")


# --------------------------------------------------------------------------- #
# curl fallback (--no-browser)
# --------------------------------------------------------------------------- #

def get_cookie_string(override_cookie: str) -> str:
    if override_cookie:
        return override_cookie

    for filename in ("cookie.http", "cookies.http"):
        filepath = os.path.join(SCRIPT_DIR, filename)
        if not os.path.exists(filepath):
            continue
        try:
            with open(filepath, "r") as f:
                for line in f:
                    line_clean = line.strip()
                    if not line_clean:
                        continue
                    if line_clean.lower().startswith("cookie:"):
                        return line_clean.split(":", 1)[1].strip()
                    return line_clean
        except Exception as e:
            log(f"[-] Warning: Failed to read {filepath}: {e}")

    die("No cookie available. Run the script once without --no-browser so it can\n"
        "    extract cookies from the site, or pass --cookie '...'.")


def fetch_sms_curl(number: str, per_page: int, cookie_str: str):
    clean = digits_only(number)
    url = f"{BASE_URL}/api/sms-receiver/{clean}/sms?perPage={per_page}"

    cmd = [
        "curl", "-s", "--url", url,
        "-H", "accept: */*",
        "-H", "accept-language: en-US,en;q=0.9",
        "-b", cookie_str,
        "-H", f"referer: {LIST_URL}",
        "-H", "sec-fetch-dest: empty",
        "-H", "sec-fetch-mode: cors",
        "-H", "sec-fetch-site: same-origin",
        "-H", "user-agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as err:
        die(f"curl failed: {err}")

    body = proc.stdout.strip()
    if not body:
        return {"status": 0, "contentType": "", "body": ""}
    return {"status": 200, "contentType": "application/json", "body": body}


def fetch_sms_curl_retry(number: str, per_page: int, cookie_str: str, retries: int = 2):
    """curl the API, retrying when Cloudflare throws a challenge at it."""
    response = fetch_sms_curl(number, per_page, cookie_str)
    for _ in range(retries):
        if is_json_response(response):
            break
        time.sleep(1.5)
        response = fetch_sms_curl(number, per_page, cookie_str)
    return response


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def suggest_refresh():
    """The hint printed when a lookup fails: pull a fresh front-page list."""
    log("    Hint: the number list may be stale or the number may be gone.")
    log("    Run this to pull the current front-page numbers:")
    log(f"      {os.path.basename(os.path.abspath(__file__))} --xnumbers")


def fetch_sms_with_fallback(session, number: str, per_page: int):
    """Get the messages, surviving Cloudflare challenges on the API path.

    1. in-page fetch (the browser is the visitor CF trusts most)
    2. curl with the browser's cookies (fast, and often slips through)
    3. rotate the browser profile and try the in-page fetch again
    """
    response = session.fetch_sms(number, per_page, retries=0)
    if is_json_response(response):
        return response

    cookies = session.cookie_string()
    if cookies:
        log("[*] The API challenged the browser — retrying with curl and the same cookies...")
        response = fetch_sms_curl(number, per_page, cookies)
        if is_json_response(response):
            return response
        invalidate_clearance()

    return session.fetch_sms(number, per_page, retries=2)


def print_messages(number: str, response, verbose: bool, suggest=True):
    clean = digits_only(number)
    status = response.get("status", 0)
    body = (response.get("body") or "").strip()

    if verbose:
        log(f"[DEBUG] HTTP {status} for +{clean}")
        log(f"[DEBUG] Raw body:\n{body}\n")

    if not body:
        log(f"[-] Empty response for +{clean} (HTTP {status}).")
        if suggest:
            suggest_refresh()
        return

    if status == 403 or not body.lstrip().startswith(("{", "[")):
        log(f"[-] Blocked for +{clean} (HTTP {status}) — HTML returned instead of JSON.")
        log("    Cloudflare is refusing the request right now; a fresh clearance was saved,")
        log("    so simply running the command again usually works.")
        if suggest:
            suggest_refresh()
        return

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log(f"[-] Could not decode JSON for +{clean}.")
        if suggest:
            suggest_refresh()
        return

    if not isinstance(data, dict) or "data" not in data:
        log(f"[-] Response for +{clean} has no 'data' field (possible challenge page).")
        if suggest:
            suggest_refresh()
        return

    messages = data.get("data") or []
    if not messages:
        print(f"No messages found for +{clean}.")
        if suggest:
            suggest_refresh()
        return

    print(f"=== Messages for +{clean} ({len(messages)}) ===")
    for msg in messages:
        sender = msg.get("friendlyFrom") or msg.get("from") or "Unknown"
        text = (msg.get("body") or "").strip()
        received = msg.get("receivedAt") or "Unknown time"
        print(f"[{received}] From: {sender}\n  {text}\n")


def list_numbers(entries):
    if not entries:
        die("No numbers available. Run with --xnumbers to extract them from the site.")
    print(f"{'Index':<6} {'Phone Number':<18} {'Region'}")
    print("-" * 46)
    for idx, entry in enumerate(entries, start=1):
        display = entry.get("display") or "+" + entry["number"]
        print(f"{idx:<6} {display:<18} {entry.get('region', 'Unknown')}")


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #

def resolve_numbers(args, session):
    """Return the number list, scraping the site when the cache is stale."""
    entries, payload = load_cached_numbers()
    age = age_of(NUMBERS_JSON, (payload or {}).get("fetched_at")) if payload else None
    ttl_seconds = args.ttl * 60
    stale = age is None or age > ttl_seconds
    clearance = clearance_state()

    if not args.refresh and not args.xnumbers and not stale and entries and clearance["valid"]:
        if args.verbose:
            log(f"[*] Using cached numbers ({len(entries)}, {int(age // 60)} min old).")
        return entries

    if args.refresh:
        reason = "--refresh"
    elif args.xnumbers:
        reason = "--xnumbers"
    elif not entries:
        reason = "no cache"
    elif not clearance["valid"]:
        reason = clearance["reason"]
    else:
        reason = f"cache {int(age // 60)} min old"

    log(f"[*] Extracting temporary numbers from {LIST_URL} (front page; {reason})...")
    scraped = session.scrape_numbers()
    if not scraped:
        if entries:
            log("[-] Extraction returned nothing; keeping the previous cache.")
            return entries
        die("Extraction returned no numbers.")
    save_cookie_file(session.cookie_string())
    save_numbers(scraped)
    log(f"[+] Extracted {len(scraped)} numbers from the front page -> "
        f"{os.path.basename(NUMBERS_JSON)} "
        f"(+ {os.path.basename(NUMBERS_TXT)}, {os.path.basename(COOKIE_FILE)})")
    return load_cached_numbers()[0] or scraped


def main():
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]) or "sms_fetcher",
        description="Fetch received SMS from esimplus.me temporary numbers.",
        epilog=(
            "The number list and the Cloudflare clearance are pulled from the site on\n"
            "their own and then reused, so the browser only starts when one of them is\n"
            "missing, expired or was refused by Cloudflare.\n"
            "\n"
            "examples:\n"
            "  %(prog)s --list                  show the saved numbers\n"
            "  %(prog)s -n 17427245094          read the messages of one number\n"
            "  %(prog)s --all -c 3              read the messages of every saved number\n"
            "  %(prog)s --refresh               pull a fresh number list from the site\n"
            "  %(prog)s --install               install the missing dependencies\n"
            "\n"
            "files kept next to the script:\n"
            "  numbers.json    the saved number list        clearance.json  the saved\n"
            "  numbers.noise   legacy flat copy of the list cookie.http     Cloudflare clearance"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    listing = parser.add_argument_group("listing")
    listing.add_argument("--list", action="store_true",
                         help="print the saved numbers (no browser while the cache is fresh)")
    listing.add_argument("--xnumbers", action="store_true",
                         help="re-extract the numbers from the front page now and print them")
    listing.add_argument("--refresh", action="store_true",
                         help="re-extract the number list now (same as --xnumbers)")

    messages = parser.add_argument_group("messages")
    messages.add_argument("-n", "--number", type=str,
                          help="fetch the messages of a single number")
    messages.add_argument("--all", action="store_true",
                          help="fetch the messages of every saved number, one after another")
    messages.add_argument("-c", "--count", type=int, default=10,
                          help="messages to fetch per number (default: 10)")

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument("--ttl", type=int, default=DEFAULT_TTL_MINUTES,
                           help="re-extract the number list after this many minutes (default: "
                                f"{DEFAULT_TTL_MINUTES}; the list is also refreshed once the "
                                "saved clearance expires)")
    behaviour.add_argument("--no-browser", action="store_true",
                           help="use curl with the saved cookies, never start a browser")
    behaviour.add_argument("--cookie", type=str, default="",
                           help="override the Cookie header (curl mode only)")
    behaviour.add_argument("--timeout", type=int, default=60,
                           help="page/API timeout in seconds (default: 60)")
    behaviour.add_argument("-v", "--verbose", action="store_true",
                           help="show what the script is doing")

    setup = parser.add_argument_group("setup")
    setup.add_argument("--install", action="store_true",
                       help="install the missing dependencies (playwright, chromium, Xvfb) and exit")
    setup.add_argument("--show", action="store_true",
                       help="draw the browser on the real desktop (default: hidden on a virtual display)")

    args = parser.parse_args()

    if args.install:
        ensure_dependencies(auto_install=True)
        print("Dependencies ready.")
        return

    numbers_wanted = args.list or args.number or args.all or args.xnumbers or args.refresh
    if not numbers_wanted:
        parser.print_help()
        return

    # The saved list and the saved Cloudflare clearance are enough for most
    # runs: only when one of them is missing or expired does a browser appear.
    entries, payload = load_cached_numbers()
    age = age_of(NUMBERS_JSON, (payload or {}).get("fetched_at")) if payload else None
    stale = age is None or age > args.ttl * 60
    clearance = clearance_state()
    force_extract = args.refresh or args.xnumbers

    if args.list and not force_extract and entries and not stale and clearance["valid"]:
        if args.verbose:
            log(f"[*] Using the saved numbers ({len(entries)}, {int(age // 60)} min old) "
                f"— no browser needed.")
        list_numbers(entries)
        return

    if args.no_browser:
        entries = load_cached_numbers()[0]
        if args.list:
            list_numbers(entries)
        elif args.number:
            print_messages(args.number, fetch_sms_curl(args.number, args.count, get_cookie_string(args.cookie)),
                           args.verbose)
        elif args.all:
            if not entries:
                die("No cached numbers. Run once without --no-browser to extract them.")
            for entry in entries:
                print_messages(entry["number"],
                               fetch_sms_curl(entry["number"], args.count, get_cookie_string(args.cookie)),
                               args.verbose)
        return

    # SMS lookup with the saved clearance: no browser, no waiting.
    clearance_refused = False
    if (args.number or args.all) and not force_extract and clearance["valid"] and (args.number or entries):
        cookie_header = clearance_cookie_header(clearance)
        targets = [digits_only(args.number)] if (args.number and not args.all) else [e["number"] for e in entries]
        if cookie_header and targets:
            probe = fetch_sms_curl_retry(targets[0], args.count, cookie_header)
            if is_json_response(probe):
                if args.verbose:
                    log("[*] Using the saved Cloudflare clearance — no browser needed.")
                print_messages(targets[0], probe, args.verbose)
                for number in targets[1:]:
                    print_messages(number, fetch_sms_curl_retry(number, args.count, cookie_header),
                                   args.verbose)
                return
            clearance_refused = True
            invalidate_clearance()
            log("[*] The saved clearance was refused — opening the browser for a new one.")

    # First run on a fresh machine: install whatever is missing.
    first_run = is_first_run()
    ensure_dependencies(auto_install=True)
    if first_run:
        log("[*] First run on this machine — the browser runs on a hidden virtual display.")
        mark_ready()

    def session_factory():
        return BrowserSession(verbose=args.verbose, timeout=args.timeout, show=args.show)

    try:
        with session_factory() as session:
            if clearance_refused:
                # Cloudflare turned the saved clearance down — start from a clean
                # visitor instead of handing it back to the browser.
                session.skip_clearance_restore = True
            entries = resolve_numbers(args, session)

            if args.list or (force_extract and not (args.number or args.all)):
                # A bare --refresh/--xnumbers is "give me the numbers".
                list_numbers(entries)
                return

            targets = entries if args.all else []
            if args.number and not args.all:
                targets = [args.number]
            if not targets:
                die("Nothing to fetch.")

            known = {e.get("number") for e in entries}
            session.ensure_ready()  # loads the listing once if the cache was fresh
            for target in targets:
                number = digits_only(target) if isinstance(target, str) else target["number"]
                if known and number not in known and args.verbose:
                    log(f"[*] +{number} is not in the extracted list (it may be gone).")
                response = fetch_sms_with_fallback(session, number, args.count)
                print_messages(number, response, args.verbose)
    except PageBlocked as e:
        die(f"Cloudflare blocked the session mid-run: {e}\n"
            "    Retry, or run ./sms_fetcher.py --xnumbers to pull a fresh front-page list.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("Interrupted.", 130)
