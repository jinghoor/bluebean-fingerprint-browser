"""Per-account proxy configuration with a local authenticating relay.

Chrome's `--proxy-server` accepts `scheme://host:port` and nothing else. Measured
on this build:

    --proxy-server=socks5://127.0.0.1:7890                works
    --proxy-server=socks5://tester:secret@127.0.0.1:7890  fails outright
    (no --proxy-server)                                   uses the system proxy
    --no-proxy-server                                     really bypasses

So every commercial proxy -- which all require credentials -- needs a relay: a
local listener Chrome can reach without auth, forwarding to the upstream with the
credentials attached. That is what this module provides, for the three schemes
Chrome speaks:

    socks5  local SOCKS5 listener  -> upstream SOCKS5 (user/pass, RFC 1929)
    http    local HTTP CONNECT     -> upstream HTTP CONNECT (Proxy-Authorization)
    https   local HTTP CONNECT     -> upstream TLS + HTTP CONNECT

Two modes need no relay at all:

    direct  -> --no-proxy-server
    system  -> add nothing, let Chrome use the OS settings

Configuration lives in a JSON file, one entry per account, so an account's exit
address is a property of the account rather than of the command line. That is the
whole point: the same account must keep the same IP across launches, and two
accounts must never share one.

Usage (library):
    cfg = ProxyConfig.load(path)
    spec = cfg.for_account("acct-001")
    with spec.relay() as relay:            # no-op for direct/system
        argv += relay.chrome_flags()

CLI:
    python fp_proxy.py list
    python fp_proxy.py set  --account acct-001 --mode socks5 --host 1.2.3.4 --port 1080 --username u --password p
    python fp_proxy.py set  --account acct-002 --mode direct
    python fp_proxy.py test --account acct-001
    python fp_proxy.py clear --account acct-001
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess

from _nowindow import no_window
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from bluebean_paths import PROXY_CONFIG

DEFAULT_CONFIG = Path(os.environ.get("FP_PROXY_CONFIG") or PROXY_CONFIG)

SCHEMES = ("socks5", "http", "https")

# direct  -- bypass every proxy (--no-proxy-server)
# system  -- follow the OS proxy settings; add no flag at all
# local   -- a proxy on this machine at a chosen port, independent of the OS
#            settings. Useful when the OS proxy is unset or should not be
#            touched, but a local client (clash, v2ray) is listening anyway.
# socks5/http/https -- a remote exit, optionally with credentials
MODES = SCHEMES + ("direct", "system", "local")

# Schemes usable for the local mode. "https" is excluded: a local TLS-terminating
# proxy is possible but nothing installs one by default, and offering it invites
# configs that silently never connect.
LOCAL_SCHEMES = ("socks5", "http")


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

@dataclass
class ProxySpec:
    """One account's exit configuration."""

    mode: str = "direct"
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    scheme: str = "socks5"        # only meaningful for mode == "local"

    @property
    def needs_relay(self) -> bool:
        return self.mode in SCHEMES and bool(self.username)

    @property
    def upstream_url(self) -> str:
        """What could be handed to --proxy-server if no credentials were needed."""
        if self.mode in SCHEMES and self.host:
            return f"{self.mode}://{self.host}:{self.port}"
        return ""

    def redacted(self) -> str:
        if self.mode == "direct":
            return "direct"
        if self.mode == "system":
            return "system (follow OS proxy settings)"
        if self.mode == "local":
            return f"local {self.scheme}://127.0.0.1:{self.port}"
        auth = "(auth)" if self.username else "(no-auth)"
        return f"{self.mode}://{auth}@{self.host}:{self.port}"

    def validate(self) -> list[str]:
        problems = []
        if self.mode not in MODES:
            problems.append(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.mode == "local":
            if not (0 < self.port < 65536):
                problems.append(f"local mode needs a port, got {self.port}")
            if self.scheme not in LOCAL_SCHEMES:
                problems.append(
                    f"local scheme must be one of {LOCAL_SCHEMES}, "
                    f"got {self.scheme!r}")
        if self.mode in SCHEMES:
            if not self.host:
                problems.append("host is required for a proxied mode")
            if not (0 < self.port < 65536):
                problems.append(f"port out of range: {self.port}")
            if self.username and not self.password:
                problems.append("username given without a password")
        return problems

    def relay(self) -> "Relay":
        return Relay(self)

    def chrome_flags(self) -> list[str]:
        """Flags for the case where no relay is needed."""
        if self.mode == "direct":
            # Without this Chrome falls back to the system proxy -- measured.
            return ["--no-proxy-server"]
        if self.mode == "system":
            # Follow the OS settings: pass nothing and let Chromium read them.
            return []
        if self.mode == "local":
            # A proxy on this machine, chosen explicitly. Distinct from "system"
            # because it does not depend on -- or alter -- the OS configuration.
            return [f"--proxy-server={self.scheme}://127.0.0.1:{self.port}"]
        if self.mode in SCHEMES and self.host:
            return [f"--proxy-server={self.mode}://{self.host}:{self.port}"]
        return ["--no-proxy-server"]


@dataclass
class ProxyConfig:
    accounts: dict[str, ProxySpec] = field(default_factory=dict)
    path: Path = DEFAULT_CONFIG

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG) -> "ProxyConfig":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise SystemExit(f"proxy config {p} is unreadable: {e}")
        accounts: dict[str, ProxySpec] = {}
        for name, entry in (raw.get("accounts") or {}).items():
            accounts[name] = ProxySpec(
                mode=entry.get("mode", "direct"),
                host=entry.get("host", ""),
                port=int(entry.get("port") or 0),
                username=entry.get("username", ""),
                password=entry.get("password", ""),
                scheme=entry.get("scheme", "socks5"),
            )
        return cls(accounts=accounts, path=p)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"accounts": {}}
        for name, spec in sorted(self.accounts.items()):
            entry = {"mode": spec.mode}
            if spec.mode in SCHEMES:
                entry.update({"host": spec.host, "port": spec.port})
                if spec.username:
                    entry.update({"username": spec.username,
                                  "password": spec.password})
            elif spec.mode == "local":
                entry.update({"port": spec.port, "scheme": spec.scheme})
            payload["accounts"][name] = entry
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)   # credentials live here
        except OSError:
            pass

    def for_account(self, account: str) -> ProxySpec:
        spec = self.accounts.get(account)
        if spec is None:
            raise SystemExit(
                f"no proxy configured for account {account!r} in {self.path}. "
                f"Configured: {sorted(self.accounts) or '(none)'}\n"
                f"Run: python fp_proxy.py set --account {account} --mode ...")
        problems = spec.validate()
        if problems:
            raise SystemExit(
                f"proxy config for {account!r} is invalid: " + "; ".join(problems))
        return spec

    def assign(self, account: str, spec: ProxySpec) -> None:
        problems = spec.validate()
        if problems:
            raise SystemExit("invalid proxy spec: " + "; ".join(problems))
        self.accounts[account] = spec


