'use strict';
const query = new URLSearchParams(location.search);
const env = query.get('env'), key = query.get('key'), $ = id => document.getElementById(id);
const text = (id, value) => { $(id).textContent = value ?? ''; };
let result = null, busy = false, run = 0, accounts = [], opening = '', accountLoading = false;
const codes = new Map();
const passwords = new Map();
let codeRequests = 0;
text('lang', navigator.languages.join(', '));
text('tz', Intl.DateTimeFormat().resolvedOptions().timeZone);
text('intl', Intl.DateTimeFormat().resolvedOptions().locale);
text('ua', navigator.userAgent);
text('platform', navigator.userAgentData?.platform || navigator.platform);
text('screen', `${screen.width}×${screen.height} @${devicePixelRatio}x`);

function accountMessage(message, error = false) { text('account-message', message); $('account-message').dataset.tone = error ? 'error' : 'status'; }

async function api(path, data) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({env, key, ...data}), cache:'no-store', signal:controller.signal});
    const row = await response.json();
    if (!response.ok || !row.ok) throw new Error(row.error || '操作失败，请重试。');
    return row;
  } finally { clearTimeout(timeout); }
}
function button(label, action, primary = false) {
  const el = document.createElement('button');
  el.className = 'btn' + (primary ? ' primary' : '') + (action === 'close' ? ' danger' : '');
  el.textContent = label; el.disabled = busy && action !== 'close';
  el.addEventListener('click', () => decide(action)); $('actions').appendChild(el);
}
function render(row) {
  result = row;
  text('env', row.display_name || env); text('expected', row.expected_exit_ip || '尚未保存');
  const querying = row.status === 'checking' || row.checking_network;
  const ipLabel = row.exit_ip || (querying ? '正在查询出口 IP…' : '出口 IP 暂未取得');
  text('current', ipLabel); text('ipnum', ipLabel);
  text('mode', row.status === 'direct' ? '直连 · 不核对静态 IP' : row.ip_mode === 'dynamic' ? '动态 IP' : '静态 IP');
  $('progress').className = 'progress' + (querying ? ' running' : '');
  $('refresh').disabled = busy || querying;
  $('actions').replaceChildren(); $('geo').replaceChildren();
  for (const [label, k] of [['国家','country'],['地区','region'],['城市','city'],['时区','timezone'],['出口查询','latency_ms']]) {
    if (row.geo?.[k] !== undefined && row.geo[k] !== '') {
      const el = document.createElement('span');
      el.textContent = label + '：' + row.geo[k] + (k === 'latency_ms' ? ' ms' : ''); $('geo').appendChild(el);
    }
  }
  $('services').replaceChildren();
  for (const name of ['Google','Facebook','Instagram','TikTok','百度','哔哩哔哩']) {
    const item = row.probes?.[name], el = document.createElement('div'), dot = document.createElement('span');
    el.className = 'svc'; dot.className = 'dot ' + (item && !item.retrying ? (item.ok ? 'ok' : item.reachable ? 'restricted' : 'bad') : 'pending');
    dot.textContent = item && !item.retrying ? (item.ok ? '✓' : '×') : '·';
    const label = document.createElement('span');
    label.textContent = name + ' · ' + (item?.retrying ? '首次连接未成功，正在自动复测…' : item ?
      (item.ok ? '可连接' : item.reachable ? '站点限制' : '未连通') + ' · ' + (item.latency_ms ?? '—') + ' ms' +
      (item.status ? ' · HTTP ' + item.status : '') + (item.attempts > 1 ? item.recovered ? ' · 复测通过' : ' · 已复测' : '') : '等待检测');
    el.append(dot, label); $('services').appendChild(el);
  }
  let message = '', kind = '';
  text('explain', '检测结束前暂不能打开账号网站，你仍可切换到其他环境。');
  if (row.status === 'checking') {
    message = '正在通过当前环境的网络查询出口 IP 与网站连通性…'; button('关闭环境', 'close');
  } else if (row.status === 'mismatch' && !row.unlocked) {
    kind = 'warn'; message = '当前 IP 与环境中保存的 IP 不一致，继续访问网站存在账号风险。';
    text('explain', '请先确认本次 IP 变化，再选择右侧的账号。');
    button('接受本次风险，解锁账号', 'continue_once', true);
    button('改为动态 IP 并继续', 'dynamic'); button('不打开，关闭环境', 'close');
  } else if (row.status === 'unrecorded' && !row.unlocked) {
    kind = 'warn'; message = '此环境尚未保存出口 IP。请确认本次出口后保存，再打开账号网站。';
    button('保存当前 IP 并继续', 'baseline', true); button('使用动态 IP 并继续', 'dynamic'); button('不打开，关闭环境', 'close');
  } else if (row.status === 'error' || row.status === 'stale') {
    kind = 'error'; message = row.error || '检测失败，请重试。'; button('关闭环境', 'close');
  } else {
    kind = 'success';
    message = row.status === 'direct' ? (row.checking_network ? '直连模式，已允许浏览；正在查询出口 IP 与网站连通性…' : row.warning || '直连模式，当前出口 IP 与网站检测结果如下，不核对静态 IP。') : row.decision === 'continue_once' ?
      '已接受本次 IP 变化风险，可以打开账号网站。' : row.ip_mode === 'dynamic' || row.decision === 'dynamic' ?
      '代理已连接，当前使用动态 IP 模式。' : '出口 IP 检查通过，可以打开账号网站。';
    text('explain', '请选择右侧已保存的账号。单个网站未连通不代表代理不可用。'); button('关闭环境', 'close');
  }
  if (row.warning) kind = 'warn';
  $('status').className = 'status fade ' + kind; text('status', message);
  text('network-note', '所有检测沿用当前环境的连接方式。站点限制不代表环境断网，也不会隐藏已取得的出口 IP。');
  updateAccountButtons();
}
function updateAccountButtons() {
  const allowed = !!result?.unlocked && !busy;
  text('account-hint', allowed ? '选择账号，在独立登录会话中打开。' : '完成左侧检测和风险确认后，即可打开账号。');
  for (const el of $('account-list').querySelectorAll('[data-open-account]')) {
    const account = accounts.find(a => a.account_id === el.dataset.openAccount);
    el.disabled = !allowed || !!opening || !account?.can_open;
    el.textContent = opening === account?.account_id ? '打开中' : '打开';
    el.title = !account?.can_open ? '请在“我的环境”中补充账号网站地址' : allowed ? '打开此账号的网站' : '请先完成代理检测和风险确认';
  }
}
function hidePassword(state) {
  state.generation = (state.generation || 0) + 1;
  clearTimeout(state.timer); state.valueText = ''; state.deadline = 0; state.loading = false;
  if (state.value) { state.value.textContent = '••••••••'; state.copy.disabled = true; state.reveal.textContent = '显示'; state.reveal.disabled = false; }
}
function clearCredentials() {
  for (const state of passwords.values()) hidePassword(state);
  for (const state of codes.values()) { state.code = ''; state.deadline = 0; state.generation = (state.generation || 0) + 1; state.loading = false; if (state.value) state.value.textContent = '······'; }
}
function renderAccounts() {
  clearCredentials(); passwords.clear(); codes.clear();
  const list = $('account-list'); list.replaceChildren(); text('account-count', accounts.length ? String(accounts.length) : '');
  if (!accounts.length) {
    const empty = document.createElement('p'); empty.className = 'account-empty';
    empty.textContent = '此环境还没有保存账号。请在“我的环境”中编辑此环境并添加账号。'; list.appendChild(empty); return;
  }
  for (const account of accounts) {
    const card = document.createElement('article'); card.className = 'account'; card.dataset.account = account.account_id;
    const main = document.createElement('div'); main.className = 'account-main';
    if (account.icon) { const icon = document.createElement('img'); icon.src = account.icon; icon.alt = ''; icon.width = icon.height = 24; main.appendChild(icon); }
    const identity = document.createElement('div'); identity.className = 'account-identity';
    const name = document.createElement('strong'); name.textContent = account.platform || '网站账号'; identity.appendChild(name); main.appendChild(identity);
    const open = document.createElement('button'); open.className = 'btn account-open'; open.dataset.openAccount = account.account_id;
    open.setAttribute('aria-label', '打开 ' + (account.platform || '') + ' ' + (account.login || ''));
    open.addEventListener('click', () => openAccount(account)); main.appendChild(open); card.appendChild(main);
    const details = document.createElement('div'); details.className = 'account-details';
    const loginRow = document.createElement('div'); loginRow.className = 'account-field account-login-row';
    const loginLabel = document.createElement('span'); loginLabel.className = 'credential-label'; loginLabel.textContent = '账号'; loginRow.appendChild(loginLabel);
    const login = document.createElement('span'); login.className = 'account-login-value'; login.textContent = account.login || '未设置'; login.title = account.login || ''; loginRow.appendChild(login);
    if (account.login) {
      const copyLogin = document.createElement('button'); copyLogin.className = 'account-login-copy'; copyLogin.type = 'button'; copyLogin.textContent = '复制';
      copyLogin.setAttribute('aria-label', '复制 ' + (account.platform || '') + ' 登录账号');
      copyLogin.addEventListener('click', async () => { try { await copyText(account.login); copyLogin.textContent = '已复制'; clearTimeout(copyLogin.copiedTimer); copyLogin.copiedTimer = setTimeout(() => { if (copyLogin.isConnected) copyLogin.textContent = '复制'; }, 1600); } catch (error) { accountMessage(error.message, true); } });
      loginRow.appendChild(copyLogin);
    }
    details.appendChild(loginRow);
    if (account.username) {
      const usernameRow = document.createElement('div'); usernameRow.className = 'account-field';
      const usernameLabel = document.createElement('span'); usernameLabel.className = 'credential-label'; usernameLabel.textContent = '用户名';
      const username = document.createElement('span'); username.className = 'account-login-value'; username.textContent = account.username; username.title = account.username;
      usernameRow.append(usernameLabel, username); details.appendChild(usernameRow);
    }
    card.appendChild(details);
    const credential = document.createElement('div'); credential.className = 'account-credential';
    const label = document.createElement('span'); label.className = 'credential-label'; label.textContent = '密码'; credential.appendChild(label);
    const passwordValue = document.createElement('span'); passwordValue.className = 'credential-value';
    passwordValue.textContent = account.has_password ? '••••••••' : '未保存'; credential.appendChild(passwordValue);
    if (account.has_password) {
      const reveal = document.createElement('button'); reveal.className = 'credential-reveal'; reveal.type = 'button'; reveal.textContent = '显示';
      reveal.setAttribute('aria-label', '显示或隐藏 ' + account.platform + ' 密码');
      const copy = document.createElement('button'); copy.className = 'credential-reveal'; copy.type = 'button'; copy.textContent = '复制'; copy.disabled = true;
      copy.setAttribute('aria-label', '复制 ' + account.platform + ' 密码');
      const state = {value:passwordValue, reveal, copy, generation:0}; passwords.set(account.account_id, state);
      reveal.addEventListener('click', () => state.valueText ? hidePassword(state) : loadPassword(account.account_id));
      copy.addEventListener('click', () => copyPassword(account.account_id)); credential.append(reveal, copy);
    }
    card.appendChild(credential);
    const otp = document.createElement('div'); otp.className = 'account-otp';
    const otpLabel = document.createElement('span'); otpLabel.className = 'credential-label'; otpLabel.textContent = '2FA'; otp.appendChild(otpLabel);
    if (account.has_otp) {
      const value = document.createElement('button'); value.className = 'otp-code'; value.textContent = '······'; value.title = '点击复制验证码';
      value.setAttribute('aria-label', '复制 ' + account.platform + ' 2FA 验证码');
      const remaining = document.createElement('button'); remaining.className = 'otp-time'; remaining.textContent = '生成中…'; remaining.type = 'button';
      const state = {value, remaining, generation:0}; codes.set(account.account_id, state);
      remaining.addEventListener('click', () => loadCode(account.account_id)); value.addEventListener('click', () => copyCode(account.account_id));
      otp.append(value, remaining);
    } else { const note = document.createElement('span'); note.className = 'muted'; note.textContent = '未设置'; otp.appendChild(note); }
    card.appendChild(otp); list.appendChild(card);
  }
  updateAccountButtons(); refreshCodes();
}
async function loadAccounts() {
  if (accountLoading) return; accountLoading = true; $('accounts-refresh').disabled = true;
  clearCredentials();
  try { const row = await api('/accounts', {}); accounts = row.accounts; renderAccounts(); accountMessage(''); }
  catch (error) { accountMessage('账号载入失败：' + error.message, true); refreshCodes(); }
  finally { accountLoading = false; $('accounts-refresh').disabled = false; }
}
async function openAccount(account) {
  if (opening || busy || !result?.unlocked) return;
  opening = account.account_id; updateAccountButtons(); accountMessage('');
  for (const state of passwords.values()) hidePassword(state);
  try { await api('/account', {action:'open', account_id:account.account_id}); accountMessage('已切换到 ' + account.platform + ' 账号网站。'); }
  catch (error) { accountMessage(error.message, true); }
  finally { opening = ''; updateAccountButtons(); }
}
async function loadCode(id) {
  const state = codes.get(id);
  if (!state || document.hidden || !state.value.isConnected) return;
  if (state.loading) return state.pending;
  if (codeRequests >= 4) return;
  codeRequests++; state.loading = true; const generation = state.generation;
  if (!state.deadline || state.deadline <= performance.now()) state.value.textContent = '······';
  state.pending = (async () => {
    try {
      const row = await api('/account', {action:'totp', account_id:id});
      if (codes.get(id) !== state || state.generation !== generation || document.hidden) return;
      state.code = row.code; state.deadline = performance.now() + Math.max(0, row.expires_at - row.server_time) * 1000;
      state.value.textContent = row.code; state.failed = false; state.remaining.textContent = Math.max(0, Math.ceil((state.deadline-performance.now())/1000)) + ' 秒后更新';
    } catch (error) {
      if (codes.get(id) !== state || state.generation !== generation) return;
      state.failed = true; state.code = ''; state.value.textContent = '······'; state.remaining.textContent = '重试'; accountMessage(error.message, true);
    } finally { codeRequests--; if (state.generation === generation) { state.loading = false; state.pending = null; } }
  })();
  return state.pending;
}
async function refreshCodes() {
  const ids = [...codes.keys()]; let index = 0;
  await Promise.all(Array.from({length:Math.min(4,ids.length)}, async () => { while (index < ids.length && !document.hidden) await loadCode(ids[index++]); }));
}
async function copyText(value) {
  try { await navigator.clipboard.writeText(value); }
  catch {
    const field = document.createElement('textarea'); field.value = value; field.className = 'clipboard-field'; document.body.appendChild(field);
    try { field.select(); if (!document.execCommand('copy')) throw new Error('复制失败，请手动复制。'); }
    finally { field.value = ''; field.remove(); }
  }
}
async function copyCode(id) {
  const state = codes.get(id); if (!state) return;
  if (state.loading) await state.pending;
  if (codes.get(id) !== state) return;
  try {
    if (!state.code || state.deadline - performance.now() < 1000) await loadCode(id);
    if (codes.get(id) !== state || !state.code || state.deadline <= performance.now()) return;
    await copyText(state.code); state.remaining.textContent = '已复制'; state.copiedUntil = performance.now() + 1600;
  } catch (error) { accountMessage(error.message, true); }
}
async function loadPassword(id) {
  const state = passwords.get(id); if (!state || state.loading || document.hidden) return;
  state.loading = true; state.reveal.disabled = true; const generation = state.generation;
  try {
    const row = await api('/account', {action:'credential', field:'password', account_id:id});
    if (passwords.get(id) !== state || generation !== state.generation || document.hidden) return;
    state.valueText = row.value; state.deadline = performance.now() + Math.max(0, row.expires_at - row.server_time) * 1000;
    state.value.textContent = row.value; state.reveal.textContent = '隐藏'; state.copy.disabled = false;
    state.timer = setTimeout(() => hidePassword(state), Math.max(1, state.deadline-performance.now()));
  } catch (error) { if (generation === state.generation) { hidePassword(state); accountMessage(error.message, true); } }
  finally { if (generation === state.generation) { state.loading = false; state.reveal.disabled = false; } }
}
async function copyPassword(id) {
  const state = passwords.get(id); if (!state?.valueText || state.deadline <= performance.now()) { if (state) hidePassword(state); return; }
  try { await copyText(state.valueText); accountMessage('密码已复制。页面中的密码将在 15 秒内自动隐藏。'); }
  catch (error) { accountMessage(error.message, true); }
}
setInterval(() => {
  for (const state of passwords.values()) if (state.valueText && state.deadline <= performance.now()) hidePassword(state);
  if (document.hidden) return;
  for (const [id, state] of codes) {
    if (state.failed || !state.value?.isConnected) continue;
    const seconds = Math.max(0, Math.ceil(((state.deadline || 0) - performance.now()) / 1000));
    if (performance.now() >= (state.copiedUntil || 0)) state.remaining.textContent = seconds ? seconds + ' 秒后更新' : '正在更新…';
    if (!seconds) { state.value.textContent = '······'; loadCode(id); }
  }
}, 1000);
document.addEventListener('visibilitychange', () => { if (document.hidden) clearCredentials(); else refreshCodes(); });
window.addEventListener('pagehide', clearCredentials);
window.addEventListener('blur', () => { for (const state of passwords.values()) hidePassword(state); });
async function start(force = false) {
  if (busy) return; const token = ++run; busy = true; render({status:'checking', display_name:env});
  try {
    let row = await api('/check', {force});
    while (token === run && (row.status === 'checking' || row.checking_network || (row.allowed && !row.unlocked))) {
      busy = !row.unlocked; render(row); await new Promise(resolve => setTimeout(resolve, 350));
      const response = await fetch('/status?' + new URLSearchParams({env,key}), {cache:'no-store'});
      row = await response.json(); if (!response.ok || !row.ok) throw new Error(row.error || '检测服务连接失败');
    }
    if (token === run) {
      busy = false; render(row);
      if (row.inherited && row.unlocked && row.auto_open_url) location.replace(row.auto_open_url);
    }
  } catch (error) { if (token === run) { busy = false; render({status:'error', error:error.message, display_name:env}); } }
}
async function decide(action) {
  if (busy && action !== 'close') return; busy = true; updateAccountButtons();
  for (const el of $('actions').querySelectorAll('button')) el.disabled = true;
  try {
    await api('/decision', {action, receipt:result?.receipt || ''});
    if (action === 'close') { ++run; text('status', '环境已关闭。'); return; }
    const response = await fetch('/status?' + new URLSearchParams({env,key}), {cache:'no-store'});
    const row = await response.json(); if (!response.ok || !row.ok) throw new Error(row.error || '状态读取失败，请重试');
    busy = false; render(row); $('account-list').querySelector('[data-open-account]:not(:disabled)')?.focus();
  } catch (error) {
    busy = false; text('status', error.message); $('status').className = 'status error'; $('refresh').disabled = false;
    for (const el of $('actions').querySelectorAll('button')) el.disabled = false; updateAccountButtons();
  }
}
$('refresh').addEventListener('click', () => start(true));
$('accounts-refresh').addEventListener('click', loadAccounts);
loadAccounts(); start();
