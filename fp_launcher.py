#!/usr/bin/env python3
"""Per-account Chromium launcher with deterministic fingerprint seeding.

Companion to the Blink patch in this tree
(third_party/blink/renderer/core/frame/navigator_fingerprint.{h,cc}).

The C++ side reads a single env var, CHROME_FP_SEED, and derives every
JS-visible value from it with FNV-1a. This script reproduces that exact
derivation in Python so the launcher can keep the *other* channels in sync:

  channel                   source of truth
  ------------------------  --------------------------------------------
  navigator.webdriver       C++    (always false when seeded)
  navigator.platform        C++    ("Win32")
  navigator.hardwareConc.   C++    (derived from seed)
  navigator.deviceMemory    C++    (derived from seed)
  navigator.languages       C++    (derived from seed)
  HTTP Accept-Language      --lang  (must match the C++ language choice)
  TLS/HTTP2 fingerprint     untouched in batch 1

Keeping the last two rows aligned is the whole point of duplicating the
derivation here: a profile claiming en-US in JS but zh-CN on the wire is one of
the cheapest inconsistencies to detect.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess

from _nowindow import no_window
import sys
import time
from pathlib import Path
from datetime import datetime

# --- must stay byte-for-byte in sync with navigator_fingerprint.cc ----------

FNV_OFFSET = 1469598103934665603
FNV_PRIME = 1099511628211
MASK64 = 0xFFFFFFFFFFFFFFFF

# Cores and RAM by GPU tier. Must stay in lockstep with kCores*/kMemory* in
# navigator_fingerprint.cc. A flat list produced "4 cores + RTX 3080 Ti" and
# "4 GB RAM + discrete GPU" -- combinations no real machine has.
CORES_BY_TIER = {
    0: [4, 6, 8, 12, 16],           # integrated graphics
    1: [6, 8, 12, 16, 20],          # mid-range discrete
    2: [8, 12, 16, 20, 24, 32],     # high-end discrete
}
MEMORY_BY_TIER = {
    0: [4.0, 8.0, 16.0, 32.0],
    1: [8.0, 16.0, 32.0],
    2: [16.0, 32.0],
}
PLATFORMS = ["Win32"]
# (languages, timezone candidates). Must stay in lockstep with kLocales in
# navigator_fingerprint.cc -- timezone is derived FROM the language, never drawn
# independently, so "zh-CN" can never resolve to an American zone.
LOCALES = [
    # Canonical IANA names only: Asia/Chongqing is a tzdata link to
    # Asia/Shanghai and ICU canonicalises it, which made the browser report a
    # different zone than this table derived. All other candidates round-trip.
    ("zh-CN,zh",       ["Asia/Shanghai"]),
    ("zh-CN,zh,en",    ["Asia/Shanghai"]),
    ("en-US,en",       ["America/New_York", "America/Los_Angeles"]),
    ("en-US,en,zh-CN", ["America/New_York"]),
    ("zh-TW,zh",       ["Asia/Taipei"]),
    ("ja-JP,ja",       ["Asia/Tokyo"]),
    ("ko-KR,ko",       ["Asia/Seoul"]),
]

# (vendor, renderer) pairs. Must stay in lockstep with kGpuProfiles in
# navigator_fingerprint.cc -- same order, same strings.
# (vendor, renderer, tier). Must stay in lockstep with kGpuProfiles in
# navigator_fingerprint.cc -- same order, same strings, same tier.
TIER_LOW, TIER_MID, TIER_HIGH = 0, 1, 2

GPU_PROFILES = [
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Ti (0x00002208) Direct3D11 vs_5_0 ps_5_0, D3D11)", TIER_HIGH),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)", TIER_HIGH),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)", TIER_MID),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)", TIER_MID),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)", TIER_HIGH),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)", TIER_LOW),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)", TIER_LOW),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)", TIER_LOW),
]

# (width, height, avail_width, avail_height, min_tier). Must stay in lockstep
# with kScreens in navigator_fingerprint.cc -- same order, same values.
#
# Every panel is 1080p or larger: sub-1080p resolutions belong to low-end
# machines from years ago and would not plausibly pair with the GPUs in the table
# above. The tier is the MINIMUM GPU tier allowed to drive that panel, not an
# exact match -- a 3080 Ti on a 1080p panel is a normal gaming setup.
SCREENS = [
    (1920, 1080, 1920, 1040, TIER_LOW),   # 1080p, most common panel by far
    (1920, 1200, 1920, 1160, TIER_LOW),   # 16:10 24" office monitor
    (2560, 1440, 2560, 1400, TIER_MID),   # 1440p
    (2560, 1600, 2560, 1560, TIER_MID),   # 1440p 16:10
    (2880, 1800, 2880, 1760, TIER_HIGH),  # 3K laptop panel
    (3440, 1440, 3440, 1400, TIER_HIGH),  # 21:9 ultrawide
    (3840, 2160, 3840, 2120, TIER_HIGH),  # 4K
]

# The flags that shape every launch. Kept as a module-level constant so the
# benchmark and concurrency tools measure the same configuration production uses
# -- hardcoding a copy in each tool caused real drift once: the concurrency
# harness kept a --renderer-process-limit that had been removed from the launcher
# because it broke the timezone field, so its numbers described a configuration
# nobody shipped.
BASELINE_FLAGS = [
    # Without --no-sandbox the renderer starts under the sandbox and ICU's default
    # timezone ends up latched from the host before LocalDOMWindow installs the
    # seeded one. Measured: --dump-dom reports Etc/GMT-8 (host) with sandboxing
    # and Asia/Shanghai (seeded) without it, same seed and same other flags.
    # fp_drift has always passed --no-sandbox, which is why its baseline never
    # showed this.
    "--no-sandbox",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-service-autorun",
    "--disable-background-networking",
    "--disable-component-extensions-with-background-pages",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-extensions",
    "--disable-component-update",
    "--in-process-gpu",
    "--disable-breakpad",
    # DNS prefetch resolves every link's hostname up front, and those lookups do
    # not travel through the SOCKS5 tunnel -- they hit the host resolver. Several
    # profiles sharing one resolver is a correlation signal, and closing it costs
    # nothing. (QUIC was checked separately: with a proxy configured, Chromium
    # never opened UDP:443, so it does not leak that way.)
    "--dns-prefetch-disable",
    # Multi-account RPA needs timers and rAF to keep running while a window sits
    # behind another. Measured with two windows, the occluded one without these
    # flags ran setInterval at 4 Hz (vs 62 focused) and rAF at 0 Hz; with them it
    # ran 62.7 Hz and 15 Hz. Minimising still kills rAF outright -- these flags do
    # not fix that, so windows must stay on screen.
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    # --webrtc-ip-handling-policy is the CHROME switch (chrome_switches.h ->
    # chrome_command_line_pref_store.cc -> prefs::kWebRTCIPHandlingPolicy ->
    # RendererPreferences). The content-layer --force-webrtc-ip-handling-policy
    # only works in content_shell and headless_shell; Chrome never reads it, so
    # the previous flag was inert and WebRTC leaked the real public IP on every
    # profile. Verified: with this switch ICE produces zero candidates.
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
]

from bluebean_paths import CHROME_EXE as DEFAULT_CHROME_PATH, PROFILE_ROOT as DEFAULT_PROFILE_ROOT_PATH, PROXY_CONFIG as DEFAULT_PROXY_CONFIG

DEFAULT_CHROME = str(DEFAULT_CHROME_PATH)
DEFAULT_PROFILE_ROOT = str(DEFAULT_PROFILE_ROOT_PATH)
SEED_VAR = "CHROME_FP_SEED"


def fnv1a(h: int, data: bytes) -> int:
    for b in data:
        h ^= b
        h = (h * FNV_PRIME) & MASK64
    return h


class Fingerprint:
    """Mirror of blink::NavigatorFingerprint."""

    def __init__(self, seed: str):
        self.seed = seed
        self.base = fnv1a(FNV_OFFSET, seed.encode("utf-8"))

    def derive(self, salt: str) -> int:
        h = fnv1a(self.base, b":")
        h = fnv1a(h, salt.encode("utf-8"))
        # splitmix64 finalizer. FNV-1a's low bits are non-uniform, and every
        # consumer reduces this modulo a small table size (7-10 entries), so
        # without this the draws collapse onto a few buckets.
        h ^= h >> 30
        h = (h * 0xBF58476D1CE4E5B9) & MASK64
        h ^= h >> 27
        h = (h * 0x94D049BB133111EB) & MASK64
        h ^= h >> 31
        return h

    @property
    def hardware_concurrency(self) -> int:
        _, _, tier = self.gpu
        table = CORES_BY_TIER[tier]
        return table[self.derive("hw") % len(table)]

    @property
    def device_memory(self) -> float:
        _, _, tier = self.gpu
        table = MEMORY_BY_TIER[tier]
        return table[self.derive("dm") % len(table)]

    @property
    def platform(self) -> str:
        return PLATFORMS[self.derive("pf") % len(PLATFORMS)]

    @property
    def _locale(self) -> tuple[str, list[str]]:
        return LOCALES[self.derive("lang") % len(LOCALES)]

    @property
    def languages(self) -> str:
        return self._locale[0]

    @property
    def timezone(self) -> str:
        """IANA id, drawn from the language's candidate list.

        Mirrors NavigatorFingerprint: the C++ side picks
        `timezones[Derive("tz") % candidates]` and then pushes it into ICU.
        """
        candidates = self._locale[1]
        return candidates[self.derive("tz") % len(candidates)]

    @property
    def ui_language(self) -> str:
        """Primary tag, for --lang. Chromium wants a single BCP-47 tag here."""
        return self.languages.split(",")[0]

    @property
    def gpu(self) -> tuple[str, str, int]:
        return GPU_PROFILES[self.derive("gpu") % len(GPU_PROFILES)]

    @property
    def screen(self) -> tuple[int, int, int, int, int]:
        """(width, height, avail_width, avail_height, tier).

        Tier comes from the GPU rather than being drawn independently. A panel is
        eligible when its min_tier is at or below the GPU tier, so better hardware
        unlocks more panels while low-end GPUs stay on the common ones.
        """
        _, _, tier = self.gpu
        candidates = [s for s in SCREENS if s[4] <= tier]
        return candidates[self.derive("scr") % len(candidates)]

    @property
    def window_size(self) -> tuple[int, int]:
        """Window size that fits inside the reported screen.

        A window larger than navigator.screen is the most obvious tell in this
        whole area, so this is derived from the screen rather than hardcoded.
        Dimensions are rounded down to even numbers -- odd window widths make the
        renderer report a fractional devicePixelRatio that no real setup has.
        """
        _, _, avail_w, avail_h, _ = self.screen
        w = max(800, (avail_w * 92 // 100) & ~1)
        h = max(600, (avail_h * 94 // 100) & ~1)
        return w, h

    def as_dict(self) -> dict:
        # NOTE: the screen's fifth field is its *minimum* GPU tier, not the
        # profile's actual tier. Reporting that as hardware_tier would describe
        # the panel requirement instead of the card.
        w, h, aw, ah, screen_min_tier = self.screen
        ww, wh = self.window_size
        vendor, renderer, gpu_tier = self.gpu
        return {
            "seed": self.seed,
            "seed_hash": f"{self.base:016x}",
            "webdriver": False,
            "platform": self.platform,
            "hardwareConcurrency": self.hardware_concurrency,
            "deviceMemory": self.device_memory,
            "languages": self.languages,
            "ui_language": self.ui_language,
            "timezone": self.timezone,
            "webgl_vendor": vendor,
            "webgl_renderer": renderer,
            "hardware_tier": ["low", "mid", "high"][gpu_tier],
            "screen_min_tier": ["low", "mid", "high"][screen_min_tier],
            "screen": f"{w}x{h}",
            "avail_screen": f"{aw}x{ah}",
            "window_size": f"{ww}x{wh}",
        }


def derive_seed(master_key: str, account_id: str) -> str:
    """Stable per-account seed.

    HMAC rather than a bare hash so that knowing one account's seed does not let
    you derive another's without the master key.
    """
    digest = hmac.new(
        master_key.encode("utf-8"), account_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest[:32]


def _clean_user_agent() -> str:
    """The UA a headed build sends, for use when running headless.

    Headless Chromium replaces the product token with "HeadlessChrome", so a
    headless launch is trivially identifiable no matter what else is masked. The
    version comes from the binary itself so it cannot drift from the build; only
    the major component is used, matching Chrome's UA-Reduction form.
    """
    version = "158.0.0.0"
    try:
        out = subprocess.run([DEFAULT_CHROME, "--version"], capture_output=True,
                             text=True, timeout=15, encoding="utf-8",
                             errors="replace", creationflags=no_window())
        m = re.search(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", out.stdout or "")
        if m:
            version = f"{m.group(1)}.0.0.0"
    except (subprocess.SubprocessError, OSError):
        pass
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version} Safari/537.36"
    )


def launch_argv(fp: "Fingerprint",
                extra: list[str] | None = None,
                headless: bool = False) -> list[str]:
    """The exact flags production uses, minus the binary path and profile path.

    Every measurement tool must go through this. Testing without --lang is what
    let a real inconsistency hide: ICU's default locale (which drives
    Intl.NumberFormat().locale, Intl.Collator().locale and the localised
    timezone name in Date.toTimeString()) follows --lang, not
    navigator.languages. A profile claiming ja-JP while formatting numbers as
    zh-CN is trivially detectable, and earlier probe runs never noticed because
    they omitted the flag the launcher always passes.
    """
    w, h = fp.window_size
    args = [
        *BASELINE_FLAGS,
        f"--lang={fp.ui_language}",
        f"--window-size={w},{h}",
    ]
    if headless:
        # Headless rewrites the product token to "HeadlessChrome". Override the
        # UA here rather than at each call site so no measurement tool can
        # accidentally measure the marked-up string.
        args += ["--headless=new", f"--user-agent={_clean_user_agent()}"]
    args += extra or []
    return args


RELAY_STATE = ".fp_relay.json"

# PID of the relay this process started, so cmd_launch can stop it once Chrome
# exits. Relays are reusable across launches, but nothing closed them when a
# non-detached run finished, and a batch of test launches left a dozen behind.
_RELAY_PID = 0


def _relay_port_alive(port: int, scheme: str = "socks5") -> bool:
    """True when something is listening on the relay's port."""
    import socket as _socket
    try:
        # 1.5 s was the old value; on Windows a closed loopback port waits for the
        # full timeout instead of failing fast (measured 1503 ms, WSAEWOULDBLOCK),
        # so every launch paid it in full whenever a stale state file was present.
        with _socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def ensure_relay(account: str, profile_dir: Path, config_path: str) -> str:
    """Return a proxy URL for the local relay, starting it when needed.

    The relay has to outlive this process: --detach returns immediately while
    Chrome keeps running. So it runs as its own detached child, publishes its port
    in a state file inside the profile, and can be reused on the next launch of the
    same account instead of being restarted every time.
    """
    import json as _json
    import subprocess as _sp
    import sys as _sys
    import time as _time

    state_path = Path(profile_dir) / RELAY_STATE
    if state_path.exists():
        try:
            info = _json.loads(state_path.read_text(encoding="utf-8"))
            if info.get("proxy_url") and _relay_port_alive(int(info["port"])):
                return info["proxy_url"]
        except (ValueError, OSError, KeyError):
            pass
        try:
            state_path.unlink()
        except OSError:
            pass

    port_file = Path(profile_dir) / ".fp_relay.port"
    try:
        port_file.unlink()
    except OSError:
        pass

    script = Path(__file__).resolve().parent / "fp_proxy.py"
    cmd = [_sys.executable, str(script), "--config", config_path,
           "serve", "--account", account, "--port-file", str(port_file)]
    creation = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: survive this process exit.
        creation = 0x00000008 | 0x00000200
    _sp.Popen(cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
              creationflags=creation, close_fds=True,
              env={**os.environ})

    deadline = _time.time() + 20
    while _time.time() < deadline:
        if port_file.exists():
            try:
                info = _json.loads(port_file.read_text(encoding="utf-8"))
                if info.get("proxy_url"):
                    state_path.write_text(_json.dumps(info), encoding="utf-8")
                    global _RELAY_PID
                    _RELAY_PID = int(info.get("pid") or 0)
                    return info["proxy_url"]
            except (ValueError, OSError):
                pass
        _time.sleep(0.25)
    raise SystemExit(
        "relay did not become ready within 20s; check fp_proxy.py serve output")