# ---------------------------------------------------------------------------
# relay
# ---------------------------------------------------------------------------

class Relay:
    """Local authenticating proxy in front of an upstream that needs credentials.

    Lifecycle: `with Relay(spec) as r: argv += r.chrome_flags()`. When the spec has
    no credentials the relay is a no-op and `chrome_flags()` returns the direct
    flags, so callers do not need to special-case anything.
    """

    def __init__(self, spec: ProxySpec):
        self.spec = spec
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._port = 0
        self._error: str | None = None

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "Relay":
        if not self.spec.needs_relay:
            return self
        self._start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop()

    def _start(self) -> None:
        ready = threading.Event()

        def run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                server = loop.run_until_complete(self._serve())
                self._server = server
                self._port = server.sockets[0].getsockname()[1]
                ready.set()
                loop.run_forever()
            except Exception as e:                      # noqa: BLE001
                self._error = f"{type(e).__name__}: {e}"
                ready.set()
            finally:
                # Let connection handlers close both ends before destroying the
                # private loop (notably Windows' pending IOCP accept operations).
                if self._server:
                    self._server.close()
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                if self._server:
                    loop.run_until_complete(self._server.wait_closed())
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
                self._port = 0

        self._thread = threading.Thread(target=run, daemon=True,
                                        name="fp-proxy-relay")
        self._thread.start()
        if not ready.wait(10):
            raise SystemExit("relay did not start within 10s")
        if self._error:
            raise SystemExit(f"relay failed to start: {self._error}")

    def _stop(self) -> None:
        if self._loop:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    async def _serve(self):
        return await asyncio.start_server(self._handle, "127.0.0.1", 0)

    def chrome_flags(self) -> list[str]:
        if not self.spec.needs_relay:
            return self.spec.chrome_flags()
        if not self._port:
            raise RuntimeError("relay is not running; use it as a context manager")
        # Chrome talks to the local listener in the same scheme it would have used
        # upstream, so the TLS/plain decision stays identical.
        scheme = "socks5" if self.spec.mode == "socks5" else "http"
        return [f"--proxy-server={scheme}://127.0.0.1:{self._port}"]

    # -- dispatch ----------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            if self.spec.mode == "socks5":
                await self._socks5(reader, writer)
            else:
                await self._http_connect(reader, writer)
        except Exception:                               # noqa: BLE001
            pass
        finally:
            await self._close_writer(writer)

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=2)
        except (OSError, asyncio.TimeoutError):
            pass

    async def _pipe(self, reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter) -> None:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                # TCP EOF only ends this direction. The server may still be
                # sending a response; closing the socket here truncates it.
                if writer.can_write_eof():
                    writer.write_eof()
                    await writer.drain()
                else:
                    # TLS transports do not support TCP half-close.
                    writer.close()
                return
            writer.write(chunk)
            await writer.drain()

    async def _tunnel(self, reader, writer, up_r, up_w) -> None:
        pipes = [asyncio.create_task(self._pipe(reader, up_w)),
                 asyncio.create_task(self._pipe(up_r, writer))]
        try:
            await asyncio.gather(*pipes)
        finally:
            for task in pipes:
                task.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)

    # -- SOCKS5 ------------------------------------------------------------
    async def _socks5(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        # greeting from Chrome: version, nmethods, methods
        head = await reader.readexactly(2)
        if head[0] != 5:
            return
        await reader.readexactly(head[1])
        writer.write(b"\x05\x00")           # no auth for the local hop
        await writer.drain()

        req = await reader.readexactly(4)
        if req[0] != 5 or req[1] != 1:      # CONNECT only
            writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return
        atyp = req[3]
        # Keep the ATYP and the address bytes as they arrived. Handing the upstream
        # the same encoding the client chose avoids both a lossy decode and an
        # encode that could change the label (IDNA vs raw ASCII). This is also what
        # made the earlier idna/replace call fail: that codec takes no `errors`
        # argument, so every domain-form request died with UnicodeError.
        if atyp == 1:
            addr = b"\x01" + await reader.readexactly(4)
        elif atyp == 3:
            n = (await reader.readexactly(1))[0]
            addr = b"\x03" + bytes([n]) + await reader.readexactly(n)
        elif atyp == 4:
            addr = b"\x04" + await reader.readexactly(16)
        else:
            writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return
        port = struct.unpack("!H", await reader.readexactly(2))[0]

        up_r, up_w = await self._connect_upstream_socks5(addr, port)
        if up_r is None:
            writer.write(b"\x05\x05\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return
        try:
            writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await writer.drain()
            await self._tunnel(reader, writer, up_r, up_w)
        finally:
            await self._close_writer(up_w)

    async def _connect_upstream_socks5(self, addr: bytes, port: int):
        spec = self.spec
        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(spec.host, spec.port), timeout=20)
        except Exception:                               # noqa: BLE001
            return None, None
        try:
            if spec.username:
                # Offer user/pass ONLY. Measured: offering a "no authentication"
                # method as well let the upstream pick it, and the unauthenticated
                # session could reach some hosts but stalled on facebook.com,
                # tiktok.com, google.com and youtube.com (SOCKS5 error 97 after
                # ~20.8 s through this relay, while curl with credentials straight
                # to the same upstream reached all four in 2-4 s).
                w.write(b"\x05\x01\x02")                # user/pass only
                await w.drain()
                resp = await asyncio.wait_for(r.readexactly(2), timeout=10)
                if resp[0] != 5 or resp[1] != 2:
                    raise RuntimeError("upstream did not select the offered authentication method")
                if resp[1] == 2:
                    u = spec.username.encode()
                    p = spec.password.encode()
                    w.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
                    await w.drain()
                    auth = await asyncio.wait_for(r.readexactly(2), timeout=10)
                    if auth != b"\x01\x00":
                        raise RuntimeError("upstream rejected credentials")
            else:
                w.write(b"\x05\x01\x00")
                await w.drain()
                if await asyncio.wait_for(r.readexactly(2), timeout=10) != b"\x05\x00":
                    raise RuntimeError("upstream did not select the offered authentication method")

            # addr already carries its own ATYP, so it can be forwarded verbatim.
            # A domain reaches the upstream unresolved, which keeps DNS off the host.
            w.write(b"\x05\x01\x00" + addr + struct.pack("!H", port))
            await w.drain()
            head = await asyncio.wait_for(r.readexactly(4), timeout=20)
            if head[:3] != b"\x05\x00\x00":
                raise RuntimeError(f"CONNECT failed, reply {head[1]}")
            if head[3] == 1:
                await asyncio.wait_for(r.readexactly(4 + 2), timeout=10)
            elif head[3] == 3:
                n = (await asyncio.wait_for(r.readexactly(1), timeout=10))[0]
                await asyncio.wait_for(r.readexactly(n + 2), timeout=10)
            elif head[3] == 4:
                await asyncio.wait_for(r.readexactly(16 + 2), timeout=10)
            else:
                raise RuntimeError("invalid upstream address type")
            return r, w
        except (Exception, asyncio.CancelledError) as exc:
            await self._close_writer(w)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return None, None

    # -- HTTP CONNECT ------------------------------------------------------
    async def _http_connect(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=15)
        except asyncio.TimeoutError:
            return
        if not request_line:
            return
        headers: list[bytes] = []
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=15)
            if not line or line in (b"\r\n", b"\n"):
                break
            headers.append(line)

        parts = request_line.split()
        if len(parts) < 2 or parts[0] != b"CONNECT":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await writer.drain()
            return
        target = parts[1].decode("latin-1")
        if ":" not in target:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return
        host, _, port_s = target.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return

        up_r, up_w = await self._connect_upstream_http(host, port, headers)
        if up_r is None:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            return
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await self._tunnel(reader, writer, up_r, up_w)
        finally:
            await self._close_writer(up_w)

    async def _connect_upstream_http(self, host: str, port: int,
                                     client_headers: list[bytes]):
        spec = self.spec
        try:
            if spec.mode == "https":
                ctx = ssl.create_default_context()
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(spec.host, spec.port, ssl=ctx),
                    timeout=20)
            else:
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(spec.host, spec.port), timeout=20)
        except Exception:                               # noqa: BLE001
            return None, None
        try:
            lines = [f"CONNECT {host}:{port} HTTP/1.1",
                     f"Host: {host}:{port}"]
            if spec.username:
                token = base64.b64encode(
                    f"{spec.username}:{spec.password}".encode()).decode()
                lines.append(f"Proxy-Authorization: Basic {token}")
            # Keep User-Agent etc. if the browser sent them; drop hop-by-hop bits.
            for raw in client_headers:
                name = raw.split(b":", 1)[0].strip().lower()
                if name in (b"proxy-authorization", b"proxy-connection",
                            b"content-length", b"transfer-encoding"):
                    continue
                if name in (b"host",):
                    continue
                lines.append(raw.decode("latin-1").rstrip("\r\n"))
            w.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            await w.drain()
            status = await asyncio.wait_for(r.readline(), timeout=25)
            if not status:
                raise RuntimeError("no status line")
            code = int(status.split()[1])
            while True:
                line = await asyncio.wait_for(r.readline(), timeout=15)
                if not line or line in (b"\r\n", b"\n"):
                    break
            if code != 200:
                raise RuntimeError(f"upstream CONNECT returned {code}")
            return r, w
        except (Exception, asyncio.CancelledError) as exc:
            await self._close_writer(w)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return None, None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _curl() -> str | None:
    if os.name == "nt":
        cand = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "curl.exe")
        if os.path.exists(cand):
            return cand
    return shutil.which("curl")


