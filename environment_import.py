"""Local, encrypted, resumable environment imports. Native storage has one owner."""
from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import json
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from environment_accounts import catalog, totp
from local_account_vault import _dpapi

HEADERS = {
    'environments': [('环境名称', 'display_name'), ('分组', 'group'), ('代理', 'proxy'),
                     ('首次打开页面', 'start_url'), ('语言', 'locale')],
    'proxies': [('环境', 'environment'), ('代理', 'proxy')],
    'accounts': [('环境', 'environment'), ('平台', 'platform_id'), ('登录账号', 'login'),
                 ('密码', 'password'), ('2FA密钥', 'otp_secret'), ('用户名', 'username'),
                 ('网站', 'url'), ('备注', 'label')],
}
LIMIT = 200
ACTIVE = ('running', 'cancelling')


class ImportRowError(ValueError):
    """Only controlled, credential-free messages may be exposed to the client."""
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class EnvironmentImports:
    def __init__(self, manager):
        self.manager = manager
        self.directory = manager.profile / 'env-imports'
        self.lock = threading.RLock()
        self.jobs = {}

    def busy(self):
        with self.lock:
            return any(job['status'] in ACTIVE for job in self.jobs.values())

    def _write(self, job):
        from env_management import atomic_json
        job['updated_at'] = time.time()
        raw = json.dumps(job, ensure_ascii=False).encode('utf-8')
        atomic_json(self.directory / (job['id'] + '.json'), {
            'version': 1, 'ciphertext': base64.b64encode(_dpapi(raw, protect=True)).decode()})

    def _load(self, token):
        if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
            raise ValueError('导入任务编号无效')
        if token not in self.jobs:
            try:
                doc = json.loads((self.directory / (token + '.json')).read_text(encoding='utf-8'))
                job = json.loads(_dpapi(base64.b64decode(doc['ciphertext']), protect=False))
            except Exception:
                raise ValueError('本机导入记录不可用，请重新预览') from None
            if job['status'] in ACTIVE:
                job['status'] = 'interrupted'
                for row in job['rows']:
                    if row['status'] == 'running': row['status'] = 'pending'
                self._write(job)
            self.jobs[token] = job
        return self.jobs[token]

    @staticmethod
    def snapshot(job):
        rows = [{k: r[k] for k in ('line', 'label', 'env_id', 'status', 'error', 'error_code', 'retryable', 'summary') if k in r}
                for r in job['rows']]
        return {'id': job['id'], 'kind': job['kind'], 'status': job['status'], 'rows': rows,
                'created_at': job.get('created_at'), 'updated_at': job.get('updated_at'),
                'total': len(rows), 'done': sum(r['status'] == 'success' for r in rows),
                'processed': sum(r['status'] in ('success', 'failed') for r in rows),
                'resumable': sum(r['status'] in ('pending', 'running') or r['status'] == 'failed' and r.get('retryable', True) for r in rows),
                'failed': sum(r['status'] == 'failed' for r in rows),
                'invalid': sum(r['status'] == 'invalid' for r in rows)}

    def history(self):
        """Return summaries only; credentials and row identities stay inside each plan."""
        if not self.directory.exists(): return []
        result = []
        for path in self.directory.glob('*.json'):
            if not re.fullmatch(r'[a-f0-9]{32}', path.stem): continue
            cached = path.stem in self.jobs
            try:
                item = self.snapshot(self._load(path.stem))
                item.pop('rows')
            except (ValueError, OSError):
                item = {'id': path.stem, 'kind': 'unknown', 'status': 'unavailable',
                        'total': 0, 'done': 0, 'failed': 0, 'invalid': 0,
                        'updated_at': 0}
            finally:
                if not cached and self.jobs.get(path.stem, {}).get('status') not in ACTIVE:
                    self.jobs.pop(path.stem, None)
            result.append(item)
        return sorted(result, key=lambda j: j.get('updated_at') or j.get('created_at') or 0, reverse=True)

    def dispatch(self, payload):
        action = payload.get('action', 'status')
        with self.lock:
            if action == 'history': return {'ok': True, 'jobs': self.history()}
            if action == 'discard':
                token = payload.get('job_id')
                if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
                    raise ValueError('导入任务编号无效')
                if self.jobs.get(token, {}).get('status') in ACTIVE:
                    raise ValueError('请先停止导入再清除记录')
                # A damaged encrypted task can also be cleared through its safe token.
                (self.directory / (token + '.json')).unlink(missing_ok=True)
                self.jobs.pop(token, None)
                return {'ok': True, 'job': None}
            if action == 'preview':
                token = payload.get('request_id')
                if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
                    raise ValueError('请提供有效的预览请求编号')
                if token in self.jobs or (self.directory / (token + '.json')).exists():
                    return {'ok': True, 'job': self.snapshot(self._load(token))}
                # Bound retained encrypted plans, without deleting a running import.
                if self.directory.exists():
                    for path in self.directory.glob('*.json'):
                        cached = self.jobs.get(path.stem)
                        if path.stat().st_mtime < time.time() - 7 * 86400 and not (cached and cached['status'] in ACTIVE):
                            path.unlink(missing_ok=True); self.jobs.pop(path.stem, None)
                    if len(list(self.directory.glob('*.json'))) >= 100:
                        raise ValueError('本机导入记录已达上限，请先清除已完成的任务记录')
                job = self.preview(payload, token)
                self._write(job)
                self.jobs[token] = job
            else:
                token = payload.get('job_id')
                if not token and action == 'status': return {'ok': True, 'job': None}
                job = self._load(token)
                if action == 'source':
                    if job['status'] != 'preview': raise ValueError('执行后的任务不能恢复原始输入，请重新准备导入数据')
                    return {'ok': True, 'source': job.get('source')}
                if action in ('apply', 'retry'):
                    if job['status'] in ACTIVE or job['status'] == 'complete':
                        return {'ok': True, 'job': self.snapshot(job)}
                    if action == 'apply' and job['status'] != 'preview':
                        # A delayed duplicate confirmation must never undo a cancellation.
                        # Only the explicit Retry action can resume a terminal task.
                        return {'ok': True, 'job': self.snapshot(job)}
                    if any(r['status'] == 'invalid' for r in job['rows']):
                        raise ValueError('请先修正全部错误行并重新预览')
                    if not self.snapshot(job)['resumable']:
                        raise ValueError('剩余项目存在配置冲突或重复账号，请重新检查数据后导入')
                    if any(j['status'] in ACTIVE for j in self.jobs.values()):
                        raise ValueError('已有批量导入正在执行，请等待完成')
                    previous = copy.deepcopy(job)
                    job['status'] = 'running'
                    job.pop('source', None)
                    for r in job['rows']:
                        if r['status'] != 'success' and r.get('retryable', True):
                            r['status'] = 'pending'
                            for field in ('error', 'error_code', 'retryable'): r.pop(field, None)
                    try:
                        self._write(job)
                        threading.Thread(target=self._run, args=(job,), daemon=True, name='environment-import').start()
                    except Exception:
                        # No worker was started. Do not leave a false busy/running task behind.
                        job.clear(); job.update(previous)
                        try: self._write(job)
                        except OSError: pass
                        raise ValueError('导入任务未启动，请检查本机保存权限或系统资源后重试；原始预览已保留') from None
                elif action == 'cancel':
                    if job['status'] == 'running': job['status'] = 'cancelling'; self._write(job)
                elif action != 'status': raise ValueError('不支持的导入操作')
            return {'ok': True, 'job': self.snapshot(job)}

    @staticmethod
    def parse(kind, text):
        if not isinstance(text, str) or len(text.encode('utf-8')) > 1024 * 1024:
            raise ValueError('请粘贴或上传不超过 1MB 的 CSV / TSV 文件')
        text = text.lstrip('\ufeff')
        delimiter = '\t' if '\t' in text.split('\n', 1)[0] else ','
        reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True)
        aliases = {name: key for name, key in HEADERS[kind]}
        aliases.update({key: key for _, key in HEADERS[kind]})
        try:
            header = next(reader)
            keys = [aliases.get(k.strip()) for k in header]
            required = {'environments': {'display_name'}, 'proxies': {'environment', 'proxy'},
                        'accounts': {'environment', 'platform_id', 'login'}}[kind]
            if None in keys or len(keys) != len(set(keys)) or not required <= set(keys):
                raise ValueError('表头无效，请使用当前类型的模板；必填列不能缺少，列名不能重复')
            rows = []
            while True:
                line = reader.line_num + 1
                try: values = next(reader)
                except StopIteration: break
                if not values or not any(v.strip() for v in values): continue
                if len(values) != len(keys):
                    raise ValueError(f'第 {line} 行列数与表头不一致，请给含逗号的内容加双引号')
                rows.append((line, {k: v if k == 'password' else v.strip() for k, v in zip(keys, values)}))
                if len(rows) > LIMIT: raise ValueError(f'每批最多 {LIMIT} 行，请拆分导入')
        except (csv.Error, StopIteration):
            raise ValueError('CSV 格式错误或没有表头，请使用模板并检查引号') from None
        if not rows: raise ValueError('请至少填写一行数据')
        return rows

    def preview(self, payload, token):
        from env_management import definition, revision, validate_definition
        kind = payload.get('kind')
        if kind not in HEADERS: raise ValueError('请选择批量操作类型')
        if payload.get('generate'):
            if kind != 'environments': raise ValueError('此类型不支持自动生成')
            g = payload['generate']
            if not isinstance(g, dict): raise ValueError('批量生成设置格式无效')
            count, start = g.get('count'), g.get('start', 1)
            if type(count) is not int or not 1 <= count <= LIMIT or type(start) is not int or not 0 <= start <= 999999:
                raise ValueError(f'数量须为 1–{LIMIT}，起始编号须为 0–999999 的整数')
            prefix = g.get('prefix', '')
            if not isinstance(prefix, str) or not prefix.strip(): raise ValueError('请填写名称前缀')
            proxies = g.get('proxies', '')
            if not isinstance(proxies, str) or len(proxies.encode('utf-8')) > 1024 * 1024:
                raise ValueError('代理列表格式无效或超过1MB')
            proxies = [p.strip() for p in proxies.splitlines() if p.strip()]
            if proxies and len(proxies) != count: raise ValueError('代理行数必须与新建数量一致；按顺序一对一分配')
            source = [(i + 1, {'display_name': prefix.strip() + str(start + i).zfill(3),
                       'group': g.get('group', ''), 'locale': g.get('locale', ''),
                       'start_url': g.get('start_url', ''), 'proxy': proxies[i] if proxies else 'direct'})
                      for i in range(count)]
        else: source = self.parse(kind, payload.get('text', ''))
        manager = self.manager
        with manager.lock:
            internal_ids = manager.internal_session_ids()
            current = [r for r in manager._state()['envs'] if r['env_id'] not in internal_ids]
            metadata = manager._workspace().metadata() if kind == 'accounts' else {}
        by_id = {r['env_id']: r for r in current}
        by_name = {}
        for r in current: by_name.setdefault(r.get('display_name', ''), []).append(r)
        account_identities = {env: {(a.get('platform_id'), a.get('login'), a.get('url'))
                                   for a in bucket.get('accounts', [])} for env, bucket in metadata.items()}
        known_names = {r.get('display_name', '').casefold() for r in current}
        seen = set(); result = []
        for line, values in source:
            row = {'line': line, 'label': f'第 {line} 行', 'status': 'pending'}
            try:
                if kind == 'environments':
                    config = validate_definition({**values, 'env_id': 'env-' + secrets.token_hex(12),
                                                  'fingerprint_seed': secrets.token_hex(32)})
                    key = config['display_name'].casefold()
                    if key in seen or key in known_names: raise ValueError('环境名称重复，请修改名称')
                    row.update(env_id=config['env_id'], label=config['display_name'], config=config,
                               summary='独立指纹 · 独立网站数据' + (' · 已分配代理' if config['proxy'] not in ('', 'direct') else ' · 直接连接'))
                else:
                    target = values.get('environment', '')
                    match = [by_id[target]] if target in by_id else by_name.get(target, [])
                    if not target or len(match) != 1: raise ValueError('环境不存在或名称重名，请填写准确的环境编号')
                    env = match[0]; row.update(env_id=env['env_id'], revision=revision(env))
                    if kind == 'proxies':
                        if not values.get('proxy'): raise ValueError('代理不能为空；直连请填写 direct')
                        config = validate_definition({'proxy': values['proxy']}, env)
                        key = env['env_id']; row.update(config=config, label=env['display_name'], summary='更新代理；已有环境需重启生效')
                    else:
                        a = self.validate_account(values)
                        identity = (a['platform_id'], a['login'], a['url'])
                        key = (env['env_id'], *identity)
                        if identity in account_identities.get(env['env_id'], set()):
                            raise ValueError('此环境已存在同平台、同登录账号和网站的记录；请在账号管理中编辑')
                        a['client_id'] = 'import-' + token + '-' + str(line)
                        row.update(account=a, label=env['display_name'] + ' · ' + a['platform'],
                                   summary=a['login'] + ' · 密码' + ('已填写' if a.get('password') else '未填写') + ' · 2FA' + ('已填写' if a.get('otp_secret') else '未填写'))
                if key in seen: raise ValueError('同一批次重复行，请移除重复项')
                seen.add(key)
            except ValueError as exc:
                row = {'line': line, 'label': f'第 {line} 行', 'status': 'invalid',
                       'error': self.safe_error(exc)}
            result.append(row)
        source = ({'method': 'generate', 'generate': {'prefix': prefix, 'start': start, 'count': count,
                   **{key: g.get(key, '') for key in ('group', 'locale', 'start_url', 'proxies')}}}
                  if payload.get('generate') else {'method': 'csv', 'text': payload.get('text', '')})
        return {'id': token, 'kind': kind, 'status': 'preview', 'created_at': time.time(), 'rows': result, 'source': source}

    @staticmethod
    def validate_account(values):
        a = {k: values.get(k, '') for k in ('platform_id', 'login', 'password', 'otp_secret', 'username', 'url', 'label')}
        p = next((p for p in catalog() if a['platform_id'].casefold() in (p['id'].casefold(), p['name'].casefold())), None)
        if not p or not a['login']: raise ValueError('请选择目录中的平台并填写登录账号')
        a.update(platform_id=p['id'], platform=p['name'], url=a['url'] or p['url'])
        for k, limit in {'login': 240, 'username': 240, 'label': 120, 'url': 2048, 'password': 2048, 'otp_secret': 2048}.items():
            if len(a[k]) > limit: raise ValueError('账号字段过长')
        if not a['url']: raise ValueError('自定义平台必须填写网站地址')
        try:
            u = urlsplit(a['url']); _ = u.port
            if u.scheme not in ('http', 'https') or not u.hostname or u.username is not None or u.password is not None or any(c.isspace() for c in a['url']): raise ValueError()
        except ValueError: raise ValueError('请输入有效的 http(s) 网站地址，不能包含凭据') from None
        if a['otp_secret']:
            try: totp(a['otp_secret'])
            except Exception: raise ValueError('2FA 请填写验证器密钥，不是六位验证码') from None
        return a

    @staticmethod
    def safe_error(exc):
        fields = getattr(exc, 'reply', {}).get('field_errors')
        if fields: return '；'.join(fields.values())
        # These messages come only from our validators, never parser/raw transport exceptions.
        return str(exc)

    def _apply(self, job, row):
        from env_management import definition, revision
        m = self.manager
        with m.lock:
            current = next((r for r in m._state()['envs'] if r['env_id'] == row['env_id']), None)
            if job['kind'] == 'environments':
                if current:
                    if definition(current) == row['config']: return
                    raise ImportRowError('config_conflict', '环境编号已对应其他配置，请检查当前环境后重新导入')
                reply = m.save({**row['config'], 'create_only': True})
            elif job['kind'] == 'proxies':
                if current and definition(current) == row['config']: return
                if not current or revision(current) != row['revision']:
                    raise ImportRowError('config_conflict', '环境已删除或配置已修改；请重新预览，避免覆盖他人的修改')
                reply = m.save({**row['config'], 'revision': row['revision']})
            else:
                if not current or revision(current) != row['revision']:
                    raise ImportRowError('config_conflict', '目标环境已删除或配置已修改，请检查后重新导入')
                store = m._workspace(); a = row['account']
                aid = 'acc-' + hashlib.sha256((row['env_id'] + ':' + a['client_id']).encode()).hexdigest()[:24]
                existing = store.list(row['env_id'])
                if any(x['account_id'] == aid for x in existing): return
                if any(all(x.get(k) == a.get(k) for k in ('platform_id', 'login', 'url')) for x in existing):
                    raise ImportRowError('duplicate_account', '账号已在其他页面添加，请到账号管理查看，不会重复创建')
                store.save(row['env_id'], a)
                try: m._record('account_save', row['env_id'])
                except OSError: pass  # Account is already durable; never replay this mutation.
                return
            if not reply.get('ok'): raise ImportRowError('save_failed', '保存未完成，请检查连接后重试', True)

    @staticmethod
    def failure(exc):
        if isinstance(exc, ImportRowError):
            return {'status': 'failed', 'error_code': exc.code, 'error': str(exc), 'retryable': exc.retryable}
        if getattr(exc, 'status', None) == 409:
            return {'status': 'failed', 'error_code': 'config_conflict', 'retryable': False,
                    'error': '环境配置、名称或保留资料发生冲突，请检查后重新预览；不会覆盖已有数据'}
        if getattr(exc, 'status', None) == 503 or isinstance(exc, (ConnectionError, TimeoutError)):
            return {'status': 'failed', 'error_code': 'connection', 'retryable': True,
                    'error': '浏览器暂未连接或保存结果待确认；恢复连接后可重试，系统会先核对已保存结果'}
        if isinstance(exc, OSError):
            return {'status': 'failed', 'error_code': 'local_storage', 'retryable': True,
                    'error': '本机加密保存失败，请检查磁盘空间与保存权限后重试'}
        return {'status': 'failed', 'error_code': 'save_failed', 'retryable': True,
                'error': '保存暂未完成，请检查本机服务后重试；已完成条目会保留'}

    def _run(self, job):
        try:
            for row in job['rows']:
                with self.lock:
                    if job['status'] == 'cancelling': break
                    if row['status'] == 'success' or not row.get('retryable', True): continue
                    row['status'] = 'running'; self._write(job)
                try:
                    self._apply(job, row)
                    outcome = {'status': 'success'}
                except Exception as exc:
                    # No raw exception, URL, password or OTP may leave the encrypted plan.
                    outcome = self.failure(exc)
                with self.lock:
                    row.update(outcome); self._write(job)
            with self.lock:
                job['status'] = ('cancelled' if job['status'] == 'cancelling' else
                                 'complete' if all(r['status'] == 'success' for r in job['rows']) else 'partial')
                self._write(job)
        except Exception:
            # If the progress file cannot be persisted, stop dispatching additional writes.
            with self.lock: job['status'] = 'interrupted'
