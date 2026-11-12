"""Four-field proxy editor backed by encrypted, persistent local auth relays.

Only a loopback URL reaches Chromium. Credentials never enter envs.json,
configuration backups, command lines, relay state files or API list responses.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from local_account_vault import _dpapi
from _runtime_state import atomic_json, process_identity, startup_lock

_CHILDREN = {}


class ProxySettings:
    def __init__(self, profile):
        self.profile = Path(profile)
        self.path = self.profile / 'network-proxies.dpapi.json'
        self.directory = self.profile / 'network-relays'
        self.lock = threading.RLock()

    def read(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if data.get('version') != 1:
            raise ValueError('代理凭据格式不兼容')
        return json.loads(_dpapi(base64.b64decode(data['ciphertext'], validate=True), protect=False))

    def write(self, entries):
        raw = json.dumps(entries, ensure_ascii=False).encode('utf-8')
        atomic_json(self.path, {'version': 1, 'ciphertext': base64.b64encode(_dpapi(raw, protect=True)).decode('ascii')})

    @staticmethod
    def bare(proxy):
        try:
            u = urlsplit(proxy)
            return {'host': u.hostname or '', 'port': str(u.port or (80 if u.scheme == 'http' else 443 if u.scheme == 'https' else '')),
                    'username': '', 'password': '', 'has_password': False}
        except ValueError:
            return {'host': '', 'port': '', 'username': '', 'password': '', 'has_password': False}

    def fields(self, proxy, entries=None):
        entries = self.read() if entries is None else entries
        record = next((r for r in entries.values() if r['proxy'] == proxy), None)
        if not record:
            return self.bare(proxy)
        return {**{k: str(record['settings'].get(k, '')) for k in ('host', 'port', 'username')},
                'password': '', 'has_password': bool(record['settings'].get('password'))}

    def normalize(self, fields, original='direct'):
        from env_management import ManagementError
        from lan_bridge import validate_proxy_config
        if not isinstance(fields, dict):
            raise ManagementError('代理配置无效', fields={'proxy': '请填写 IP、端口、用户和密码'})
        for key in ('host', 'port', 'username', 'password'):
            if not isinstance(fields.get(key, ''), str) or len(fields.get(key, '')) > 2048:
                raise ManagementError('代理配置无效', fields={'proxy': '代理内容无效或过长'})
        host, port = fields.get('host', '').strip(), fields.get('port', '').strip()
        username, password = fields.get('username', ''), fields.get('password', '')
        old = next((r['settings'] for r in self.read().values() if r['proxy'] == original), {})
        if username and fields.get('password_action', 'keep') == 'keep' and not password:
            password = old.get('password', '')
        if not host and not port and not username and not password:
            return {'proxy': 'system' if original == 'system' and not fields.get('edited') else 'direct'}
        if not host or not port or not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ManagementError('请检查代理配置', fields={'proxy': 'IP 和端口必须同时填写；端口为 1–65535'})
        # Keep the established scheme for existing proxies; new four-field entries use HTTP.
        scheme = old.get('scheme') or urlsplit(original).scheme or 'http'
        if scheme not in ('http', 'https', 'socks4', 'socks5'):
            scheme = 'http'
        host = host.strip('[]')
        if ':' in host:
            try:
                host = str(ipaddress.IPv6Address(host))
            except ValueError:
                raise ManagementError('请检查代理配置', fields={'proxy': '请输入有效的 IP 或主机名'}) from None
        bare = f'{scheme}://{"["+host+"]" if ":" in host else host}:{int(port)}'
        try:
            bare = validate_proxy_config(bare)
        except ValueError as exc:
            raise ManagementError('请检查代理配置', fields={'proxy': str(exc)}) from None
        if any(c in username + password for c in '\r\n\x00') or bool(username) != bool(password):
            raise ManagementError('请检查代理配置', fields={'proxy': '用户和密码须同时填写，或同时留空'})
        if username and (scheme == 'socks4' or ':' in username):
            raise ManagementError('请检查代理配置', fields={'proxy': '此代理认证格式不受支持'})
        if username and scheme == 'socks5' and (len(username.encode()) > 255 or len(password.encode()) > 255):
            raise ManagementError('请检查代理配置', fields={'proxy': 'SOCKS5 用户和密码各不能超过 255 字节'})
        return {'proxy': bare, 'scheme': scheme, 'host': host, 'port': str(int(port)), 'username': username, 'password': password}

    def prepare(self, settings):
        if not settings.get('username'):
            return settings['proxy']
        with self.lock, startup_lock(self.profile / '.network-proxy.lock'):
            entries = self.read()
            for key, record in entries.items():
                if record['settings'] == settings:
                    self.ensure(key, record)
                    return record['proxy']
            # Reserve the address briefly; the child must bind this exact port.
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            key = secrets.token_hex(16)
            scheme = 'socks5' if settings['scheme'] == 'socks5' else 'http'
            record = {'settings': settings, 'proxy': f'{scheme}://127.0.0.1:{port}'}
            entries[key] = record
            self.write(entries)
            self.ensure(key, record)
            return record['proxy']

    def ensure(self, key, record):
        from _nowindow import console_python, detached_no_window, hidden_startup_info
        from env_management import ManagementError
        self.directory.mkdir(parents=True, exist_ok=True)
        state_file = self.directory / (key + '.json')
        with startup_lock(self.directory / (key + '.lock')):
            try:
                state = json.loads(state_file.read_text(encoding='utf-8'))
                identity = process_identity(state['pid'])
                if identity.get('alive') and identity.get('started') == state.get('started') and state.get('proxy') == record['proxy']:
                    with socket.create_connection(('127.0.0.1', urlsplit(record['proxy']).port), timeout=.2):
                        return
            except (OSError, ValueError, KeyError):
                pass
            state_file.unlink(missing_ok=True)
            child = subprocess.Popen([console_python(), str(Path(__file__).resolve()), '--serve', key, '--profile', str(self.profile)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=detached_no_window(), startupinfo=hidden_startup_info())
            for pid, process in list(_CHILDREN.items()):
                if process.poll() is not None:
                    del _CHILDREN[pid]
            _CHILDREN[child.pid] = child
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and child.poll() is None:
                if state_file.exists():
                    state = json.loads(state_file.read_text(encoding='utf-8'))
                    if state.get('pid') == child.pid and state.get('proxy') == record['proxy']:
                        return
                time.sleep(.025)
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            raise ManagementError('代理认证服务启动失败，请重试；未切换为直连', 503, fields={'proxy': '本机代理认证服务暂不可用'})

    def restore(self, proxies):
        wanted = set(proxies)
        restored = []
        if not self.path.exists():
            return restored
        with self.lock:
            for key, record in self.read().items():
                if record['proxy'] in wanted:
                    self.ensure(key, record)
                    restored.append(record['proxy'])
        return restored

    def serve(self, key):
        from fp_proxy import ProxySpec, Relay
        record = self.read()[key]
        s = record['settings']
        relay = Relay(ProxySpec(mode=s['scheme'], host=s['host'], port=int(s['port']), username=s['username'], password=s['password']),
                      listen_port=urlsplit(record['proxy']).port)
        relay._start()
        identity = process_identity(os.getpid())
        atomic_json(self.directory / (key + '.json'), {'pid': os.getpid(), 'started': identity.get('started'), 'proxy': record['proxy']})
        try:
            while relay._thread.is_alive():
                relay._thread.join(timeout=60)
        finally:
            relay._stop()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', required=True)
    parser.add_argument('--profile', required=True)
    args = parser.parse_args()
    ProxySettings(args.profile).serve(args.serve)