def resolve_proxy(args, profile_dir: Path, dry_run: bool = False):
    """Return (chrome_flags, description) for the account's configured exit.

    `--proxy-account` reads the JSON config so an account's exit is a property of
    the account. `--proxy` stays supported for one-off runs.
    """
    account = getattr(args, "proxy_account", "") or ""
    raw = getattr(args, "proxy", "") or ""
    if not account and not raw:
        # No proxy selected: match the historical behaviour and add nothing, which
        # means Chrome uses the system proxy. --proxy-mode direct overrides that.
        mode = getattr(args, "proxy_mode", "") or ""
        if mode == "direct":
            return ["--no-proxy-server"], "direct (OS proxy bypassed)"
        if mode == "system":
            return [], "system proxy (follow OS settings)"
        if mode == "local":
            port = int(getattr(args, "local_port", 0) or 0)
            scheme = getattr(args, "local_scheme", "") or "socks5"
            if not (0 < port < 65536):
                raise SystemExit(
                    "--proxy-mode local needs --local-port (e.g. 7890)")
            return ([f"--proxy-server={scheme}://127.0.0.1:{port}"],
                    f"local {scheme}://127.0.0.1:{port}")
        return [], ""

    if account:
        from fp_proxy import ProxyConfig
        cfg = ProxyConfig.load(getattr(args, "proxy_config", "")
                               or str(DEFAULT_PROXY_CONFIG))
        spec = cfg.for_account(account)
        if spec.needs_relay:
            if dry_run:
                # --dry-run must not start a process or write a state file. Report
                # what would happen instead.
                return ([], f"{spec.mode} via local relay -> {spec.host}:{spec.port}"
                             " (relay not started: dry run)")
            url = ensure_relay(account, profile_dir,
                               getattr(args, "proxy_config", "")
                               or str(DEFAULT_PROXY_CONFIG))
            return ([f"--proxy-server={url}"],
                    f"{spec.mode} via local relay -> {spec.host}:{spec.port}")
        return spec.chrome_flags(), spec.redacted()

    # --proxy with an inline URL: strip credentials, they cannot be expressed.
    from fp_proxy import ProxySpec
    if "@" in raw:
        raise SystemExit(
            "--proxy cannot carry credentials (Chrome ignores them). "
            "Configure the account instead:\n"
            "  python fp_proxy.py set --account <name> --mode <scheme> "
            "--host <h> --port <p> --username <u> --password <p>\n"
            "then pass --proxy-account <name>")
    spec = ProxySpec(mode=raw.split("://", 1)[0] if "://" in raw else "http",
                     host=raw.split("://")[-1].rsplit(":", 1)[0],
                     port=int(raw.rsplit(":", 1)[-1]) if ":" in raw else 0)
    return [f"--proxy-server={raw}"], spec.redacted()


