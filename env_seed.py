"""Pick a fingerprint seed whose derived timezone matches a proxy's exit.

Why this exists: in the environment model the seed is no longer derived from an
account the launcher prepared ahead of time -- an environment is created inside a
running browser, so something has to choose its seed at that moment. Choosing one
at random is what the timezone gate exists to prevent: a profile claiming
Asia/Shanghai while exiting in New York is one lookup away from being tied to
every other environment behind the same proxy.

The search is a brute force over seed suffixes because the derivation is a hash:
there is no closed form for "seed whose tz draw lands in this zone". Small value
spaces make this cheap -- a zone list holds 3-6 entries, so a match appears within
a few dozen tries.

    python env_seed.py --proxy socks5://127.0.0.1:7890 --base envA
    python env_seed.py --exit-ip 212.60.14.245 --timezone America/New_York
    python env_seed.py --proxy http://1.2.3.4:8080 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fp_launcher import Fingerprint, lookup_proxy_geo  # noqa: E402

CACHE = HERE / ".geo-cache"


def pick_seed(base: str, want_tz: str, limit: int = 4000) -> tuple[str, Fingerprint] | None:
    for i in range(1, limit + 1):
        seed = f"{base}-{i:04d}"
        fp = Fingerprint(seed)
        if fp.timezone == want_tz:
            return seed, fp
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="",
                    help="proxy URL to look the exit up through (x://host:port)")
    ap.add_argument("--exit-ip", default="")
    ap.add_argument("--timezone", default="", help="IANA zone of the exit")
    ap.add_argument("--base", default="env", help="prefix of the seed to search")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    exit_ip = args.exit_ip
    exit_tz = args.timezone
    geo: dict = {}
    if args.proxy:
        geo = lookup_proxy_geo(args.proxy, CACHE, force=True) or {}
        exit_ip = exit_ip or geo.get("query", "")
        exit_tz = exit_tz or geo.get("timezone", "")
    if not exit_tz:
        print("FAIL: need --timezone or a reachable --proxy", file=sys.stderr)
        return 2

    found = pick_seed(args.base, exit_tz)
    if not found:
        print(f"FAIL: no seed under '{args.base}-*' derives to {exit_tz}",
              file=sys.stderr)
        return 1
    seed, fp = found

    payload = {
        "seed": seed,
        "timezone": fp.timezone,
        "languages": fp.languages,
        "ui_language": fp.ui_language,
        "exit_ip": exit_ip,
        "exit_timezone": exit_tz,
        "exit_country": geo.get("countryCode", ""),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"seed        {seed}")
        print(f"derived tz  {fp.timezone}   languages {fp.languages}")
        print(f"exit        {exit_ip or '-'}  {exit_tz}  {geo.get('countryCode', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
