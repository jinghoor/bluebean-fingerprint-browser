"""Per-profile, on-demand startup checks. Never scan inactive environments."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit
import urllib.request

from _runtime_state import atomic_json, startup_lock

VERSION = 5
SITES = [('Google', 'https://www.gstatic.com/generate_204'),
         ('Facebook', 'https://www.facebook.com/robots.txt'),
         ('Instagram', 'https://www.instagram.com/robots.txt'),
         ('TikTok', 'https://www.tiktok.com/robots.txt'),
         ('百度', 'https://www.baidu.com/robots.txt'),
         ('哔哩哔哩', 'https://www.bilibili.com/robots.txt')]
EXIT_SOURCES = [('IPinfo', 'https://ipinfo.io/json'), ('IPIP', 'https://myip.ipip.net/')]
EXIT_FALLBACKS = [('ipify', 'https://api.ipify.org?format=json'), ('3322', 'https://ip.3322.net/')]


def signature(row):
    return tuple(str(row.get(k, '')) for k in ('proxy', 'expected_exit_ip', 'ip_mode'))


def normalize_ip(value):
    return str(ipaddress.ip_address(str(value).strip()))


def probe_system_url(url):
    """Use Windows Internet Settings, including PAC and bypass rules.

    Never substitute shell HTTP_PROXY variables for Chromium's system setting.
    A private WinINet session does not read or write the browser's cookies.
    """
    import ctypes as c
    from ctypes import wintypes as w
    started = time.monotonic()
    session = request = None
    try:
        api = c.WinDLL('wininet', use_last_error=True)
        api.InternetOpenW.argtypes = [w.LPCWSTR, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD]
        api.InternetOpenW.restype = w.HANDLE
        api.InternetSetOptionW.argtypes = [w.HANDLE, w.DWORD, c.c_void_p, w.DWORD]
        api.InternetOpenUrlW.argtypes = [w.HANDLE, w.LPCWSTR, w.LPCWSTR, w.DWORD, w.DWORD, c.c_size_t]
        api.InternetOpenUrlW.restype = w.HANDLE
        api.HttpQueryInfoW.argtypes = [w.HANDLE, w.DWORD, c.c_void_p, c.POINTER(w.DWORD), c.POINTER(w.DWORD)]
        api.InternetReadFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
        api.InternetCloseHandle.argtypes = [w.HANDLE]
        session = api.InternetOpenW('BlueBean Environment Check', 0, None, None, 0)
        if not session:
            raise c.WinError(c.get_last_error())
        for option, value in ((2, 3000), (3, 1), (5, 7000), (6, 7000)):
            timeout = w.DWORD(value)
            if not api.InternetSetOptionW(session, option, c.byref(timeout), c.sizeof(timeout)):
                raise c.WinError(c.get_last_error())
        # Reload, do not cache, never use cookies, never display an OS dialog.
        request = api.InternetOpenUrlW(session, url, None, 0, 0x84080200, 0)
        if not request:
            raise c.WinError(c.get_last_error())
        status, length = w.DWORD(), w.DWORD(c.sizeof(w.DWORD))
        if not api.HttpQueryInfoW(request, 19 | 0x20000000, c.byref(status), c.byref(length), None):
            raise c.WinError(c.get_last_error())
        chunks, size = [], 0
        while size < 32768:
            buffer, received = c.create_string_buffer(min(8192, 32768-size)), w.DWORD()
            if not api.InternetReadFile(request, buffer, len(buffer), c.byref(received)):
                raise c.WinError(c.get_last_error())
            if not received.value:
                break
            chunks.append(buffer.raw[:received.value]);size += received.value
        return {'ok': 200 <= status.value < 400, 'status': status.value, 'reachable': True,
                'latency_ms': round((time.monotonic()-started)*1000),
                'body': b''.join(chunks).decode('utf-8', 'replace')}
    except (OSError, AttributeError) as exc:
        return {'ok': False, 'status': 0, 'reachable': False,
                'error': 'WindowsSystemProxy:' + str(getattr(exc, 'winerror', type(exc).__name__)),
                'latency_ms': round((time.monotonic()-started)*1000)}
    finally:
        if request:
            api.InternetCloseHandle(request)
        if session:
            api.InternetCloseHandle(session)


def probe_url(proxy, url):
    if proxy == 'system':
        return probe_system_url(url)
    import requests
    started = time.monotonic()
    try:
        with requests.Session() as session:
            session.trust_env = False
            proxy = proxy.replace('socks5://', 'socks5h://', 1).replace('socks4://', 'socks4a://', 1)
            # Explicit direct mode must never inherit HTTP_PROXY or system settings.
            session.proxies = {} if proxy in ('', 'direct') else {'http': proxy, 'https': proxy}
            response = session.get(url, timeout=(3, 7), allow_redirects=True)
            return {'ok': 200 <= response.status_code < 400, 'status': response.status_code,
                    'reachable': True,
                    'latency_ms': round((time.monotonic() - started) * 1000),
                    'body': response.text[:32768]}
    except Exception as exc:
        return {'ok': False, 'status': 0, 'error': type(exc).__name__,
                'reachable': False,
                'latency_ms': round((time.monotonic() - started) * 1000)}


def exit_result(reply):
    """Accept structured IP responses, a bare address, or IPIP's explicit IP label.

    Never infer an exit from arbitrary numbers embedded in a block/error page.
    An unavailable geolocation service must not hide a valid public exit address.
    """
    if not reply.get('ok'):
        return None
    body = str(reply.get('body', '')).strip()
    candidate = None
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            candidate = parsed
    except (ValueError, TypeError):
        pass
    if candidate is None:
        address = body
        match = re.match(r'^当前\s*IP\s*[:：]\s*([^\s,，]+)', body)
        if match:
            address = match.group(1)
        candidate = {'ip': address}
    try:
        address = normalize_ip(candidate.get('ip', ''))
    except (ValueError, TypeError):
        return None
    geo = {k: str(candidate[k])[:256] for k in ('country', 'region', 'city', 'postal', 'timezone', 'org')
           if isinstance(candidate.get(k), str) and candidate[k]}
    return {'ip': address, **geo, 'latency_ms': reply.get('latency_ms')}


def transient_site_failure(reply):
    """Retry a failed connection, never a site's HTTP restriction or bad URL."""
    if reply.get('ok') or reply.get('reachable') or reply.get('status'):
        return False
    error = str(reply.get('error', ''))
    return error in {'ConnectTimeout', 'ReadTimeout', 'Timeout', 'ConnectionError',
                     'ProxyError', 'ConnectionResetError', 'ConnectionRefusedError',
                     'RemoteDisconnected'} or error in {
                         'WindowsSystemProxy:' + str(code)
                         for code in (12002, 12007, 12029, 12030, 12031)}