def build_argv(args, fp: Fingerprint, profile_dir: Path) -> list[str]:
    window_w, window_h = fp.window_size
    argv = [
        args.chrome,
        f"--user-data-dir={profile_dir}",
        *BASELINE_FLAGS,
        # Pin the application locale. This is what network_context_params
        # .accept_language is set from, so it keeps the HTTP header aligned with
        # the JS-side language derived from the seed.
        f"--lang={fp.ui_language}",
        # Window must fit inside the screen the C++ side reports, or the very
        # first thing a page can check -- window.outerWidth vs screen.width --
        # contradicts itself.
        f"--window-size={window_w},{window_h}",
        "--window-position=0,0",
        # --webrtc-ip-handling-policy already lives in BASELINE_FLAGS; adding it
        # again here only duplicated the switch on the command line.
    ]

    proxy_flags, proxy_desc = resolve_proxy(
        args, profile_dir, dry_run=getattr(args, "dry_run", False))
    argv.extend(proxy_flags)

    if args.headless:
        # build_argv assembles its own list (launch_argv is for tools), so the
        # headless handling has to be duplicated here. Same reasoning: headless
        # rewrites the product token to "HeadlessChrome".
        argv.append("--headless=new")
        argv.append(f"--user-agent={_clean_user_agent()}")

    if args.extra_arg:
        argv.extend(args.extra_arg)

    if not args.no_url:
        argv.append(args.url)

    return argv


