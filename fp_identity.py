#!/usr/bin/env python3
"""Track each account's identity over time and flag risky changes.

The open question this answers: when a proxy changes, should the fingerprint
follow it, or should the account keep the fingerprint it was born with?

Reality check first. A real person does not move between continents weekly. If an
account's exit IP jumps from Tokyo to New York in one step, no fingerprint choice
makes that look natural:

  * keeping the old timezone  -> "same machine, IP suddenly on another continent"
                                 reads as VPN use, which is itself a flag
  * changing the timezone too -> "the user teleported", which is worse, because
                                 it also means the device changed

So the tool does not try to make migration safe. It records what an account
looks like on first use, then reports precisely how much each subsequent launch
diverges, so the operator sees the risk before it accumulates detection history.

Risk model:

  seed changed        -> device changed. Fatal; must never happen to a live account.
  exit tz moved > 4h  -> cross-continent move. High risk on a warmed account.
  exit tz moved <= 4h -> neighbouring zone. Acceptable, common for travellers.
  country changed     -> same risk tier as the timezone move it implies.

Usage:
    python fp_identity.py record  --account acct-001 --proxy socks5://...
    python fp_identity.py check   --account acct-001 --proxy socks5://...
    python fp_identity.py list
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fp_launcher import (  # noqa: E402
    DEFAULT_PROFILE_ROOT,
    Fingerprint,
    derive_seed,
    lookup_proxy_geo,
    tz_offset_hours,
)

IDENTITY_DIRNAME = ".identity"
MAX_TZ_DRIFT_HOURS = 4.0


def identity_dir(profile_root: str) -> Path:
    """Kept beside the profiles rather than inside them: Chromium owns the
    profile directory and may rewrite or clean unknown files in it."""
    return Path(profile_root) / IDENTITY_DIRNAME


def identity_path(profile_root: str, account: str) -> Path:
    return identity_dir(profile_root) / f"{account}.json"


def load_identity(profile_root: str, account: str) -> dict | None:
    path = identity_path(profile_root, account)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_identity(profile_root: str, account: str, data: dict) -> None:
    d = identity_dir(profile_root)
    d.mkdir(parents=True, exist_ok=True)
    identity_path(profile_root, account).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def build_identity(account: str, seed: str, fp: Fingerprint, geo: dict | None) -> dict:
    return {
        "account": account,
        "seed": seed,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        # The fields that define "which machine is this". Anything here changing
        # means the account is being re-created, not continued.
        "fingerprint": {
            "platform": fp.platform,
            "hardwareConcurrency": fp.hardware_concurrency,
            "deviceMemory": fp.device_memory,
            "languages": fp.languages,
            "timezone": fp.timezone,
            "screen": fp.as_dict()["screen"],
            "hardware_tier": fp.as_dict()["hardware_tier"],
            "webgl_renderer": fp.gpu[1],
        },
        "exit": {
            "ip": geo.get("query") if geo else None,
            # Compare on the two-letter code; the display name is for humans and can
        # change between geo provider updates ("United States" vs "USA"), which
        # would otherwise look like the exit moved.
        "country": geo.get("country") if geo else None,
        "country_code": geo.get("countryCode") if geo else None,
            "timezone": geo.get("timezone") if geo else None,
        },
    }


def compare(recorded: dict, current: dict) -> tuple[str, list[str]]:
    """Returns (verdict, findings). Verdict is ok / warn / fatal."""
    findings = []
    verdict = "ok"

    # --- seed: the single most important check ---
    if recorded.get("seed") != current.get("seed"):
        verdict = "fatal"
        findings.append(
            "SEED CHANGED -- this account now presents a different device. "
            "A live account must keep its original seed; regenerating it is "
            "equivalent to moving the account to a new computer."
        )

    # --- fingerprint drift ---
    rec_fp = recorded.get("fingerprint", {})
    cur_fp = current.get("fingerprint", {})
    for key, rec_val in rec_fp.items():
        cur_val = cur_fp.get(key)
        if str(rec_val) != str(cur_val):
            if verdict != "fatal":
                verdict = "warn"
            findings.append(f"{key}: was {rec_val!r}, now {cur_val!r}")

    # --- exit location drift ---
    rec_exit = recorded.get("exit", {})
    cur_exit = current.get("exit", {})
    rec_tz, cur_tz = rec_exit.get("timezone"), cur_exit.get("timezone")
    # Prefer the stable two-letter code; records written before it existed only
    # carry the display name.
    rec_country = rec_exit.get("country_code") or rec_exit.get("country")
    cur_country = cur_exit.get("country_code") or cur_exit.get("country")

    if rec_tz and cur_tz and rec_tz != cur_tz:
        a, b = tz_offset_hours(rec_tz), tz_offset_hours(cur_tz)
        drift = abs(a - b) if (a is not None and b is not None) else None
        if drift is not None and drift > MAX_TZ_DRIFT_HOURS:
            if verdict == "ok":
                verdict = "warn"
            findings.append(
                f"EXIT TIMEZONE MOVED {drift:.0f}h: {rec_tz} -> {cur_tz}. A "
                f"cross-continent jump on a warmed account is a strong signal. "
                f"Prefer an exit in the same region ({rec_tz})."
            )
        else:
            span = f"{drift:.0f}h" if drift is not None else "unknown distance"
            findings.append(
                f"exit timezone moved {span}: {rec_tz} -> {cur_tz} "
                f"(within {MAX_TZ_DRIFT_HOURS:.0f}h tolerance)"
            )

    if rec_country and cur_country and rec_country != cur_country:
        if verdict == "ok":
            verdict = "warn"
        findings.append(f"exit country changed: {rec_country} -> {cur_country}")

    # --- the profile claiming a zone that matches neither ---
    if cur_tz and cur_fp.get("timezone") and cur_tz != cur_fp["timezone"]:
        a, b = tz_offset_hours(cur_tz), tz_offset_hours(cur_fp["timezone"])
        drift = abs(a - b) if (a is not None and b is not None) else None
        if drift is not None and drift > MAX_TZ_DRIFT_HOURS:
            if verdict != "fatal":
                verdict = "warn"
            findings.append(
                f"exit ({cur_tz}) and profile ({cur_fp['timezone']}) disagree by "
                f"{drift:.0f}h -- this is the mismatch --verify-proxy blocks"
            )

    return verdict, findings


def resolve(args) -> tuple[str, Fingerprint]:
    if args.seed:
        return args.seed, Fingerprint(args.seed)
    seed = derive_seed(args.master_key, args.account)
    return seed, Fingerprint(seed)


def cmd_record(args) -> int:
    seed, fp = resolve(args)
    geo = None
    if args.proxy:
        geo = lookup_proxy_geo(args.proxy, Path(args.profile_root).parent / ".geo-cache")
    ident = build_identity(args.account, seed, fp, geo)
    save_identity(args.profile_root, args.account, ident)
    print(f"recorded identity for {args.account}")
    print(f"  seed       {seed}")
    print(f"  timezone   {fp.timezone}")
    print(f"  languages  {fp.languages}")
    print(f"  screen     {ident['fingerprint']['screen']}")
    print(f"  exit       {ident['exit']}")
    print(f"  file       {identity_path(args.profile_root, args.account)}")
    return 0


def cmd_check(args) -> int:
    recorded = load_identity(args.profile_root, args.account)
    if recorded is None:
        print(f"no recorded identity for {args.account}")
        print(f"run: python fp_identity.py record --account {args.account} ...")
        return 2

    seed, fp = resolve(args)
    geo = None
    if args.proxy:
        geo = lookup_proxy_geo(args.proxy, Path(args.profile_root).parent / ".geo-cache")
    current = build_identity(args.account, seed, fp, geo)

    verdict, findings = compare(recorded, current)

    print(f"account : {args.account}")
    print(f"recorded: {recorded.get('recorded_at')}")
    print(f"verdict : {verdict.upper()}")
    if findings:
        print("\nfindings:")
        for f in findings:
            print(f"  - {f}")
    else:
        print("\nno divergence: same seed, same fingerprint, same exit region")

    return {"ok": 0, "warn": 1, "fatal": 2}[verdict]


def cmd_list(args) -> int:
    d = identity_dir(args.profile_root)
    if not d.exists():
        print(f"no identities under {d}")
        return 0
    rows = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        rows.append(
            (
                data.get("account", p.stem),
                data.get("recorded_at", "?"),
                data.get("fingerprint", {}).get("timezone", "?"),
                data.get("exit", {}).get("country") or "-",
                (data.get("exit", {}).get("ip") or "-"),
            )
        )
    print(f"{'account':<18} {'recorded':<20} {'tz':<20} {'cc':<4} exit ip")
    print("-" * 82)
    for r in rows:
        print(f"{r[0]:<18} {r[1]:<20} {r[2]:<20} {r[3]:<4} {r[4]}")
    print(f"\n{len(rows)} account(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Track account identity over time and flag risky changes"
    )
    ap.add_argument("--profile-root", default=DEFAULT_PROFILE_ROOT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, help_text in (
        ("record", "snapshot the account's current identity"),
        ("check", "compare the current identity against the snapshot"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--account", required=True)
        p.add_argument("--seed", help="raw seed; overrides --master-key derivation")
        p.add_argument("--master-key", default=os.environ.get("FP_MASTER_KEY", ""))
        p.add_argument("--proxy", default="", help="proxy whose exit location to record")
        # SUPPRESS, not None: an unset subcommand flag must not write the
        # attribute at all, otherwise the subparser default overwrites the value
        # already parsed from the global --profile-root.
        p.add_argument("--profile-root", default=argparse.SUPPRESS)

    sub.add_parser("list", help="list recorded identities").add_argument(
        "--profile-root", default=argparse.SUPPRESS
    )

    args = ap.parse_args()

    # One place decides the profile root; a subcommand flag wins only if given.
    if getattr(args, "profile_root", None) is None:
        args.profile_root = DEFAULT_PROFILE_ROOT

    if args.cmd in ("record", "check"):
        if not args.seed and not args.master_key:
            ap.error("provide --seed or --master-key (or set $FP_MASTER_KEY)")

    if args.cmd == "record":
        return cmd_record(args)
    if args.cmd == "check":
        return cmd_check(args)
    return cmd_list(args)


if __name__ == "__main__":
    sys.exit(main())
