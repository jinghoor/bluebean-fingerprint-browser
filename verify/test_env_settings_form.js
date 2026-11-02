// Focused renderer smoke test for the real DSH client plugin component.
// This stays isolated from the user's browser and only exercises the create/edit
// state transitions with a tiny React hook/element shim.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
  path.join(__dirname, '..', 'lan_seats', 'lib', 'client.js'),
  'utf8',
);
const hooks = [];
let hookIndex = 0;
let plugin;

const React = {
  createElement(type, props, ...children) {
    return { type, props: props || {}, children };
  },
  useState(initial) {
    const index = hookIndex++;
    if (!(index in hooks)) hooks[index] = typeof initial === 'function' ? initial() : initial;
    return [hooks[index], value => {
      hooks[index] = typeof value === 'function' ? value(hooks[index]) : value;
    }];
  },
  useCallback: fn => fn,
  useEffect() {},
  useRef: initial => ({ current: initial }),
};

const window = {
  __ModuleLoader__: {
    load({ factory }) {
      plugin = factory(name => name === 'react' ? React : {});
    },
  },
};

vm.runInNewContext(source, {
  window,
  React,
  console,
  AbortSignal,
  EventTarget,
  crypto: { randomUUID: () => 'verify-12345678' },
  fetch: async () => ({ json: async () => ({ ok: true, envs: [], active: '' }) }),
  setInterval,
  clearInterval,
  confirm: () => true,
}, { filename: 'lan-seats-client.js' });

const registrations = [];
const slots = {
  register(config, component) {
    const registration = { config, component };
    registrations.push(registration);
    return registration;
  },
  inject(_name, register) {
    const result = register();
    if (result && typeof result.next === 'function') {
      for (const _entry of result) { /* registration is captured above */ }
    }
  },
};
plugin.apply({ effect() {}, slots });

const panel = registrations.find(entry => entry.config.key === 'lan-envs')?.component;
assert.equal(typeof panel, 'function', 'native “我的环境” panel is registered');

function render() {
  hookIndex = 0;
  return panel();
}

function walk(node, result = []) {
  if (node == null || typeof node === 'boolean') return result;
  if (Array.isArray(node)) {
    for (const child of node) walk(child, result);
  } else if (typeof node === 'object') {
    result.push(node);
    walk(node.children, result);
  }
  return result;
}

function buttonLabel(node) {
  return (node.children || []).flat(Infinity).filter(value => typeof value === 'string').join('');
}

function assertFullForm(nodes, envId) {
  const form = nodes.find(node => node.props?.['data-lan-env-editor']);
  assert.ok(form, 'editor form is rendered');
  assert.equal(form.props['data-lan-env-editor'], envId);

  const details = nodes.filter(node => node.type === 'details');
  assert.equal(details.length, 4, 'all four settings sections are present');
  assert.ok(details.every(section => section.props.open), 'all settings sections are expanded');

  const text = nodes.flatMap(node => node.children || [])
    .filter(value => typeof value === 'string').join(' ');
  for (const label of [
    '基础设置', '代理信息', '账号信息（本机加密）', '浏览器身份与启动设置',
    '代理地址', '账号名称', '更新密码', '更新 OTP 密钥',
    '首次启动页面', '新标签页主页', '区域设置', '请求语言',
    '指纹种子', '保存环境',
  ]) assert.ok(text.includes(label), `form includes ${label}`);
}

hooks[0] = { envs: [], active: '', error: '', api_version: 2 };
let nodes = walk(render());
const createButton = nodes.find(node => node.type === 'button' && buttonLabel(node).includes('＋ 新建环境'));
assert.ok(createButton, 'create action is visible from the environment list');
createButton.props.onClick();
nodes = walk(render());
assertFullForm(nodes, 'env-verify-1');

const existing = {
  env_id: 'fb-020',
  display_name: 'fb-020 测试号',
  proxy: 'socks5://127.0.0.1:55068',
  locale: 'en-US',
  accept_languages: 'en-US,en',
  fingerprint_seed: 'fb-020-0001',
  group: '', tags: '', notes: '',
  start_url: 'https://ipinfo.io/ip?e=fb-020', homepage: '',
};
hooks.length = 0;
hooks[0] = { envs: [existing], active: existing.env_id, error: '', api_version: 2 };
nodes = walk(render());
const editButton = nodes.find(node => node.type === 'button' && buttonLabel(node) === '编辑设置');
assert.ok(editButton, 'edit settings action is visible from the environment list');
editButton.props.onClick({ stopPropagation() {} });
nodes = walk(render());
assertFullForm(nodes, existing.env_id);
for (const value of [existing.proxy, existing.locale, existing.accept_languages, existing.fingerprint_seed]) {
  assert.ok(nodes.some(node => node.type === 'input' && node.props.value === value),
    `edit form preloads ${value}`);
}

console.log('PASS: create/edit forms render all settings; edit values are preloaded.');