def prepare_profile(profile_dir: Path, fp: Fingerprint) -> None:
    """Seed the on-disk prefs so the very first run already looks right.

    Chromium writes its own Preferences on first run; pre-creating the intl node
    avoids a window where a page could observe a mismatch between the JS
    language and the profile's selected_languages.
    """
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    pref_path = default_dir / "Preferences"

    data = {}
    if pref_path.exists():
        try:
            data = json.loads(pref_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}

    intl = data.setdefault("intl", {})
    intl["selected_languages"] = fp.languages
    intl.setdefault("accept_languages", fp.languages)

    pref_path.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )


# Cache directory for proxy geo lookups. One lookup per proxy endpoint per day is
# enough -- the answer does not change, and proxy traffic is metered.
GEO_CACHE_TTL_SECONDS = 24 * 3600


def _curl_path() -> str | None:
    """Locate the real curl binary. On Windows `curl` may be a PowerShell alias."""
    if os.name == "nt":
        candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                 "System32", "curl.exe")
        if os.path.exists(candidate):
            return candidate
    return shutil.which("curl")


def lookup_proxy_geo(proxy: str, cache_dir: Path, force: bool = False,
                     cache_key: str | None = None) -> dict | None:
    """Ask an IP geolocation service what timezone the proxy's exit node is in.

    Returns a dict with `query` (IP), `timezone` (IANA id), `country` (English
    display name, e.g. "United States") and `countryCode` (two-letter code,
    e.g. "US"), or None on failure. Callers that compare against a set of
    countries must use `countryCode` -- the display name never matches a code
    table. Failures are not cached, so a transient network error does not poison
    the next run.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Key the cache on the proxy endpoint only; credentials must not land on disk.
    # cache_key overrides that when the caller reaches the upstream through a local
    # relay: the relay port changes every launch, so keying on it would defeat the
    # cache entirely.
    endpoint = cache_key or proxy.split("@")[-1]
    safe = "".join(c if c.isalnum() else "_" for c in endpoint)
    cache_file = cache_dir / f"geo_{safe}.json"

    if not force and cache_file.exists():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - payload.get("_cached_at", 0) < GEO_CACHE_TTL_SECONDS:
                return payload
        except (json.JSONDecodeError, OSError):
            pass

    curl = _curl_path()
    if not curl:
        return None

    # Plain HTTP on purpose: ip-api's free tier is HTTP-only, the response is
    # public data, and this saves a TLS handshake through a metered proxy.
    url = (
        "http://ip-api.com/json/"
        "?fields=status,country,countryCode,timezone,query"
    )
    cmd = [curl, "-s", "--max-time", "10", "--proxy", proxy, url]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace", creationflags=no_window())
    except (subprocess.SubprocessError, OSError):
        return None

    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    if data.get("status") != "success" or not data.get("timezone"):
        return None

    data["_cached_at"] = time.time()
    try:
        cache_file.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return data


def tz_offset_hours(tz_name: str) -> float | None:
    """Current UTC offset of an IANA zone, in hours. None if unknown."""
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
    except ImportError:
        return None
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        return None
    return now.utcoffset().total_seconds() / 3600.0


# Countries whose browsers plausibly run a given UI language.
#
# Deliberately generous: a language being spoken somewhere does not require the
# speaker to be a national, and expat populations are large. The table exists to
# catch obvious contradictions (Japanese on a Brazilian exit), not to enforce
# nationality. English is absent on purpose -- it is the default second language
# almost everywhere, so constraining en-* would produce constant false alarms.
LANG_COUNTRY_HINTS = {
    "zh-CN": {"CN", "SG", "MY", "HK", "MO", "US", "CA", "AU", "GB", "JP"},
    "zh-TW": {"TW", "HK", "MO", "US", "CA", "AU", "GB", "JP"},
    "ja-JP": {"JP", "US", "CA", "AU", "GB", "SG", "TH", "PH"},
    "ko-KR": {"KR", "US", "CA", "AU", "GB", "JP", "CN"},
}

# Language-country pairs extreme enough to refuse outright.
#
# Kept deliberately narrow. Real diasporas make most "implausible" pairings
# actually possible: Brazil has roughly 2M people of Japanese descent and 300k of
# Chinese descent, so ja-JP on a Brazilian exit is uncommon but real, and
# refusing it would block legitimate accounts. Only pairings with both strong
# linguistic separation and negligible migration are listed here; everything else
# falls through to a warning so the operator decides.
LANG_COUNTRY_HARD_BLOCK = {
    "ja-JP": {"NG", "EG", "SA", "IR", "PK", "BD"},
    "ko-KR": {"NG", "EG", "SA", "IR", "PK", "BD"},
    "zh-CN": {"NG", "EG", "SA", "IR"},
    "zh-TW": {"NG", "EG", "SA", "IR"},
}


def check_language_country(fp: "Fingerprint", country: str | None) -> tuple[bool, bool, str]:
    """(ok, is_hard_violation, message) for the language/country pairing.

    `ok=False, hard=False` means suspicious but tolerable -- the caller warns.
    `ok=False, hard=True` means the pairing is implausible enough to refuse.
    """
    if not country:
        return True, False, "no exit country available"

    primary = fp.languages.split(",")[0]
    base = primary.split("-")[0]
    key = primary if primary in LANG_COUNTRY_HINTS else None

    if key is None:
        # English and unconstrained languages: nothing to assert.
        return True, False, f"language {primary} places no country constraint"

    blocked = LANG_COUNTRY_HARD_BLOCK.get(key, set())
    if country in blocked:
        return False, True, (
            f"language {primary} with exit in {country} is implausible "
            f"-- this pairing is a strong signal")

    allowed = LANG_COUNTRY_HINTS[key]
    if country in allowed:
        return True, False, f"language {primary} consistent with exit {country}"

    return False, False, (
        f"language {primary} but exit in {country} "
        f"(usual: {', '.join(sorted(allowed))})")


def verify_proxy(fp: "Fingerprint", proxy: str, cache_dir: Path,
                 force: bool = False,
                  cache_key: str | None = None) -> tuple[bool, str]:
    """Compare the proxy exit location against the profile's derived timezone.

    Returns (ok, message). A mismatch beyond MAX_TZ_DRIFT_HOURS means the JS side
    claims one continent while the traffic leaves from another -- one of the
    cheapest and most reliable signals a platform can use to link accounts.
    """
    MAX_TZ_DRIFT_HOURS = 4.0

    geo = lookup_proxy_geo(proxy, cache_dir, force=force, cache_key=cache_key)
    if not geo:
        return True, "proxy geo lookup unavailable; skipping check"

    proxy_tz = geo["timezone"]
    want = fp.timezone
    drift = None
    a, b = tz_offset_hours(proxy_tz), tz_offset_hours(want)
    if a is not None and b is not None:
        drift = abs(a - b)

    if proxy_tz == want:
        return True, (
            f"proxy exit {geo.get('query')} ({geo.get('country')}) "
            f"timezone {proxy_tz} matches profile"
        )
    if drift is not None and drift <= MAX_TZ_DRIFT_HOURS:
        return True, (
            f"proxy timezone {proxy_tz} vs profile {want}: "
            f"{drift:.0f}h apart, within tolerance"
        )
    detail = (
        f"proxy exit {geo.get('query')} ({geo.get('country')}) is in {proxy_tz} "
        f"but the profile claims {want}"
    )
    if drift is not None:
        detail += f" ({drift:.0f}h apart)"
    return False, detail


def verify_language_country(fp: "Fingerprint", proxy: str, cache_dir: Path,
                            force: bool = False,
                          cache_key: str | None = None) -> tuple[bool, str]:
    """Second gate: the claimed language must not contradict the exit country.

    Runs independently of the timezone gate because the two catch different
    mistakes. A ja-JP profile on a Sao Paulo exit is only 12h from Tokyo and can
    slip past a 4-hour timezone tolerance, yet nobody in Brazil browses with a
    Japanese UI.
    """
    geo = lookup_proxy_geo(proxy, cache_dir, force=force, cache_key=cache_key)
    if not geo:
        return True, "proxy geo lookup unavailable; skipping language check"
    # countryCode, not country: LANG_COUNTRY_HINTS holds two-letter codes, while
    # geo["country"] is the English display name ("United States"). Passing the
    # display name made every membership test fail, so the gate reported a soft
    # conflict for every non-English profile -- including correct pairings. The
    # bug hid behind en-US, which short-circuits the check entirely.
    code = geo.get("countryCode")
    ok, hard, message = check_language_country(fp, code)
    if ok:
        return True, message
    # Show the display name in the warning; operators read names, not codes.
    display = geo.get("country") or code
    message = message.replace(str(code), str(display)) if code else message
    return (not hard), ("REFUSE: " if hard else "warn: ") + message


def cmd_check_proxy(args) -> int:
    """Verify that a proxy's exit location matches what a profile claims."""
    seed = derive_seed(args.master_key, args.account)
    fp = Fingerprint(seed)
    cache_dir = Path(args.profile_root).parent / ".geo-cache"
    ok, message = verify_proxy(fp, args.proxy, cache_dir, force=args.force)
    print(f"account : {args.account}")
    print(f"profile : {fp.timezone}  ({fp.languages})")
    print(f"proxy   : {args.proxy.split('@')[-1]}")
    print(f"verdict : {'PASS' if ok else 'FAIL'} -- {message}")
    return 0 if ok else 2