def cmd_list(args) -> int:
    cfg = ProxyConfig.load(args.config)
    if not cfg.accounts:
        print(f"no accounts configured in {cfg.path}")
        return 0
    print(f"{'account':<24}{'mode':<10}{'endpoint':<28}auth")
    print("-" * 72)
    for name in sorted(cfg.accounts):
        spec = cfg.accounts[name]
        if spec.mode in SCHEMES:
            endpoint = f"{spec.host}:{spec.port}"
        elif spec.mode == "local":
            endpoint = f"127.0.0.1:{spec.port} ({spec.scheme})"
        else:
            endpoint = "-"
        auth = "yes" if spec.username else ("no" if spec.mode in SCHEMES else "-")
        print(f"{name:<24}{spec.mode:<10}{endpoint:<28}{auth}")
    return 0


def cmd_set(args) -> int:
    cfg = ProxyConfig.load(args.config)
    spec = ProxySpec(mode=args.mode, host=args.host or "",
                     port=int(args.port or 0),
                     username=args.username or "",
                     password=args.password or "",
                     scheme=args.scheme)
    cfg.assign(args.account, spec)
    cfg.save()
    print(f"configured {args.account}: {spec.redacted()}")
    if spec.needs_relay:
        print("  credentials present -> launches go through a local relay")
    return 0