def network_check(row, probe=probe_url, progress=None):
    if row.get('restart_required'):
        return {'status': 'error', 'error': '此环境的代理设置已更改。请重启蓝豆浏览器后重新检测，避免使用旧代理连接。', 'probes': {}}
    proxy = row.get('proxy', '').strip()
    direct = proxy in ('', 'direct')
    probes, geo, source, rank = {}, {}, '', float('inf')
    responded = False

    def publish():
        if progress:
            progress({'geo': dict(geo), 'exit_ip': geo.get('ip', ''),
                      'ip_source': source, 'probes': {k: dict(v) for k, v in probes.items()}})

    def batch(sources, sites=(), attempt=1):
        nonlocal geo, source, rank, responded
        # Only this environment is queried. Alternate services use the same route;
        # a failed proxy is never retried through a direct/system connection.
        with ThreadPoolExecutor(max_workers=3 if attempt == 2 else 8) as pool:
            futures = {pool.submit(probe, proxy, url): ('site', name, 0) for name, url in sites}
            futures.update({pool.submit(probe, proxy, url): ('exit', name, index)
                            for index, (name, url) in enumerate(sources)})
            for future in as_completed(futures):
                kind, name, priority = futures[future]
                try:
                    reply = future.result()
                    if not isinstance(reply, dict):
                        raise ValueError('invalid probe result')
                except Exception as exc:
                    reply = {'ok': False, 'error': type(exc).__name__, 'status': 0}
                responded = responded or bool(reply.get('reachable') or reply.get('ok'))
                if kind == 'site':
                    previous = probes.get(name, {})
                    probes[name] = {k: v for k, v in reply.items() if k != 'body'}
                    probes[name]['attempts'] = attempt
                    if attempt == 2:
                        probes[name].update(first_error=previous.get('error', ''),
                                            first_latency_ms=previous.get('latency_ms'),
                                            recovered=bool(reply.get('ok')))
                else:
                    found = exit_result(reply)
                    if found and priority < rank:
                        geo, source, rank = found, name, priority
                publish()

    batch(EXIT_SOURCES, SITES)
    if not geo:
        batch(EXIT_FALLBACKS)
    # The first connections may race proxy/DNS/TLS warm-up. Once this route has
    # responded, verify only connection failures once, using the same route and
    # at most three concurrent requests. Never rerun successful/restricted sites,
    # IP lookups, other environments, or fall back to a direct connection.
    retry_sites = [(name, url) for name, url in SITES
                   if transient_site_failure(probes.get(name, {}))] if geo or responded else []
    if retry_sites:
        for name, _ in retry_sites:
            probes[name]['retrying'] = True
        publish()
        time.sleep(0.25)
        batch((), retry_sites, attempt=2)
    connected = bool(geo) or responded
    result = {'geo': geo, 'probes': probes, 'exit_ip': geo.get('ip', ''), 'ip_source': source,
              'network_status': 'online' if connected else 'unreachable', 'checking_network': False,
              'expected_exit_ip': row.get('expected_exit_ip', '')}
    if not geo:
        # A website can be reachable while every IP service is blocked. Distinguish
        # that case from all probes failing, and never fabricate an address.
        message = ('网络已连通，但出口 IP 查询服务暂不可用，请重新检测。' if connected else
                   '当前环境的网络检测未能连通，无法取得出口 IP，请检查网络后重试。')
        result.update(ip_error='ip_lookup_failed' if connected else 'network_unreachable')
        if direct:
            return {**result, 'status': 'direct', 'allowed': True, 'warning': message}
        return {**result, 'status': 'error', 'error': message, 'allowed': False}
    if direct:
        return {**result, 'status': 'direct', 'allowed': True}
    expected = row.get('expected_exit_ip', '')
    if row.get('ip_mode') == 'dynamic':
        status = 'dynamic'
    elif not expected:
        status = 'unrecorded'
    else:
        try:
            status = 'matched' if normalize_ip(expected) == geo['ip'] else 'mismatch'
        except ValueError:
            status = 'unrecorded'
    return {**result, 'status': status, 'allowed': status in ('matched', 'dynamic')}