def cmd_dist(args) -> int:
    """Report how a batch of accounts spreads across the derived dimensions.

    Small batches cluster by nature -- 20 draws into 8 GPU buckets will often
    put 6 or 7 in one bucket, which looks like a bug but is just variance. This
    command makes that visible before an operator commits to a batch, so they can
    rotate the master key if a particular run happens to clump.
    """
    accounts = []
    for i in range(1, args.count + 1):
        account = f"{args.account_prefix}{i:03d}"
        fp = Fingerprint(derive_seed(args.master_key, account))
        accounts.append((account, fp))

    def tally(label: str, values: list[str]) -> None:
        counts = collections.Counter(values)
        n = len(values)
        print(f"\n{label}  ({len(counts)} distinct / {n} accounts)")
        for value, count in counts.most_common():
            bar = "#" * max(1, round(40 * count / n))
            print(f"  {value:<26} {count:>4}  {100*count/n:5.1f}%  {bar}")
        # max/min is a bad gauge here: with 20 draws into 8 buckets, 6-vs-1 is
        # ordinary variance, not a bias. What actually matters is whether one
        # value dominates -- a single value holding more than half of a batch is
        # worth acting on; 2-3x spread over 20 samples is not.
        share = max(counts.values()) / n
        verdict = "ok" if share <= 0.5 else "DOMINANT -- consider rotating key"
        print(f"  top share = {100*share:.0f}%  {verdict}")

    tally("GPU tier", [fp.as_dict()["hardware_tier"] for _, fp in accounts])
    tally("GPU model", [fp.gpu[1].split(",")[1].strip()[:24] for _, fp in accounts])
    tally("screen", [fp.as_dict()["screen"] for _, fp in accounts])
    tally("hardwareConcurrency", [str(fp.hardware_concurrency) for _, fp in accounts])
    tally("languages", [fp.languages for _, fp in accounts])
    tally("timezone", [fp.as_dict().get("timezone", "?") for _, fp in accounts])

    # Exact-duplicate profiles: two accounts sharing every dimension is a real
    # problem (they are linkable), unlike two accounts sharing one field.
    # Include every discrete dimension the C++ side controls, not just the
    # obvious ones. With only tier+screen+hw+dm+lang the space is 4704 and a
    # 100-account batch collides by the birthday paradox about 65% of the time --
    # a false alarm. Adding the full GPU string and the timezone raises the space
    # to ~75k, which is what the operator actually cares about.
    combos = collections.Counter()
    for _, fp in accounts:
        d = fp.as_dict()
        combos[
            (
                d["webgl_renderer"],
                d["screen"],
                d["hardwareConcurrency"],
                d["deviceMemory"],
                d["languages"],
                d["timezone"],
            )
        ] += 1
    dupes = {k: v for k, v in combos.items() if v > 1}
    linkable = sum(count for count in dupes.values())
    # The tiers are not independent: a GPU fixes the tier, which bounds the
    # eligible screens, core counts and memory sizes. Summing per GPU gives the
    # real space instead of the earlier flat product (which also referenced a
    # constant that no longer exists).
    discrete = 0
    for _vendor, _renderer, tier in GPU_PROFILES:
        eligible_screens = sum(1 for s in SCREENS if s[4] <= tier)
        discrete += (
            eligible_screens
            * len(CORES_BY_TIER[tier])
            * len(MEMORY_BY_TIER[tier])
        )
    space = discrete * len(LOCALES) * max(len(c) for _, c in LOCALES)
    print(
        f"\ndiscrete combination space: {space:,} "
        f"(GPU x screen x hw x dm x locale x tz)"
    )
    print(f"fully-identical profiles: {len(dupes)} pair(s), {linkable} account(s)")
    for combo, count in dupes.items():
        gpu = combo[0].split(",")[1].strip()[:32] if "," in combo[0] else combo[0]
        print(f"  x{count}  {gpu} | {combo[1]} | hw={combo[2]} dm={combo[3]} "
              f"| {combo[4]} | {combo[5]}")
    if linkable == 0:
        print("  -> every account is distinguishable. Nothing to do.")
    else:
        print(
            "  -> the accounts listed above match on every discrete dimension.\n"
            "     Canvas/WebGL/Audio hashes still differ between them, but those\n"
            "     are the cheapest fields for a detector to cluster. Rename one of\n"
            "     the colliding accounts or rotate the master key."
        )
    return 0