def cmd_clear(args) -> int:
    cfg = ProxyConfig.load(args.config)
    if args.account not in cfg.accounts:
        print(f"{args.account} was not configured")
        return 0
    del cfg.accounts[args.account]
    cfg.save()
    print(f"cleared {args.account}")
    return 0


def cmd_test(args) -> int:
    """Prove the configured exit actually works, through the same relay Chrome uses."""
    cfg = ProxyConfig.load(args.config)
    spec = cfg.for_account(args.account)
    curl = _curl()
    if not curl:
        print("FAIL: curl not found")
        return 2

    print(f"account {args.account}: {spec.redacted()}")
    with spec.relay() as relay:
        flags = relay.chrome_flags()
        proxy_arg = ""
        for f in flags:
            if f.startswith("--proxy-server="):
                proxy_arg = f.split("=", 1)[1]
            elif f == "--no-proxy-server":
                proxy_arg = ""
        if spec.mode == "system":
            print("  system mode: this check exercises the OS proxy, not a config")
            proxy_arg = ""

        if spec.mode == "direct":
            print("  direct mode: verifying that traffic does NOT use a proxy")
            out = subprocess.run([curl, "-s", "--max-time", "15",
                                  "--noproxy", "*",
                                  "https://api.ipify.org?format=json"],
                                 capture_output=True, text=True, timeout=30, creationflags=no_window())
            print(f"  exit: {(out.stdout or out.stderr).strip()[:100]}")
            return 0

        cmd = [curl, "-s", "--max-time", "20", "--proxy", proxy_arg,
               "https://api.ipify.org?format=json"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=40, creationflags=no_window())
        except subprocess.SubprocessError as e:
            print(f"  FAIL: {e}")
            return 1
        body = (out.stdout or "").strip()
        if out.returncode != 0 or not body.startswith("{"):
            print(f"  FAIL: rc={out.returncode} body={body[:120]}")
            return 1
        print(f"  exit: {body[:100]}")
        print("  RESULT: PASS -- the relay path works end to end")
        return 0


