"""Local environment management. No page activation, cookie reads or storage deletion.

Native envs.json is owned by Chromium. All definition writes use its command
channel; versioned management metadata is separate so native saves cannot erase it.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

from local_account_vault import valid_env_id
from environment_workspace import WorkspaceServices

API_VERSION = 2
SCHEMA_VERSION = 1
FIELDS = ('env_id', 'display_name', 'group', 'tags', 'notes', 'proxy',
          'expected_exit_ip', 'locale', 'accept_languages', 'fingerprint_seed',
          'start_url', 'homepage')
RESTART_FIELDS = {'proxy', 'locale', 'accept_languages', 'fingerprint_seed'}
LIMITS = {'display_name': 120, 'group': 80, 'tags': 300, 'notes': 2048,
          'fingerprint_seed': 128, 'locale': 40, 'accept_languages': 200}


class ManagementError(ValueError):
    def __init__(self, message, status=400, fields=None, **details):
        super().__init__(message)
        self.status = status
        self.reply = {'ok': False, 'error': message, 'field_errors': fields or {}, **details}


def definition(row):
    return {key: str(row.get(key) or '') for key in FIELDS}


def ip_key(value):
    value = str(value or '').strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value


def revision(row):
    return hashlib.sha256(json.dumps(definition(row), sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()[:24]


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.env-management-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def validate_definition(payload, current=None):
    if not isinstance(payload, dict):
        raise ManagementError('环境配置必须是对象')
    row = definition(current or {})
    errors = {}
    for key in FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str) or len(value) > LIMITS.get(key, 2048):
            errors[key] = f'请输入文字，最多 {LIMITS.get(key, 2048)} 个字符'
        else:
            row[key] = value.strip()
    if not valid_env_id(row['env_id']):
        errors['env_id'] = '编号只能使用字母、数字、- 和 _，长度 1–64'
    if not row['display_name']:
        errors['display_name'] = '请填写环境名称'
    try:
        # Retain the existing shared proxy parser for old and new clients.
        from lan_bridge import validate_proxy_config
        row['proxy'] = validate_proxy_config(row['proxy'])
    except ValueError as exc:
        errors['proxy'] = str(exc)
    for key in ('start_url', 'homepage'):
        if row[key]:
            try:
                parsed = urlsplit(row[key])
                if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                        parsed.username is not None or parsed.password is not None or
                        any(ch.isspace() for ch in row[key])):
                    raise ValueError()
                _ = parsed.port
            except ValueError:
                errors[key] = '请输入完整的 http:// 或 https:// 网页地址（不含账号密码）'
    if row['locale'] and not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', row['locale']):
        errors['locale'] = '例如 zh-CN 或 en-US'
    if row['accept_languages'] and not re.fullmatch(
            r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*(?:\s*,\s*[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)*',
            row['accept_languages']):
        errors['accept_languages'] = '用逗号分隔语言，例如 zh-CN,zh,en-US,en'
    if row['expected_exit_ip']:
        try:
            ipaddress.ip_address(row['expected_exit_ip'])
        except ValueError:
            errors['expected_exit_ip'] = '请输入有效的 IPv4 或 IPv6 地址'
    if errors:
        raise ManagementError('请检查标出的配置项', fields=errors)
    return row


def validate_account(account):
    if not isinstance(account, dict):
        raise ManagementError('账号资料格式无效')
    limits = {'platform': 80, 'label': 120, 'username': 240, 'password': 2048, 'otp_secret': 256}
    errors = {}
    for key, value in account.items():
        if key not in limits or not isinstance(value, str) or len(value) > limits.get(key, 0):
            errors['account.' + key] = '账号字段无效或过长'
    otp = account.get('otp_secret', '').replace(' ', '')
    if otp and not re.fullmatch(r'[A-Za-z2-7]+=*', otp):
        errors['account.otp_secret'] = 'OTP 密钥应为 Base32 字符，不是一次性验证码'
    if errors:
        raise ManagementError('请检查账号资料', fields=errors)
    return account


class EnvironmentManagement(WorkspaceServices):
    def __init__(self, bridge, command=None, probe=None, background_live=True):
        self.bridge = bridge
        self.profile = Path(bridge.account_vault.path).parent
        self.path = self.profile / 'env-management.json'
        self.backup_dir = self.profile / 'env-config-backups'
        self.lock = threading.RLock()
        self.job_lock = threading.RLock()
        self.jobs = {}
        self.last_active = None
        self._command = command
        self._probe = probe or self.probe_proxy
        self.background_live = background_live
        self._live_guard = threading.Lock()
        self._live_snapshot = None
        self._live_finished = 0
        self._live_running = False
        self._live_error = False
        self._live_generation = 0
        self._pending_proxy_tests = {}
        from environment_status import ProxyHealth
        self.proxy_health = ProxyHealth(probe or self.probe_proxy_health)
        from environment_import import EnvironmentImports
        self.imports = EnvironmentImports(self)

    @staticmethod
    def probe_proxy_health(proxy):
        import requests
        proxy = proxy.replace('socks5://', 'socks5h://').replace('socks4://', 'socks4a://')
        with requests.Session() as session:
            session.trust_env = False
            with session.get('https://api.ipify.org?format=json', proxies={'http': proxy, 'https': proxy}, timeout=(3, 5)) as response:
                response.raise_for_status()
                return {'ok': True, 'exit_ip': str(ipaddress.ip_address(response.json()['ip']))}

    def runtime_status(self, payload=None):
        from environment_status import project_status, aggregate_status
        with self.lock:
            disk = self._state()
            meta = self._meta()
            accounts = self._workspace().metadata()
        # Request only the active id and environment ids that currently own a
        # live tab. The regular `list` response includes every URL/title/DevTools
        # target and is needlessly expensive for indicator refreshes.
        try:
            runtime = self.bridge.backend.runtime_state(max_age=1)
            live_status = 'ready'
            known = True
            active_session = runtime.get('active', '')
            running_ids = set(runtime.get('running_envs', []))
        except Exception:
            live_status = 'offline'
            known = False
            active_session = ''
            running_ids = set()
        parents = {a['session_env_id']: env for env, bucket in accounts.items()
                   for a in bucket.get('accounts', []) if a.get('session_env_id')}
        parents.update({child: env for env, bucket in accounts.items() for child in bucket.get('retained_sessions', [])})
        tracker = getattr(self.bridge, 'agent_activity', None)
        # Preserve Agent completion markers locally; a refresh must not scan all
        # live environments merely to decide which historical markers to prune.
        agents = tracker.snapshot(None) if tracker else {}
        running_definitions = [row for row in disk['envs'] if row.get('env_id') in running_ids]
        candidates = list(dict.fromkeys((row.get('proxy', ''), ip_key(row.get('expected_exit_ip', '')))
                                        for row in running_definitions
                                        if row.get('proxy') not in ('', None, 'direct', 'system')))
        # A status refresh reads cached proxy evidence only; it never starts probes.
        probes = self.proxy_health.snapshot(candidates)
        states = {}
        now = time.time()
        for row in disk['envs']:
            env = row['env_id']
            current = row if env in running_ids else None
            proxy = row.get('proxy', '')
            expected = ip_key(row.get('expected_exit_ip', ''))
            test = meta['envs'].get(parents.get(env, env), {}).get('proxy_test') or {}
            fresh = (test.get('proxy') == proxy and ip_key(test.get('expected_exit_ip', '')) == expected
                     and now-test.get('at', 0) <= 900)
            probe = probes.get((proxy, expected)) or {} if env in running_ids else {}
            latest = probe if probe.get('at', 0) > (test.get('at', 0) if fresh else 0) else test if fresh else {}
            network = latest.get('status', 'unchecked')
            agent = agents.get(env, {})
            if agent.get('network_error') and agent.get('network_error_at', agent.get('updated_at', 0)) > latest.get('at', 0):
                network = 'failed'
            states[env] = project_status({'tabs': [{}]} if current and known else None, known, network, agent)
        # Parent environments aggregate running account sessions; child rows keep their own status.
        for parent, bucket in accounts.items():
            if parent not in states:
                continue
            children = [a.get('session_env_id') for a in bucket.get('accounts', []) if a.get('session_env_id') != parent]
            children.extend(bucket.get('retained_sessions', []))
            members = [states[x] for x in dict.fromkeys([parent, *children]) if x in states]
            states[parent] = aggregate_status(members, known)
        return {'ok': True, 'live_status': live_status, 'active': parents.get(active_session, active_session) if known else '',
                'active_session': active_session if known else '', 'states': states, 'checked_at': time.time()}

    def _invalidate_live(self):
        self.bridge.backend._live_cache = None
        self.bridge.backend._runtime_cache = None
        with self._live_guard:
            self._live_generation += 1
            self._live_snapshot = None
            self._live_finished = 0

    def _refresh_live(self, generation):
        try:
            snapshot = self.bridge.backend.live_state(max_age=3)
            if not isinstance(snapshot.get('envs'), list) or snapshot.get('ok') is False:
                raise ValueError('invalid native state')
            failed = False
        except Exception:
            snapshot, failed = None, True
        with self._live_guard:
            self._live_running = False
            if generation == self._live_generation:
                self._live_snapshot = snapshot
                self._live_error = failed
                self._live_finished = time.monotonic()

    def _live_state(self):
        if not self.background_live:
            try:
                return self.bridge.backend.live_state(max_age=3), 'ready'
            except Exception:
                return None, 'offline'
        with self._live_guard:
            interval = 5 if self._live_error else 3
            if (not self._live_running and
                    (not self._live_finished or time.monotonic() - self._live_finished >= interval)):
                self._live_running = True
                threading.Thread(target=self._refresh_live, args=(self._live_generation,),
                                 name='environment-live-refresh', daemon=True).start()
            status = 'offline' if self._live_error else 'ready' if self._live_snapshot else 'connecting'
            return copy.deepcopy(self._live_snapshot), status

    def _meta(self):
        if not self.path.exists():
            return {'schema_version': SCHEMA_VERSION, 'envs': {}, 'events': []}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        version = data.get('schema_version', 0)
        if version == 0:
            # v0 stored entries but no history; migration is written on mutation.
            data = {'schema_version': 1, 'envs': data.get('envs', {}), 'events': data.get('events', [])}
        if (data.get('schema_version') != SCHEMA_VERSION or not isinstance(data.get('envs'), dict)
                or not isinstance(data.get('events'), list)):
            raise ManagementError('管理数据版本不兼容，请升级或从配置备份恢复', 409)
        return data

    def _write_meta(self, meta):
        atomic_json(self.path, meta)

    def _record(self, action, env_id='', **detail):
        meta = self._meta()
        meta['events'] = [{'id': secrets.token_hex(8), 'at': time.time(),
                           'action': action, 'env_id': env_id, **detail}, *meta['events']][:300]
        if env_id and action in ('create','edit','account_save','account_clear'):
            meta['envs'].setdefault(env_id, {})['updated_at'] = time.time()
        self._write_meta(meta)

    def _send(self, command):
        if self._command:
            return self._command(command)
        try:
            # Never leave an offline mutation in the native file slot to execute
            # unexpectedly when the user launches the browser at a later time.
            self.bridge.backend.live_state(max_age=1)
        except Exception:
            raise ManagementError('浏览器尚未启动或暂时未响应，请启动蓝豆浏览器后重试；当前输入已保留',
                                  503, outcome_unknown=False)
        import env_cli
        return env_cli.send(env_cli.resolve_profile_dir(self.bridge.udd), command, 20)

    def _state(self):
        state = self.bridge.backend.env_state()
        if not isinstance(state.get('envs'), list):
            raise ManagementError('环境文件格式损坏，未执行写入', 409)
        return state

    def list(self):
        # Slow native I/O runs outside the metadata lock, coalesced in one worker.
        # A stopped or frozen browser must not delay reading saved definitions.
        live, live_status = self._live_state()
        with self.lock:
            disk = self._state()
            meta = self._meta()
            warning = ''
            if live is None:
                live = disk
                live_rows = {}
                warning = ('正在连接浏览器，先显示本地配置' if live_status == 'connecting' else
                           '浏览器暂时未响应，当前显示本地配置；连接恢复后会自动更新')
            else:
                live_rows = {e['env_id']: e for e in live.get('envs', [])}
            internal = self.internal_session_ids()
            ids = [e['env_id'] for e in disk['envs'] if e['env_id'] not in internal]
            accounts_v2 = self._workspace().metadata()
            account_warning = ''
            try:
                accounts = self.bridge.account_vault.summaries(ids)
            except Exception:
                accounts = {}
                account_warning = '本机账号库暂时不可读；环境配置仍可使用，账号操作请稍后重试'
            rows = []
            for entry in disk['envs']:
                if entry['env_id'] in internal: continue
                row = {**live_rows.get(entry['env_id'], {}), **definition(entry)}
                row.update(accounts.get(entry['env_id'], {'has_account': False}))
                row['accounts'] = [self._workspace().public(a) for a in accounts_v2.get(entry['env_id'], {}).get('accounts', [])]
                row['account_count'] = len(row['accounts'])
                row['has_account'] = bool(row['accounts'])
                row.update(meta['envs'].get(entry['env_id'], {}))
                row['restart_pending'] = bool(row.get('restart_pending_pid') and
                                              row['restart_pending_pid'] == self._browser_pid())
                row['revision'] = revision(entry)
                test = row.get('proxy_test')
                if test and (time.time()-test.get('at',0)>900 or test.get('proxy') != row['proxy'] or
                             ip_key(test.get('expected_exit_ip', '')) != ip_key(row['expected_exit_ip'])):
                    row['proxy_test'] = {**test, 'stale': True}
                rows.append(row)
            active = self.session_parent(live.get('active', disk.get('active', '')))
            if not warning and active and active != self.last_active:
                stamp = time.time()
                meta['envs'].setdefault(active, {})['last_used_at'] = stamp
                try:
                    self._write_meta(meta)
                    self.last_active = active
                except OSError:
                    warning = '环境列表可用，但最近使用记录写入失败，请检查磁盘空间与权限'
                for row in rows:
                    if row['env_id'] == active:
                        row['last_used_at'] = stamp
            return {'ok': True, 'api_version': API_VERSION, 'schema_version': SCHEMA_VERSION,
                    'active': active, 'envs': rows,
                    'warning': warning, 'account_warning': account_warning, 'live_status': live_status,
                    'workspace_version':3,'platforms': __import__('environment_accounts').catalog(),
                    'capabilities': {'proxy_test': ['http', 'https','socks4','socks5'], 'proxy_auth': False,
                                     'account_encryption': 'Windows DPAPI', 'cloud_sync': False}}

    def save(self, payload):
        with self.lock:
            self._meta()  # Reject unknown metadata versions before native writes.
            state = self._state()
            current = next((e for e in state['envs'] if e['env_id'] == payload.get('env_id')), None)
            row = validate_definition(payload, current)
            if payload.get('create_only') and current:
                raise ManagementError('环境编号已存在', 409, {'env_id': '请换一个环境编号'})
            if not payload.get('create_only') and not current:
                raise ManagementError('环境已被移除，请返回列表重新选择', 409)
            if current and payload.get('revision') != revision(current):
                raise ManagementError('此环境已被其他页面修改，请重新载入后再保存', 409, conflict=True)
            account = payload.get('account')
            if account is not None:
                validate_account(account)
            changed = [key for key in FIELDS if row[key] != definition(current or {}).get(key)]
            if not current and row['env_id'] in self._meta()['envs'] and not payload.get('relink_preserved'):
                raise ManagementError('此编号保留着旧环境资料，请确认找回旧资料或使用自动生成的新编号',409,relink_required=True)
            if 'display_name' in changed and any(e['env_id'] != row['env_id'] and e.get('display_name', '').casefold() == row['display_name'].casefold()
                   for e in state['envs']) and not payload.get('allow_duplicate_name'):
                raise ManagementError('已有同名环境，可修改名称或明确允许同名', 409,
                                      {'display_name': '环境名称重复'}, duplicate_name=True)
            persisted = current
            if changed:
                try:
                    reply = self._send({'command': 'create', **row})
                except ManagementError:
                    raise
                except Exception:
                    # Native command may have committed before its response was lost.
                    # Read back instead of replaying a mutation on a timeout.
                    reply = {'dispatched': True}
                persisted = next((e for e in self._state()['envs'] if e['env_id'] == row['env_id']), None)
                if not persisted or definition(persisted) != row:
                    raise ManagementError('保存结果尚未确认，请刷新列表核实，勿反复提交', 503,
                                          outcome_unknown=reply.get('dispatched', True))
                self._invalidate_live()
            result = {'ok': True, 'config_saved': True, 'env_id': row['env_id'],
                      'config_changed': bool(changed),
                      'revision': revision(persisted), 'restart_required': sorted(RESTART_FIELDS.intersection(changed)),
                      'account_saved': account is None}
            if changed:
                meta = self._meta()
                info = meta['envs'].setdefault(row['env_id'], {})
                if current:
                    info['versions']=[{'id':secrets.token_hex(8),'at':time.time(),'config':definition(current)},*info.get('versions',[])][:30]
                info.setdefault('created_at', time.time())
                if current and RESTART_FIELDS.intersection(changed):
                    # A browser PID check clears this pending indication after a restart.
                    info['restart_pending_pid'] = self._browser_pid()
                try:
                    self._write_meta(meta)
                    self._record('create' if not current else 'edit', row['env_id'], fields=changed)
                except OSError:
                    result['warning'] = '配置已保存，但维护记录写入失败；请检查本机磁盘空间与权限'
            if account is not None:
                try:
                    result['account'] = self.bridge.account_vault.save(row['env_id'], account)
                    result['account_saved'] = True
                except Exception:
                    result.update(ok=False, partial=True, error=(
                        '环境配置已保存，账号加密保存失败；输入已保留，可仅重试账号资料' if changed else
                        '账号加密保存失败；环境配置未变，输入已保留，可仅重试账号资料'))
                else:
                    try:
                        self._record('account_save', row['env_id'])
                    except OSError:
                        result['warning'] = ('配置与账号已保存，但维护记录写入失败；请检查本机磁盘空间与权限' if changed else
                                             '账号已保存，但维护记录写入失败；请检查本机磁盘空间与权限')
            return result

    @staticmethod
    def _browser_pid():
        try:
            return json.loads((Path(__file__).parent / '.env-ui-port.json').read_text(encoding='utf-8')).get('pid')
        except (OSError, ValueError):
            return None

    def account(self, payload):
        with self.lock:
            env_id = payload.get('env_id')
            if env_id not in {e['env_id'] for e in self._state()['envs']}:
                raise ManagementError('环境不存在', 404)
            if payload.get('action') == 'clear':
                self.bridge.account_vault.delete(env_id)
                reply = {'ok': True, 'account': {'has_account': False}}
                try:
                    self._record('account_clear', env_id)
                except OSError:
                    reply['warning'] = '账号已清除，但维护记录写入失败'
                return reply
            account = validate_account(payload.get('account'))
            result = self.bridge.account_vault.save(env_id, account)
            reply = {'ok': True, 'account': result}
            try:
                self._record('account_save', env_id)
            except OSError:
                reply['warning'] = '账号已保存，但维护记录写入失败'
            return reply

    def remove(self, payload):
        with self.lock:
            state = self._state()
            row = next((e for e in state['envs'] if e['env_id'] == payload.get('env_id')), None)
            if row is None:
                raise ManagementError('环境不存在', 404)
            if state.get('active') == row['env_id']:
                raise ManagementError('请先切换到其他环境，再移除此环境')
            if payload.get('revision') != revision(row):
                raise ManagementError('环境已更新，请刷新列表后再移除', 409)
            backup = self.backup({'reason': 'before-remove'})
            try:
                reply = self._send({'command': 'delete', 'env_id': row['env_id']})
            except ManagementError:
                raise
            except Exception:
                reply = {'dispatched': True}
            if any(e['env_id'] == row['env_id'] for e in self._state()['envs']):
                raise ManagementError('移除尚未确认，请刷新核实；如仍有此环境的标签页，请先关闭再重试', 503,
                                      outcome_unknown=reply.get('dispatched', False))
            self._invalidate_live()
            result = {'ok': True, 'storage_preserved': True, 'account_preserved': True}
            try:
                self._record('remove_definition', row['env_id'], backup_id=backup['backup']['id'])
            except OSError:
                result['warning'] = '环境定义已移除且备份已保留，但维护记录写入失败'
            return result

    def backup(self, payload):
        with self.lock:
            internal=self.internal_session_ids()
            document = {'format': 'bluebean-environment-config', 'schema_version': 1,
                        'created_at': time.time(), 'reason': str(payload.get('reason', 'manual'))[:80],
                        'includes': 'configuration-only',
                        'envs': [definition(e) for e in self._state()['envs'] if e['env_id'] not in internal]}
            backup_id = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4)
            path = self.backup_dir / (backup_id + '.json')
            atomic_json(path, document)
            self._record('backup', backup_id=backup_id, count=len(document['envs']))
            return {'ok': True, 'backup': {'id': backup_id, 'created_at': document['created_at'],
                                           'count': len(document['envs']), 'path': str(path)},
                    'document': document}

    def _backup_document(self, payload):
        if 'document' in payload:
            document = payload['document']
        else:
            backup_id = payload.get('backup_id', '')
            if not isinstance(backup_id, str) or not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{8}', backup_id):
                raise ManagementError('备份编号无效')
            path = self.backup_dir / (backup_id + '.json')
            if not path.is_file():
                raise ManagementError('备份不存在', 404)
            document = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(document, dict) or document.get('format') != 'bluebean-environment-config'
                or document.get('schema_version') != 1 or not isinstance(document.get('envs'), list)
                or len(document['envs']) > 5000):
            raise ManagementError('备份格式或版本不兼容')
        rows = []
        seen = set()
        for entry in document['envs']:
            row = validate_definition(entry)
            if row['env_id'] in seen:
                raise ManagementError('备份内存在重复编号，未执行恢复')
            seen.add(row['env_id'])
            rows.append(row)
        return rows

    def restore(self, payload):
        with self.lock:
            rows = self._backup_document(payload)
            current = {e['env_id']: e for e in self._state()['envs']}
            digest = hashlib.sha256(json.dumps([rows, sorted((k, revision(v)) for k, v in current.items())],
                                              sort_keys=True).encode()).hexdigest()
            changes = [{'env_id': row['env_id'], 'display_name': row['display_name'],
                        'action': 'update' if row['env_id'] in current else 'create',
                        'fields': [key for key in FIELDS if row[key] != definition(current.get(row['env_id'], {}))[key]],
                        'before':definition(current.get(row['env_id'],{})),'after':definition(row)}
                       for row in rows if row['env_id'] not in current or revision(row) != revision(current[row['env_id']])]
            if payload.get('action') != 'apply':
                return {'ok': True, 'preview_token': digest, 'changes': changes,
                        'unchanged': len(rows) - len(changes), 'storage_preserved': True}
            if payload.get('preview_token') != digest:
                raise ManagementError('配置或备份已变化，请重新预览恢复内容', 409)
            selected = payload.get('env_ids')
            change_ids = {c['env_id'] for c in changes}
            if (not isinstance(selected, list) or not selected or
                    not all(isinstance(item, str) for item in selected) or not set(selected) <= change_ids):
                raise ManagementError('请在预览中选择要恢复的环境')
            before = self.backup({'reason': 'before-restore'})
            results = []
            for row in rows:
                if row['env_id'] not in selected:
                    continue
                # Reuse validated, revision-checked native saves; never activate.
                try:
                    existing = next((e for e in self._state()['envs'] if e['env_id'] == row['env_id']), None)
                    saved = self.save({**row, 'create_only': existing is None,
                                       'revision': revision(existing) if existing else None,
                                       'allow_duplicate_name': True,'relink_preserved':True})
                    results.append({'env_id': row['env_id'], 'ok': saved['ok']})
                except Exception:
                    results.append({'env_id': row['env_id'], 'ok': False, 'error': '恢复未确认，请检查环境详情'})
                    break
            self._record('restore', results=results, backup_id=before['backup']['id'])
            return {'ok': len(results) == len(set(selected)) and all(r['ok'] for r in results),
                    'results': results, 'before_backup': before['backup'], 'storage_preserved': True,
                    'active_preserved': True, 'restart_required': True}

    def maintenance(self):
        with self.lock:
            state = self._state()
            meta = self._meta()
            issues = []
            for key, label in [('display_name', '名称'), ('fingerprint_seed', '指纹种子'), ('proxy', '代理')]:
                groups = {}
                for row in state['envs']:
                    value = row.get(key)
                    if value and value not in ('direct', 'system'):
                        groups.setdefault(value, []).append(row['env_id'])
                for ids in groups.values():
                    if len(ids) > 1:
                        issues.append({'kind': 'shared_' + key, 'message': '多个环境使用相同' + label, 'env_ids': ids})
            for row in state['envs']:
                try:
                    validate_definition(row)
                except ManagementError as exc:
                    issues.append({'kind': 'invalid_config', 'message': '配置项需要检查',
                                   'env_ids': [row['env_id']], 'fields': exc.reply['field_errors']})
            known = {e['env_id'] for e in state['envs']}
            detached = sorted(set(meta['envs']) - known)
            backups = []
            if self.backup_dir.exists():
                for path in sorted(self.backup_dir.glob('*.json'), reverse=True)[:50]:
                    try:
                        doc = json.loads(path.read_text(encoding='utf-8'))
                        backups.append({'id': path.stem, 'created_at': doc['created_at'],
                                        'count': len(doc['envs']), 'reason': doc.get('reason', '')})
                    except (OSError, ValueError, KeyError, TypeError):
                        issues.append({'kind': 'invalid_backup', 'message': '存在无法读取的备份', 'env_ids': []})
            return {'ok': True, 'schema_version': SCHEMA_VERSION, 'issues': issues,
                    'detached_definitions': detached, 'backups': backups, 'events': meta['events'][:100],
                    'backup_directory': str(self.backup_dir),
                    'note': '检查只读取配置；同代理/种子是提醒，不代表故障。移除的环境可从备份恢复，账号与网站数据仍保留。'}

    def fingerprint(self, payload):
        """Return one environment's browser-visible fingerprint without secrets.

        The configured section is derived deterministically from the environment's
        fingerprint seed.  A running environment is probed through its own CDP tab;
        no page storage, cookies, credentials, or login state is ever queried.
        """
        env_id = str((payload or {}).get('env_id') or '').strip()
        if not env_id:
            raise ManagementError('请提供环境编号', fields={'env_id': '环境编号不能为空'})
        with self.lock:
            state = self._state()
            row = next((entry for entry in state['envs'] if entry.get('env_id') == env_id), None)
            if not row:
                raise ManagementError('环境不存在', 404, {'env_id': '请使用准确的环境编号'})
            meta = self._meta()
        from fp_launcher import Fingerprint
        import platform as host_platform
        import re as _re

        seed = str(row.get('fingerprint_seed') or f'env:{env_id}')
        fp = Fingerprint(seed)
        configured = fp.as_dict()
        configured_languages = str(row.get('accept_languages') or configured.get('languages') or '')
        configured_locale = str(row.get('locale') or configured.get('ui_language') or '')
        version_file = Path(r'E:\chromium\src\chrome\VERSION')
        version_parts = {}
        if version_file.exists():
            try:
                for line in version_file.read_text(encoding='utf-8').splitlines():
                    if '=' in line:
                        key, value = line.split('=', 1)
                        version_parts[key.strip()] = value.strip()
            except OSError:
                version_parts = {}
        configured_kernel = '.'.join(version_parts.get(k, '') for k in ('MAJOR', 'MINOR', 'BUILD', 'PATCH')).strip('.')
        win_version = host_platform.win32_ver() if host_platform.system() == 'Windows' else ()
        os_name = ('Windows ' + (win_version[0] or host_platform.release()) +
                   ((' build ' + win_version[1]) if len(win_version) > 1 and win_version[1] else '')) if win_version else \
                   f'{host_platform.system()} {host_platform.release()}'
        runtime = None
        runtime_error = ''
        try:
            runtime_state = self.bridge.backend.runtime_state(max_age=1)
            running = env_id in set(runtime_state.get('running_envs') or [])
        except Exception:
            runtime_state = {'running_envs': []}
            running = False
        if running:
            try:
                runtime = self.bridge.backend.fingerprint_snapshot(env_id)
            except Exception:
                runtime_error = '环境正在运行，但当前页面暂时无法读取实时指纹'

        browser_runtime = (runtime or {}).get('_browser', {})
        runtime_ua_data = (runtime or {}).get('user_agent_data') or {}
        if not isinstance(runtime_ua_data, dict):
            runtime_ua_data = {}
        product = str(browser_runtime.get('product') or '')
        match = _re.search(r'/([0-9]+(?:\.[0-9]+){1,3})', product)
        kernel_version = (match.group(1) if match else '') or configured_kernel
        runtime_platform = (runtime or {}).get('platform') or configured.get('platform')
        runtime_languages = (runtime or {}).get('languages') or [x for x in configured_languages.split(',') if x]
        runtime_timezone = (runtime or {}).get('timezone') or configured.get('timezone')
        runtime_screen = (runtime or {}).get('screen') or {}
        runtime_window = (runtime or {}).get('window') or {}
        runtime_webgl = (runtime or {}).get('webgl') or {}
        runtime_fonts = (runtime or {}).get('fonts')
        proxy_test = (meta.get('envs', {}).get(env_id, {}) or {}).get('proxy_test') or {}
        location = {
            'exit_ip': proxy_test.get('exit_ip') or proxy_test.get('geo_ip') or row.get('expected_exit_ip') or '',
            'expected_exit_ip': row.get('expected_exit_ip') or '',
            'country': proxy_test.get('country'),
            'region': proxy_test.get('region'),
            'city': proxy_test.get('city'),
            'timezone': proxy_test.get('timezone'),
            'org': proxy_test.get('org') or proxy_test.get('asn'),
            'latency_ms': proxy_test.get('latency_ms'),
            'status': proxy_test.get('status', '未检测') if proxy_test else '未检测',
            'checked_at': proxy_test.get('at') if proxy_test else None,
        }
        return {
            'ok': True,
            'env_id': env_id,
            'display_name': row.get('display_name') or env_id,
            'source': 'runtime' if runtime else 'configured',
            'updated_at': time.time(),
            'runtime': {
                'available': bool(runtime),
                'status': 'running' if running else 'stopped',
                'note': ('以下实时值来自当前环境页面' if runtime else
                         runtime_error or '环境未运行，以下为根据指纹种子推算的配置值'),
            },
            'browser': {
                'product': '蓝豆指纹浏览器',
                'kernel': 'Chromium',
                'kernel_version': kernel_version or '启动环境后读取',
                'user_agent': (runtime or {}).get('user_agent') or browser_runtime.get('user_agent') or '启动环境后读取',
                'platform': runtime_platform,
                'os': f'{os_name} ({host_platform.version()})',
                'architecture': runtime_ua_data.get('architecture') or host_platform.machine(),
                'webdriver': (runtime or {}).get('webdriver', configured.get('webdriver', False)),
                'max_touch_points': (runtime or {}).get('max_touch_points'),
            },
            'device': {
                'hardware_concurrency': (runtime or {}).get('hardware_concurrency') or configured.get('hardwareConcurrency'),
                'device_memory_gb': (runtime or {}).get('device_memory_gb') or configured.get('deviceMemory'),
                'screen': runtime_screen or {'value': configured.get('screen'), 'available': configured.get('avail_screen')},
                'window': runtime_window or {'value': configured.get('window_size')},
            },
            'language': {
                'locale': (runtime or {}).get('language') or configured_locale,
                'accept_languages': configured_languages,
                'navigator_language': (runtime or {}).get('language') or configured_locale,
                'navigator_languages': runtime_languages,
                'intl_locale': (runtime or {}).get('intl_locale') or configured_locale,
                'timezone': runtime_timezone,
            },
            'graphics': {
                'vendor': runtime_webgl.get('vendor') or configured.get('webgl_vendor'),
                'renderer': runtime_webgl.get('renderer') or configured.get('webgl_renderer'),
                'version': runtime_webgl.get('version') or '配置推算值',
                'hardware_tier': configured.get('hardware_tier'),
            },
            'fonts': runtime_fonts or {
                'mode': 'configured',
                'items': [],
                'count': None,
                'note': '启动环境后才能读取网页可见字体；当前仅显示默认 Windows 字体策略',
            },
            'location': location,
            'privacy': {
                'cookies_included': False,
                'credentials_included': False,
                'login_data_included': False,
                'note': '本页不读取 Cookie、密码、2FA 或网站登录数据',
            },
        }

    def test_proxy(self, payload):
        from lan_bridge import validate_proxy_config
        proxy = validate_proxy_config(str(payload.get('proxy', '')).strip())
        expected = str(payload.get('expected_exit_ip', '')).strip()
        if payload.get('env_id') and 'expected_exit_ip' not in payload:
            row = next((e for e in self._state()['envs'] if e['env_id'] == payload['env_id']), None)
            expected = row.get('expected_exit_ip', '') if row else ''
        if expected:
            try:
                expected = str(ipaddress.ip_address(expected))
            except ValueError:
                raise ManagementError('预期出口 IP 格式无效', fields={'expected_exit_ip': '请输入有效的 IPv4 或 IPv6 地址'})
        if proxy in ('', 'direct', 'system'):
            return {'ok': False, 'status': 'not_required', 'proxy': proxy, 'at': time.time(),
                    'error': '直连或系统代理不执行远端出口检测'}
        started = time.monotonic()
        result = self._probe(proxy)
        result = {**result, 'proxy': proxy, 'at': time.time(),
                  'expected_exit_ip': expected,
                  'status': 'success' if result.get('ok') else 'failed',
                  'elapsed_ms': round((time.monotonic() - started) * 1000)}
        if result.get('ok') and expected and ipaddress.ip_address(result['exit_ip']) != ipaddress.ip_address(expected):
            result.update(ok=False, status='mismatch',
                          error='代理已连接，但出口 IP 与预期不一致，请核对代理线路')
        if result.get('ok') and not payload.get('env_id'):
            now = time.time()
            self._pending_proxy_tests = {
                key: value for key, value in self._pending_proxy_tests.items()
                if now - value['at'] <= 300
            }
            while len(self._pending_proxy_tests) >= 100:
                self._pending_proxy_tests.pop(next(iter(self._pending_proxy_tests)))
            receipt = secrets.token_urlsafe(24)
            self._pending_proxy_tests[receipt] = {'proxy': proxy, 'expected_exit_ip': expected,
                                                   'result': result, 'at': time.time()}
            result['receipt'] = receipt
        if payload.get('env_id'):
            with self.lock:
                row = next((e for e in self._state()['envs'] if e['env_id'] == payload['env_id']), None)
                if (row and row.get('proxy', '') == proxy and
                        ip_key(row.get('expected_exit_ip', '')) == expected):
                    try:
                        meta = self._meta()
                        meta['envs'].setdefault(row['env_id'], {})['proxy_test'] = result
                        self._write_meta(meta)
                        self._record('proxy_test', row['env_id'], status=result['status'])
                    except OSError:
                        result['warning'] = '检测已完成，但本地检测记录未完整保存，请检查磁盘空间与权限'
        return result

    def attach_proxy_test(self, payload):
        receipt = payload.get('receipt', '')
        env = payload.get('env_id')
        if not isinstance(receipt, str) or len(receipt) > 128:
            raise ManagementError('代理检测结果已过期，请重新检测')
        with self.lock:
            pending = self._pending_proxy_tests.get(receipt)
            if not pending or time.time() - pending['at'] > 300:
                self._pending_proxy_tests.pop(receipt, None)
                raise ManagementError('代理检测结果已过期，请重新检测')
            row = next((e for e in self._state()['envs'] if e['env_id'] == env), None)
            if not row or row['proxy'] != pending['proxy'] or ip_key(row['expected_exit_ip']) != ip_key(pending['expected_exit_ip']):
                raise ManagementError('保存的代理与检测结果不一致，请重新检测')
            meta = self._meta()
            meta['envs'].setdefault(env, {})['proxy_test'] = pending['result']
            self._write_meta(meta)
            self._record('proxy_test', env, status='success')
            self._pending_proxy_tests.pop(receipt, None)
            return {'ok': True}

    @staticmethod
    def probe_proxy(proxy):
        try:
            import requests
            proxy=proxy.replace('socks5://','socks5h://').replace('socks4://','socks4a://')
            session=requests.Session();session.trust_env=False
            started=time.monotonic()
            with session.get('https://api.ipify.org?format=json',proxies={'http':proxy,'https':proxy},timeout=8) as response:
                response.raise_for_status();data=response.json()
            latency_ms=round((time.monotonic()-started)*1000)
            ipaddress.ip_address(data.get('ip', ''))
            result={'ok': True, 'exit_ip': data['ip'], 'latency_ms': latency_ms}
            try:
                with requests.get('https://ipapi.co/'+data['ip']+'/json/',timeout=5,
                                  headers={'User-Agent':'BlueBean-Environment-Proxy-Test/1.0'}) as response:
                    response.raise_for_status();geo=response.json()
                if geo.get('error'):
                    raise ValueError('geolocation unavailable')
                result.update({key:geo.get(source) for key,source in (
                    ('city','city'),('region','region'),('country','country_name'),
                    ('timezone','timezone'),('asn','asn'),('org','org')) if geo.get(source)})
                result['geo_source']='ipapi.co'
                result['geo_ip']=geo.get('ip')
                if result['geo_ip'] != data['ip']:
                    result.update({key:None for key in ('city','region','country','timezone','asn','org')})
                    result['geo_error']='地理信息服务返回的 IP 与代理出口不一致'
            except Exception:
                result['geo_source']='ipapi.co'
                result['geo_error']='出口已确认，地理信息暂不可用'
            return result
        except Exception:
            # No credentials, proxy URLs or internal exception strings in history.
            return {'ok': False, 'error': '代理连接或出口检测失败，请检查地址、端口与网络'}

    def job(self, payload):
        with self.job_lock:
            action = payload.get('action', 'status')
            if action not in ('start', 'status', 'cancel'):
                raise ManagementError('不支持的巡检操作')
            if action == 'start':
                request_id = payload.get('request_id', '')
                if not isinstance(request_id, str) or len(request_id) > 80:
                    raise ManagementError('巡检请求编号格式无效')
                if request_id:
                    previous = next((j for j in self.jobs.values() if j.get('request_id') == request_id), None)
                    if previous:
                        return {'ok': True, 'job': self._job_snapshot(previous)}
                running = next((j for j in self.jobs.values() if j['status'] in ('running', 'cancelling')), None)
                if running:
                    raise ManagementError('已有代理巡检运行中，已显示现有任务及其范围', 409,
                                          job=self._job_snapshot(running))
                rows = self._state()['envs']
                ids = payload.get('env_ids')
                known = {e['env_id'] for e in rows}
                if (not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids)
                        or not set(ids) <= known or len(ids) > 1000):
                    raise ManagementError('请选择 1–1000 个有效环境')
                selected = [definition(e) for e in rows if e['env_id'] in ids]
                job_id = secrets.token_hex(12)
                job = {'id': job_id, 'status': 'running', 'total': len(selected), 'done': 0,
                       'env_ids': [row['env_id'] for row in selected], 'request_id': request_id,
                       'results': [], 'started_at': time.time(), '_cancel': threading.Event()}
                self.jobs = {job_id: job}
                threading.Thread(target=self._run_job, args=(job, selected), daemon=True,
                                 name='environment-proxy-check').start()
            else:
                job_id = payload.get('job_id')
                if job_id is not None and not isinstance(job_id, str):
                    raise ManagementError('巡检编号格式无效')
                job = self.jobs.get(job_id)
                if not job_id and action == 'status':
                    job = next(reversed(self.jobs.values()), None)
                if job is None:
                    return {'ok': True, 'job': None}
                if action == 'cancel':
                    job['_cancel'].set()
                    if job['status'] == 'running':
                        job['status'] = 'cancelling'
            return {'ok': True, 'job': self._job_snapshot(job)}

    @staticmethod
    def _job_snapshot(job):
        return copy.deepcopy({k: v for k, v in job.items() if not k.startswith('_')})

    def _run_job(self, job, rows):
        def check(row):
            if job['_cancel'].is_set():
                return {'env_id': row['env_id'], 'ok': False, 'status': 'cancelled'}
            try:
                return {'env_id': row['env_id'], **self.test_proxy(row)}
            except Exception:
                return {'env_id': row['env_id'], 'ok': False, 'status': 'failed', 'error': '检测失败'}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(check, row) for row in rows]
            for future in as_completed(futures):
                with self.job_lock:
                    job['results'].append(future.result())
                    job['done'] += 1
        warning = ''
        with self.lock:
            try:
                self._record('proxy_inspection', count=job['done'],
                             status='cancelled' if job['_cancel'].is_set() else 'complete')
            except OSError:
                warning = '巡检已完成，但维护记录写入失败，请检查磁盘空间与权限'
        with self.job_lock:
            job['status'] = 'cancelled' if job['_cancel'].is_set() else 'complete'
            job['finished_at'] = time.time()
            if warning:
                job['warning'] = warning

    def dispatch(self, action, payload):
        methods = {'list': self.list, 'maintenance': self.maintenance,
                   'save': self.save, 'account': self.account, 'remove': self.remove,
                   'backup': self.backup, 'restore': self.restore, 'proxy-test': self.test_proxy,
                   'proxy-job': self.job,'accounts':self.account_services,'draft':self.draft,
                   'bulk':self.bulk,'history':self.history,'portable':self.portable,'open':self.workspace_open,
                   'proxy-attach':self.attach_proxy_test,'status':self.runtime_status,
                   'fingerprint': self.fingerprint,
                   'import':self.imports.dispatch}
        method = methods.get(action)
        if method is None:
            raise ManagementError('接口不存在', 404)
        return method(payload) if action == 'status' else method() if action in ('list', 'maintenance') else method(payload)