def cmd_show(args) -> int:
    seed = derive_seed(args.master_key, args.account)
    fp = Fingerprint(seed)
    print(json.dumps(fp.as_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_list(args) -> int:
    root = Path(args.profile_root)
    if not root.exists():
        print(f"no profiles under {root}")
        return 0
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        print(f"{d.name:<40} {size / 1024 / 1024:>9.1f} MB")
    return 0


def cmd_launch(args) -> int:
    chrome = Path(args.chrome)
    # Dry-run is meant to work before anything is built, so only insist on the
    # binary when we are actually going to exec it.
    if not args.dry_run and not chrome.exists():
        print(f"chrome not found: {chrome}", file=sys.stderr)
        print("hint: build first, or pass --chrome", file=sys.stderr)
        return 2

    seed = derive_seed(args.master_key, args.account)
    fp = Fingerprint(seed)

    profile_dir = Path(args.profile_root) / args.account
    # --dry-run prints the plan; it must not leave anything behind. Creating the
    # directory and seeding Preferences here meant a dry run produced
    # profile_root/<account>/Default/Preferences, so inspecting flags dirtied the
    # tree -- and a scripted "show me what would launch" sweep would litter one
    # directory per account.
    if not args.dry_run:
        profile_dir.mkdir(parents=True, exist_ok=True)
        prepare_profile(profile_dir, fp)

    # Fail closed: an account whose JS says Asia/Tokyo but whose traffic leaves
    # from Europe is trivially linkable, so refuse to launch by default.
    #
    # This used to key off args.proxy, which meant every account configured through
    # --proxy-account -- local, socks5, http, https -- skipped both gates entirely.
    # Resolve the exit first, then decide whether there is anything to verify.
    gate_proxy = ""
    gate_cache_key = None
    if not args.dry_run:
        # dry_run=False here: a relay account needs its listener up before curl can
        # probe through it. ensure_relay reuses an existing relay for this profile.
        gate_flags, _gate_desc = resolve_proxy(args, profile_dir, dry_run=False)
        for _f in gate_flags:
            if _f.startswith("--proxy-server="):
                gate_proxy = _f.split("=", 1)[1]
        if args.proxy and not gate_proxy:
            gate_proxy = args.proxy

        # The geo answer describes the upstream, not our relay, so key the cache on
        # the upstream endpoint. Without this the cache never hit under
        # --proxy-account, because the relay port differs on every launch.
        if args.proxy_account:
            try:
                from fp_proxy import ProxyConfig as _PC
                _cfg = _PC.load(args.proxy_config or
                                str(DEFAULT_PROXY_CONFIG))
                _spec = _cfg.accounts.get(args.proxy_account)
                if _spec is not None and _spec.mode in ("socks5", "http", "https"):
                    gate_cache_key = f"{_spec.mode}://{_spec.host}:{_spec.port}"
            except SystemExit:
                gate_cache_key = None

    if args.verify_proxy and gate_proxy and not args.dry_run:
        cache_dir = Path(args.profile_root).parent / ".geo-cache"
        ok, message = verify_proxy(fp, gate_proxy, cache_dir,
                                   force=args.force_geo,
                                   cache_key=gate_cache_key)
        print(f"[proxy] {message}")
        if not ok:
            print("[proxy] refusing to launch; pass --no-verify-proxy to override",
                  file=sys.stderr)
            return 3

        # Second gate, independent of the timezone one: a ja-JP profile behind a
        # Sao Paulo exit can sit inside the 4-hour tolerance yet is implausible
        # on its face. Warnings print and continue; only hard contradictions
        # refuse.
        lang_ok, lang_msg = verify_language_country(
            fp, gate_proxy, cache_dir, force=args.force_geo,
            cache_key=gate_cache_key)
        print(f"[lang]  {lang_msg}")
        if not lang_ok:
            print("[lang]  refusing to launch; the language/exit pairing is a "
                  "strong association signal", file=sys.stderr)
            return 6

    # Profile-ownership guard.
    #
    # A marker inside the profile directory, not in the side-car .identity store,
    # because the dangerous workflow is *copying a warmed profile* -- the copy
    # carries the marker with it while the side-car does not. Verified by
    # fp_resilience.py T5: a copied profile launched with a new seed reports a
    # new device fingerprint while still carrying the old session data, which is
    # precisely the pairing that links two accounts.
    owner_marker = profile_dir / ".fp_owner"
    seed_tag = f"{fp.base:016x}"
    if owner_marker.exists():
        try:
            previous = owner_marker.read_text(encoding="utf-8").strip()
        except OSError:
            previous = ""
        if previous and previous != seed_tag:
            print(
                f"[profile] REFUSING TO LAUNCH: this profile was created by a "
                f"different seed (owner {previous[:8]}…, current {seed_tag[:8]}…).",
                file=sys.stderr,
            )
            print(
                "          Its cookies and local storage belong to another "
                "account. Copying a warmed profile and switching seeds yields a"
                "\n          new fingerprint on old session data -- the exact "
                "combination platforms use to link accounts.",
                file=sys.stderr,
            )
            print(
                "          Start from an empty directory, or pass "
                "--adopt-profile to take ownership deliberately.",
                file=sys.stderr,
            )
            if not args.adopt_profile:
                return 5
            print("[profile] --adopt-profile given; overwriting the marker.")
    if not args.dry_run:
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
            owner_marker.write_text(seed_tag, encoding="utf-8")
        except OSError:
            # A read-only or exotic filesystem must not block launching.
            pass

    # Identity gate: an account is a machine, not a seed. If the seed changed,
    # the old device is gone as far as any observer is concerned -- refuse rather
    # than silently continue with a profile that no longer matches its history.
    # First launch records the identity so later launches have something to
    # compare against.
    try:
        # --dry-run only prints the plan; gating it would make debugging a
        # mismatched identity impossible without editing files first.
        if args.skip_identity or args.dry_run:
            raise ImportError("skipped")
        from fp_identity import (
            build_identity as _build_identity,
            compare as _compare_identity,
            load_identity as _load_identity,
            save_identity as _save_identity,
        )

        recorded = _load_identity(args.profile_root, args.account)
        geo_for_identity = None
        if args.proxy:
            geo_for_identity = lookup_proxy_geo(
                args.proxy, Path(args.profile_root).parent / ".geo-cache"
            )
        current = _build_identity(args.account, seed, fp, geo_for_identity)

        if args.adopt_profile and recorded is not None:
            # --adopt-profile means "I am deliberately taking this profile over".
            # That has to cover the identity record too, otherwise the user is
            # told the profile was adopted and then refused by the identity gate
            # a few lines later, and has to delete a JSON file by hand.
            print(f"[identity] --adopt-profile: re-baselining {args.account} "
                  f"on the current seed (previous seed discarded)")
            recorded = None

        if recorded is None:
            _save_identity(args.profile_root, args.account, current)
            print(f"[identity] recorded first-seen identity for {args.account} "
                  f"(tz={fp.timezone})")
        else:
            verdict, findings = _compare_identity(recorded, current)
            if verdict == "fatal":
                print("[identity] REFUSING TO LAUNCH", file=sys.stderr)
                for finding in findings:
                    print(f"  {finding}", file=sys.stderr)
                print(
                    "  If the seed change is intentional, delete the identity "
                    "record first and treat the account as newly created.",
                    file=sys.stderr,
                )
                return 4
            if verdict == "warn":
                print("[identity] WARNING -- profile diverged from history:")
                for finding in findings:
                    print(f"  {finding}")
            else:
                print(f"[identity] {args.account} matches its recorded history")
    except ImportError:
        # fp_identity is optional; missing it must not block launching.
        pass

    argv = build_argv(args, fp, profile_dir)

    env = os.environ.copy()
    env[SEED_VAR] = seed
    # Do not let a stray automation flag flip webdriver on. The C++ patch would
    # override it anyway, but a clean launch is easier to reason about.
    env.pop("CHROME_HEADLESS", None)

    if args.dry_run:
        print("env:")
        print(f"  {SEED_VAR}={seed}")
        print("fingerprint:")
        print(json.dumps(fp.as_dict(), indent=2, ensure_ascii=False))
        print("argv:")
        for a in argv:
            print(f"  {a}")
        return 0

    print(f"[fp] account={args.account} seed={seed[:8]}閳?"
          f"hw={fp.hardware_concurrency} lang={fp.languages}")

    # On Windows, Popen without an explicit stdout loses the child's output:
    # measured with --dump-dom, `Popen(argv)` produced nothing while
    # `Popen(argv, stdout=sys.stdout)` and `capture_output=True` both worked.
    # Hand the real stdout/stderr handles over so --extra-arg=--dump-dom is usable
    # from this CLI at all. sys.stdout can be None under pythonw, hence the guard.
    io_kwargs = {}
    if sys.stdout is not None:
        io_kwargs["stdout"] = sys.stdout
    if sys.stderr is not None:
        io_kwargs["stderr"] = sys.stderr
    proc = subprocess.Popen(argv, env=env, **io_kwargs, creationflags=no_window())
    if args.detach:
        # Chrome outlives this process, so the relay must too.
        print(f"[fp] detached pid={proc.pid}")
        return 0

    rc = proc.wait()
    # Not detached: Chrome is gone, so nothing needs the relay any more. Leaving it
    # running accumulates one process per launch until the TTL expires.
    if _RELAY_PID:
        try:
            os.kill(_RELAY_PID, signal.SIGTERM)
        except OSError:
            pass
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Launch an isolated Chromium profile with a deterministic fingerprint",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--chrome", default=DEFAULT_CHROME)
    ap.add_argument("--profile-root", default=DEFAULT_PROFILE_ROOT)
    ap.add_argument(
        "--master-key",
        default=os.environ.get("FP_MASTER_KEY", ""),
        help="HMAC key; defaults to $FP_MASTER_KEY",
    )
    ap.add_argument("--account", help="stable account identifier")
    ap.add_argument("--proxy", default="",
                    help="one-off proxy URL; credentials are NOT supported here")
    ap.add_argument("--proxy-account", default="",
                    help="read the exit configuration for this account from the "
                         "proxy config file")
    ap.add_argument("--proxy-config", default="",
                    help="path to proxies.json (default: "
                         "the per-user proxies.json file)")
    ap.add_argument("--proxy-mode", default="",
                    choices=["", "direct", "system", "local"],
                    help="when no account is given: direct / follow OS settings / "
                         "a proxy on this machine at --local-port")
    ap.add_argument("--local-port", type=int, default=0,
                    help="port for --proxy-mode local (e.g. 7890)")
    ap.add_argument("--local-scheme", default="socks5", choices=["socks5", "http"],
                    help="protocol for --proxy-mode local (default socks5)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--url", default="about:blank")
    ap.add_argument("--no-url", action="store_true")
    ap.add_argument("--extra-arg", action="append", default=[])
    ap.add_argument(
        "--adopt-profile",
        action="store_true",
        help="take ownership of a profile created by a different seed",
    )
    ap.add_argument(
        "--skip-identity",
        action="store_true",
        help="do not compare against the recorded identity (first-use / recovery)",
    )
    ap.add_argument(
        "--verify-proxy",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="check the proxy's exit timezone against the profile (default: on)",
    )
    ap.add_argument(
        "--force-geo",
        action="store_true",
        help="ignore the cached proxy geolocation",
    )
    ap.add_argument("--dry-run", action="store_true", help="print args and fingerprint, do not launch")
    ap.add_argument("--detach", action="store_true", help="do not wait for chrome to exit")

    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("show", help="print the fingerprint for an account")
    sub.add_parser("list", help="list existing profiles")
    sub.add_parser("launch", help="launch chrome (default)")
    check_p = sub.add_parser(
        "check-proxy",
        help="verify a proxy's exit timezone matches the profile's",
    )
    check_p.add_argument("--account", required=True)
    check_p.add_argument("--proxy", required=True, help="e.g. socks5://host:port")
    check_p.add_argument("--master-key",
                         default=os.environ.get("FP_MASTER_KEY", ""))
    check_p.add_argument("--profile-root", default=DEFAULT_PROFILE_ROOT)
    check_p.add_argument("--force", action="store_true",
                         help="ignore the cached geo lookup")

    dist_p = sub.add_parser(
        "dist", help="check how a batch of accounts spreads out"
    )
    dist_p.add_argument(
        "--account-prefix",
        default="acct-",
        help="accounts are named <prefix>001 .. <prefix><count> (default: acct-)",
    )
    dist_p.add_argument(
        "--count", type=int, default=50, help="how many accounts to examine"
    )
    dist_p.add_argument(
        "--master-key",
        default=os.environ.get("FP_MASTER_KEY", ""),
        help="HMAC key; defaults to $FP_MASTER_KEY",
    )

    args = ap.parse_args()

    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "check-proxy":
        if not args.master_key:
            ap.error("--master-key (or $FP_MASTER_KEY) is required")
        return cmd_check_proxy(args)
    if args.cmd == "dist":
        # `dist` carries its own --master-key so it can be written after the
        # subcommand, which is where people naturally put it.
        merged = argparse.Namespace(
            master_key=args.master_key or os.environ.get("FP_MASTER_KEY", ""),
            account_prefix=args.account_prefix,
            count=args.count,
        )
        if not merged.master_key:
            ap.error("--master-key (or $FP_MASTER_KEY) is required")
        return cmd_dist(merged)
    if not args.account:
        ap.error("--account is required")
    if not args.master_key:
        ap.error("--master-key (or $FP_MASTER_KEY) is required")
    if args.cmd == "show":
        return cmd_show(args)
    return cmd_launch(args)


if __name__ == "__main__":
    sys.exit(main())