def cmd_serve(args) -> int:
    """Run the relay as a standalone process.

    The launcher may detach, at which point its process exits while Chrome keeps
    running -- so the relay cannot live inside it. This mode writes the chosen
    port to a file and blocks until the TTL expires or the process is signalled.
    """
    cfg = ProxyConfig.load(args.config)
    spec = cfg.for_account(args.account)
    if not spec.needs_relay:
        print(f"{args.account} needs no relay ({spec.redacted()})", file=sys.stderr)
        return 2

    port_file = Path(args.port_file)
    port_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        port_file.unlink()
    except OSError:
        pass

    relay = spec.relay()
    relay.__enter__()
    try:
        flags = relay.chrome_flags()
        proxy_url = flags[0].split("=", 1)[1]
        payload = {
            "account": args.account,
            "proxy_url": proxy_url,
            "port": int(proxy_url.rsplit(":", 1)[1]),
            "pid": os.getpid(),
            "started": time.time(),
            "mode": spec.mode,
            "upstream": f"{spec.host}:{spec.port}",
        }
        tmp = port_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, port_file)
        print(f"relay ready: {proxy_url} -> {spec.host}:{spec.port}", flush=True)
        deadline = time.time() + args.ttl
        while time.time() < deadline:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        relay.__exit__(None, None, None)
        try:
            port_file.unlink()
        except OSError:
            pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Per-account proxy configuration and local relay")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="show every configured account")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set", help="configure one account's exit")
    p.add_argument("--account", required=True)
    p.add_argument("--mode", required=True, choices=MODES)
    p.add_argument("--host", default="")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--username", default="")
    p.add_argument("--password", default="")
    p.add_argument("--scheme", default="socks5", choices=list(LOCAL_SCHEMES),
                   help="protocol for --mode local (default socks5)")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("clear", help="remove one account's configuration")
    p.add_argument("--account", required=True)
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("test", help="verify the configured exit works")
    p.add_argument("--account", required=True)
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("serve", help="run the relay as a standalone process")
    p.add_argument("--account", required=True)
    p.add_argument("--port-file", required=True)
    p.add_argument("--ttl", type=int, default=86400,
                   help="exit after this many seconds (default 24h)")
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
