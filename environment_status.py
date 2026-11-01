"""Thread-safe Agent activity and evidence-based environment status projection."""
from __future__ import annotations

import copy
import ipaddress
import re
import secrets
import threading
import time


class AgentActivity:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = {}
        self.finished = {}

    def begin(self, env):
        token = secrets.token_hex(12)
        with self.lock:
            # Overlapping operations form one wave. A new operation must not
            # erase a sibling's already reported failure within that wave.
            if not any(run['env'] == env for run in self.running.values()):
                self.finished.pop(env, None)
            self.running[token] = {'env': env, 'started_at': time.time()}
        return token

    def finish(self, token, ok, error=''):
        with self.lock:
            run = self.running.pop(token, None)
            if run:
                # Keep errors as codes, never expose page text, credentials or URLs.
                network = re.search(r'ERR_(?:PROXY_CONNECTION_FAILED|TUNNEL_CONNECTION_FAILED|SOCKS_CONNECTION_FAILED|INTERNET_DISCONNECTED|NAME_NOT_RESOLVED|CONNECTION_REFUSED|CONNECTION_TIMED_OUT|TIMED_OUT)', str(error))
                result = self.finished.setdefault(run['env'], {'completed_count': 0, 'failed_count': 0})
                result['completed_count' if ok else 'failed_count'] += 1
                result.update(state='failed' if result['failed_count'] else 'complete', updated_at=time.time())
                if network:
                    result.update(network_error=network.group(0), network_error_at=time.time())

    def snapshot(self, live_ids=None):
        with self.lock:
            if live_ids is not None:
                # Closing an environment clears its completion marker for the next launch.
                active = {row['env'] for row in self.running.values()}
                self.finished = {env: row for env, row in self.finished.items() if env in live_ids or env in active}
            result = copy.deepcopy(self.finished)
            for run in self.running.values():
                row = result.setdefault(run['env'], {'completed_count': 0, 'failed_count': 0})
                row['active_count'] = row.get('active_count', 0) + 1
                row.update(state='running', updated_at=max(row.get('updated_at', 0), run['started_at']))
            return result


class ProxyHealth:
    """At most two probes at a time, deduplicated by actual proxy and expected IP."""
    def __init__(self, probe):
        self.probe = probe
        self.lock = threading.Lock()
        self.results = {}
        self.pending = set()

    def read(self, candidates, start=True):
        """Return cached health and optionally start missing probes.

        Runtime status reads use ``start=False`` so rendering the environment
        list never turns into an implicit network sweep. Explicit proxy tests
        retain the asynchronous probe behaviour by leaving the default enabled.
        """
        now = time.monotonic()
        with self.lock:
            wanted = set(candidates)
            self.results = {key: value for key, value in self.results.items() if key in wanted}
            if not start:
                return {key: copy.deepcopy(value[1]) for key, value in self.results.items()}
            for key in candidates:
                value = self.results.get(key)
                if key in self.pending or value and now-value[0] < (30 if value[1]['status'] != 'success' else 60):
                    continue
                if len(self.pending) >= 2:
                    break
                self.pending.add(key)
                threading.Thread(target=self._check, args=(key,), daemon=True,
                                 name='environment-proxy-health').start()
            return {key: copy.deepcopy(value[1]) for key, value in self.results.items()}

    def snapshot(self, candidates):
        """Return cached results without scheduling or touching the network."""
        now = time.monotonic()
        with self.lock:
            wanted = set(candidates)
            self.results = {key: value for key, value in self.results.items() if key in wanted}
            return {key: copy.deepcopy(value[1]) for key, value in self.results.items()
                    if now - value[0] < (30 if value[1].get('status') != 'success' else 60)}

    def _check(self, key):
        proxy, expected = key
        try:
            result = self.probe(proxy)
            state = 'success' if result.get('ok') else 'failed'
            if state == 'success' and expected and ipaddress.ip_address(result.get('exit_ip')) != ipaddress.ip_address(expected):
                state = 'mismatch'
        except Exception:
            state = 'failed'
        with self.lock:
            self.results[key] = (time.monotonic(), {'status': state, 'at': time.time(),
                                                    'proxy': proxy, 'expected_exit_ip': expected})
            self.pending.discard(key)


def project_status(live, known, network, agent):
    running = bool(live and live.get('tabs')) if known else None
    active_count = agent.get('active_count', int(agent.get('state') == 'running'))
    completed_count = agent.get('completed_count', int(agent.get('state') == 'complete'))
    failed_count = agent.get('failed_count', int(agent.get('state') == 'failed'))
    if agent.get('state') == 'running':
        state, label = 'agent-running', 'Agent 正在操作'
    elif not known:
        state, label = 'unknown', '状态未知'
    elif not running:
        state, label = 'stopped', '未运行'
    elif network in ('failed', 'mismatch'):
        state, label = 'network-error', '代理出口不符' if network == 'mismatch' else '网络异常'
    elif agent.get('state') == 'complete':
        state, label = 'agent-complete', 'Agent 操作完成'
    else:
        state, label = 'running', '运行中'
    return {'state': state, 'label': label, 'running': running, 'network': network,
            'agent': agent.get('state', 'idle'), 'updated_at': agent.get('updated_at'),
            'agent_active_count': active_count, 'agent_completed_count': completed_count,
            'agent_failed_count': failed_count,
            'agent_warning': bool(failed_count) and (bool(running) or bool(active_count)),
            'network_warning': network in ('failed', 'mismatch') and (bool(running) or agent.get('state') == 'running')}


def aggregate_status(members, known):
    running = any(x['running'] for x in members)
    # An inactive account's historical network test must not taint running siblings.
    network = ('mismatch' if any(x['network_warning'] and x['network'] == 'mismatch' for x in members)
               else 'failed' if any(x['network_warning'] for x in members) else 'unchecked')
    eligible = [x for x in members if x['running'] or x['agent'] == 'running' or
                x.get('agent_active_count', 0) or x.get('agent_completed_count', 0) or
                x.get('agent_failed_count', 0)]
    count = lambda key, state: sum(x.get(key, int(x['agent'] == state)) for x in eligible)
    active = count('agent_active_count', 'running')
    completed = count('agent_completed_count', 'complete')
    failed = count('agent_failed_count', 'failed')
    # Historical completions from inactive sibling accounts contribute warning
    # counts but must not replace the current environment's running indicator.
    current_complete = any(x['running'] and x['agent'] == 'complete' for x in eligible)
    current_failed = any(x['running'] and x['agent'] == 'failed' for x in eligible)
    state = 'running' if active else 'failed' if current_failed else 'complete' if current_complete else 'idle'
    agent = {'state': state, 'active_count': active, 'completed_count': completed, 'failed_count': failed,
             'updated_at': max((x.get('updated_at') or 0 for x in eligible), default=0)}
    return project_status({'tabs': [{}] if running else []}, known, network,
                          agent)