class StartupChecks:
    def __init__(self, profile, command=None, probe=probe_url, account_store=None, account_command=None):
        self.profile = Path(profile)
        self.command = command or self._command
        self.probe = probe
        self.lock = threading.RLock()
        self.jobs = {}
        if account_store is None:
            import env_cli
            from environment_accounts import AccountStore
            from local_account_vault import LocalAccountVault
            account_store = AccountStore(LocalAccountVault(
                env_cli.resolve_profile_dir(self.profile) / 'env-account-vault.dpapi.json'))
        self.account_store = account_store
        self.account_command = account_command or self._account_command
        self.account_open_lock = threading.Lock()

    def _account_command(self, payload):
        from bluebean_paths import DATA_ROOT
        state = json.loads((DATA_ROOT / '.lan-bridge.json').read_text(encoding='utf-8'))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        headers = {'Authorization': 'Bearer ' + state['token']}
        with opener.open(urllib.request.Request(f"http://127.0.0.1:{int(state['port'])}/v1/health",
                         headers=headers), timeout=3) as reply:
            health = json.load(reply)
        if (not health.get('ok') or Path(health.get('profile_root', '')).resolve() != self.profile.resolve()
                or Path(health.get('app_root', '')).resolve() != Path(__file__).resolve().parent):
            raise ValueError('账号服务未连接到当前环境，请重新启动浏览器。')
        request = urllib.request.Request(f"http://127.0.0.1:{int(state['port'])}/v1/lan/manage/accounts",
            data=json.dumps(payload).encode('utf-8'), headers={**headers, 'Content-Type':'application/json'})
        with opener.open(request, timeout=25) as reply:
            result = json.load(reply)
        if not result.get('ok'):
            raise ValueError(result.get('error', '账号网站未能打开，请重试。'))
        return result

    def account_owner(self, env):
        for parent, bucket in self.account_store.metadata().items():
            if any(a.get('session_env_id') == env for a in bucket.get('accounts', [])):
                return parent
        return env

    def accounts(self, env, key):
        row = self.inspect(env, key)
        owner = self.account_owner(env)
        rows = self.account_store.list(owner)
        # The parent environment may show its complete saved account list. An
        # account's dedicated session environment must only expose that one
        # account, otherwise a child session could enumerate sibling accounts.
        if owner != env:
            rows = [a for a in rows if a.get('session_env_id') == env]
        # Reuse the product's vendored icons; no third-party image requests.
        source = Path(__file__).with_name('lan_seats') / 'lib/client.js'
        try:
            match = re.search(r'const PLATFORM_ICONS = (\{.*?\});', source.read_text(encoding='utf-8'), re.S)
            icons = json.loads(match.group(1)) if match else {}
        except (OSError, ValueError):
            icons = {}
        public = []
        from environment_accounts import catalog
        platforms = {p['name'].lower(): p for p in catalog()}
        for account in rows:
            item = {k: account.get(k, '') for k in
                    ('account_id', 'platform_id', 'platform', 'username', 'login', 'label', 'url', 'site')}
            inferred = platforms.get(str(item['platform']).lower(), {})
            icon = item['platform_id'] if item['platform_id'] in icons and item['platform_id'] != 'custom' else inferred.get('id', 'custom')
            item.update(has_password=bool(account.get('has_password')),
                        has_otp=bool(account.get('has_otp')),
                        icon=icons.get(icon, icons.get('custom', '')))
            item['can_open'] = bool(item['url'] or inferred.get('url'))
            public.append(item)
        return {'ok': True, 'accounts': public, 'allowed': bool(row.get('unlocked')),
                'env_id': env, 'owner_env_id': owner}

    def account_action(self, env, key, action, account_id, field='password'):
        row = self.inspect(env, key)
        owner = self.account_owner(env)
        account = self.account_store.get(owner, account_id)
        if owner != env and account.get('session_env_id') != env:
            raise ValueError('此账号不属于当前会话环境，请刷新账号列表')
        if action == 'credential':
            if field != 'password':
                raise ValueError('此页面仅允许查看密码，不返回验证器密钥。')
            if not account.get('password'):
                raise ValueError('此账号尚未保存密码，请在“我的环境”中编辑账号。')
            # This is deliberately a single-field, short-lived response. The
            # account list and 2FA endpoint never return the password or OTP
            # secret, and this value is never written to the audit/state files.
            now = time.time()
            return {'ok': True, 'field': 'password', 'value': account['password'],
                    'server_time': now, 'expires_at': now + 15}
        if action == 'totp':
            if not account.get('otp_secret'):
                raise ValueError('此账号尚未设置 2FA 验证器密钥，请在“我的环境”中编辑账号。')
            from environment_accounts import totp
            now = time.time()
            return {'ok': True, **totp(account['otp_secret'], now=now), 'server_time': now}
        if action != 'open':
            raise ValueError('账号操作无效。')
        if not row.get('unlocked') or row.get('restart_required'):
            raise ValueError('请先完成当前环境的代理检测和风险确认，再打开账号网站。')
        from environment_accounts import catalog
        platform = next((p for p in catalog() if p['id'] == account.get('platform_id') or
                         p['name'].lower() == str(account.get('platform', '')).lower()), {})
        url = account.get('url') or platform.get('url', '')
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('请先在“我的环境”中为此账号补充有效网站地址。')
        if not self.account_open_lock.acquire(blocking=False):
            raise ValueError('另一个账号正在打开，请稍后重试。')
        try:
            # Existing account service preserves its dedicated Cookie/session partition.
            reply = self.account_command({'env_id': owner, 'account_id': account_id, 'action': 'open'})
            if reply.get('session_env_id') == env:
                opened = self.command({'command': 'newtab', 'env_id': env, 'url': url})
                if not opened.get('ok'):
                    raise ValueError('账号网站未能打开，请重试。')
            return reply
        finally:
            self.account_open_lock.release()

    def inherited_check(self, env, row):
        """An account partition may reuse its own verified parent's network check."""
        owner = self.account_owner(env)
        if owner == env or row.get('restart_required'):
            return None
        with self.lock:
            parents = [(k, v) for k, v in self.jobs.items()
                       if k[0] == owner and v['result'].get('unlocked')]
        for (parent, key), job in reversed(parents):
            try:
                current = self.inspect(parent, key)
            except (ValueError, OSError):
                continue  # Parent closed; the account must run its own check.
            if (not current.get('unlocked') or current.get('restart_required') or
                    signature(current) != job['signature'] or signature(row) != signature(current)):
                continue
            result = {k: v for k, v in job['result'].items()
                      if k not in ('unlocked', 'decision', 'auto_open_url')}
            proxy = row.get('proxy', '')
            status = 'direct' if proxy in ('', 'direct') else 'dynamic' if row.get('ip_mode') == 'dynamic' else 'matched'
            if status == 'matched' and row.get('expected_exit_ip') != result.get('exit_ip'):
                if job['result'].get('decision') != 'continue_once':
                    continue
                status = 'mismatch'
            account = next((a for a in self.account_store.list(owner) if a.get('session_env_id') == env), None)
            if not account:
                return None
            from environment_accounts import catalog
            platform = next((p for p in catalog() if p['name'].lower() == str(account.get('platform', '')).lower()), {})
            url = account.get('url') or platform.get('url', '')
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                return None
            return {**result, 'status': status, 'allowed': True, 'inherited': True, 'auto_open_url': url}
        return None

    def _command(self, payload):
        import env_cli
        return env_cli.send(env_cli.resolve_profile_dir(self.profile), payload, 6)

    def inspect(self, env, key):
        from local_account_vault import valid_env_id
        if not valid_env_id(env) or not isinstance(key, str) or not 24 <= len(key) <= 128:
            raise ValueError('检测入口无效，请重新启动此环境。')
        row = self.command({'command': 'startup-check', 'action': 'inspect', 'env_id': env, 'key': key})
        if not row.get('ok'):
            raise ValueError(row.get('error', '检测入口已过期，请重新启动此环境。'))
        return row

    def start(self, env, key, force=False):
        row = self.inspect(env, key)
        identity = (env, key)
        with self.lock:
            # Expired capabilities never retain unbounded per-launch history.
            now = time.time()
            self.jobs = {k: v for k, v in self.jobs.items()
                         if now - v['created_at'] < 3600 or v['result']['status'] == 'checking'}
            previous = self.jobs.get(identity)
            if previous and previous['signature'] == signature(row) and (previous['result']['status'] == 'checking' or previous['result'].get('checking_network')):
                return self.public(previous)
            if not force and row.get('unlocked') and not row.get('restart_required'):
                if previous and previous['signature'] == signature(row) and previous['result'].get('unlocked'):
                    return self.public(previous)
                try:
                    saved = json.loads((self.profile / 'env-check' / f'{env}.json').read_text(encoding='utf-8'))
                    if (saved.get('check_version') == VERSION and not saved.get('checking_network') and
                            saved.get('decision') in ('pass', 'continue_once', 'dynamic', 'baseline')):
                        cached = {k: saved[k] for k in ('status','geo','probes','exit_ip','decision','ip_source','network_status','warning','ip_error') if k in saved}
                        cached.update(unlocked=True, allowed=True, expected_exit_ip=row.get('expected_exit_ip', ''))
                        job = {'signature': signature(row), 'result': cached, 'row': row,
                               'receipt': secrets.token_urlsafe(24), 'created_at': now}
                        self.jobs[identity] = job
                        return self.public(job)
                except (OSError, ValueError):
                    pass
            job = {'signature': signature(row), 'result': {'status': 'checking'}, 'row': row,
                   'receipt': secrets.token_urlsafe(24), 'created_at': time.time()}
            self.jobs[identity] = job
        threading.Thread(target=self._run, args=(identity, job), daemon=True, name='environment-startup-check').start()
        return self.public(job)

    @staticmethod
    def public(job):
        row = job['row']
        return {'ok': True, 'env_id': row['env_id'], 'display_name': row.get('display_name', row['env_id']),
                'ip_mode': row.get('ip_mode', 'static'), 'expected_exit_ip': row.get('expected_exit_ip', ''),
                'receipt': job['receipt'], **job['result']}

    def _report(self, env, job):
        atomic_json(self.profile / 'env-check' / f'{env}.json', {
            'env_id': env, 'reported_at': time.time(), 'check_version': VERSION,
            'ip_mode': job['row'].get('ip_mode', 'static'), **job['result']})

    def _run(self, identity, job):
        env, key = identity
        try:
            inherited = self.inherited_check(env, job['row'])
            direct = job['row'].get('proxy', '').strip() in ('', 'direct') and not job['row'].get('restart_required')
            if direct and not inherited:
                # Displaying direct-network information must not add a startup
                # gate or a static-IP risk decision to direct browsing.
                with self.lock:
                    job['result'] = {'status': 'direct', 'allowed': True, 'checking_network': True}
                self.decide(env, key, 'pass', job['receipt'], navigate=False)
            def progress(partial):
                with self.lock:
                    if self.jobs.get(identity) is job:
                        job['result'].update(partial, checking_network=True)
            result = inherited or network_check(job['row'], self.probe, progress)
            current = self.inspect(env, key)
            if signature(current) != job['signature']:
                raise ValueError('环境代理或 IP 设置已改变，请重新检测。')
            with self.lock:
                if self.jobs.get(identity) is not job:
                    return
                early_unlock = direct and job['result'].get('unlocked')
                job['result'] = result
                if early_unlock:
                    job['result'].update(unlocked=True, decision='pass')
                    try:
                        self._report(env, job)
                    except OSError:
                        job['result']['warning'] = '检测已完成，但本次检测记录未能保存。'
            if result.get('allowed') and not early_unlock:
                self.decide(env, key, 'continue_once' if result['status'] == 'mismatch' else 'pass', job['receipt'], navigate=False)
        except Exception as exc:
            with self.lock:
                if self.jobs.get(identity) is job:
                    job['result'] = {'status': 'error', 'error': str(exc)}

    def status(self, env, key):
        current = self.inspect(env, key)
        with self.lock:
            job = self.jobs.get((env, key))
            if not job:
                return {'ok': True, 'status': 'idle', 'display_name': current.get('display_name', env)}
            if signature(current) != job['signature']:
                return {'ok': True, 'status': 'stale', 'error': '环境配置已改变，请重新检测。'}
            return self.public(job)

    def decide(self, env, key, action, receipt='', navigate=True):
        row = self.inspect(env, key)
        if action == 'close':
            return self.command({'command': 'startup-check', 'env_id': env, 'key': key, 'action': 'close'})
        with self.lock:
            job = self.jobs.get((env, key))
            if not job or not secrets.compare_digest(str(receipt), job['receipt']):
                raise ValueError('检测结果已过期，请重新检测。')
            if signature(row) != job['signature'] or time.time() - job['created_at'] > 300:
                raise ValueError('检测结果或环境配置已改变，请重新检测。')
            result = job['result']
            permitted = {'matched': ('pass',), 'dynamic': ('pass',), 'direct': ('pass',),
                         'mismatch': ('continue_once', 'dynamic'), 'unrecorded': ('baseline', 'dynamic')}
            if action not in permitted.get(result.get('status'), ()):
                raise ValueError('当前检测结果不允许此操作，请重试检测或关闭环境。')
            reply = self.command({'command': 'startup-check', 'env_id': env, 'key': key,
                                  'action': 'unlock', 'decision': action, 'navigate': False,
                                  'exit_ip': result.get('exit_ip', ''),
                                  'proxy': row.get('proxy', ''), 'expected_exit_ip': row.get('expected_exit_ip', '')})
            if not reply.get('ok'):
                raise ValueError(reply.get('error', '操作失败，请重新检测。'))
            if action == 'dynamic':
                job['row']['ip_mode'] = 'dynamic'
            if action == 'baseline':
                job['row']['expected_exit_ip'] = result['exit_ip']
                job['result']['expected_exit_ip'] = result['exit_ip']
            job['signature'] = signature(job['row'])
            job['result']['unlocked'] = True
            job['result']['decision'] = action
            # Reports contain no proxy credentials or gate capability.
            warning = ''
            try:
                self._report(env, job)
            except OSError:
                warning = '已放行，但本次检测记录未能保存。'
            return {'ok': True, 'allowed': True, 'destination': row.get('destination', '') if navigate else '', 'warning': warning}


def make_server(profile, port=0, command=None, probe=probe_url, account_store=None, account_command=None):
    checks = StartupChecks(profile, command, probe, account_store, account_command)

    class Handler(BaseHTTPRequestHandler):
        def handle(self):
            try:
                super().handle()
            except ConnectionError:
                pass  # A closed environment may reset an idle browser connection.

        def log_message(self, *args):
            pass

        def send(self, value, status=200, html=False, javascript=False):
            body = value if html or javascript else json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/javascript; charset=utf-8' if javascript else 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            try:
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionError):
                pass  # Closing the environment can close its pending HTTP request.

        def do_GET(self):
            parsed = urlsplit(self.path)
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                return self.send({'ok': False}, 403)
            if parsed.path == '/health':
                return self.send({'ok': True, 'version': VERSION, 'profile': str(Path(profile).resolve()),
                                  'app': str(Path(__file__).resolve().parent), 'pid': os.getpid()})
            if parsed.path == '/client.js':
                return self.send(Path(__file__).with_name('env_check.js').read_bytes(), javascript=True)
            query = parse_qs(parsed.query)
            env, key = query.get('env', [''])[0], query.get('key', [''])[0]
            try:
                if parsed.path == '/':
                    checks.inspect(env, key)
                    return self.send(Path(__file__).with_name('env_check.html').read_bytes(), html=True)
                if parsed.path == '/status':
                    return self.send(checks.status(env, key))
                self.send({'ok': False, 'error': 'not found'}, 404)
            except (ValueError, OSError) as exc:
                self.send({'ok': False, 'error': str(exc)}, 409)

        def do_POST(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != urlsplit(origin).netloc or self.headers.get('Origin') != origin:
                return self.send({'ok': False, 'error': 'origin rejected'}, 403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    return self.send({'ok': False}, 413)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('请求格式无效')
                env, key = data.get('env', ''), data.get('key', '')
                if self.path == '/check':
                    return self.send(checks.start(env, key, force=data.get('force') is True))
                if self.path == '/decision':
                    return self.send(checks.decide(env, key, data.get('action'), data.get('receipt', '')))
                if self.path == '/accounts':
                    return self.send(checks.accounts(env, key))
                if self.path == '/account':
                    return self.send(checks.account_action(env, key, data.get('action'), data.get('account_id'), data.get('field', 'password')))
                self.send({'ok': False}, 404)
            except (ValueError, OSError) as exc:
                self.send({'ok': False, 'error': str(exc)}, 409)

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.checks = checks
    return server


def ensure_service(profile):
    from _nowindow import console_python, detached_no_window, hidden_startup_info
    profile = Path(profile).resolve()
    state_file = profile / '.env-check-service.json'
    def health():
        try:
            state = json.loads(state_file.read_text(encoding='utf-8'))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{int(state['port'])}/health", timeout=0.3) as response:
                row = json.load(response)
            if (row.get('version') == VERSION and row.get('profile') == str(profile)
                    and row.get('app') == str(Path(__file__).resolve().parent)):
                return state
        except (OSError, ValueError, KeyError):
            pass
        return None
    with startup_lock(profile / '.env-check-service.lock', timeout=6):
        state = health()
        if state:
            return state
        log_path = profile / 'logs' / 'environment-startup-check.log'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('ab') as log:
            child = subprocess.Popen([console_python(), '-B', str(Path(__file__).resolve()), '--profile', str(profile)],
                cwd=str(Path(__file__).resolve().parent), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                creationflags=detached_no_window(), startupinfo=hidden_startup_info())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = health()
            if state and state.get('pid') == child.pid:
                return state
            if child.poll() is not None:
                break
            time.sleep(0.04)
        if child.poll() is None:
            child.terminate()
        raise RuntimeError('环境检测服务未能启动，请重试启动浏览器。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    server = make_server(args.profile, port=args.port)
    atomic_json(args.profile / '.env-check-service.json', {'port': server.server_port, 'pid': os.getpid(), 'version': VERSION})
    server.serve_forever()


if __name__ == '__main__':
    main()
