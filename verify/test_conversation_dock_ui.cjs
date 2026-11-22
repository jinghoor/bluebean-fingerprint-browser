// Regression checks for the product-owned right conversation dock.
// This test only parses the plugin/native contract. It never starts DSH or a
// browser window, so it is safe to run while the user's browser is open.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const client = fs.readFileSync(
  path.join(__dirname, '../lan_seats/lib/client.js'), 'utf8');
const native = fs.readFileSync(
  path.join(__dirname, '../patches/new-files/chrome/browser/env/env_overlay.cc'),
  'utf8');

assert.match(client, /function ConversationDockToggle\(\{ sessionId \}\)/);
assert.match(client, /lan-conversation-dock-v1/);
assert.match(client, /lan-conversation-dock-mode-v1/);
assert.match(client, /action: next \? 'dock' : 'undock'/);
assert.match(client, /fixed_right: next && modeValue === 'fixed'/);
assert.match(client, /lan-conversation-dock-mode-toggle/);
assert.match(client, /data-lan-conversation-docked=true/);
assert.match(client, /mP3x-q_frame/);
assert.match(client, /\.mP3x-q_sidebarCol\{position:absolute/);
assert.match(client, /\.mP3x-q_centerCol\{position:absolute/);
assert.match(client, /将会话框停靠到网页右侧/);
assert.match(client, /恢复会话框原状/);

// Exercise the component's initial notification in a tiny DOM/React fixture.
// This catches regressions where the button is rendered but the native host is
// never told which layout to use.
const definition = client.match(
  /^    function ConversationDockToggle\(\{ sessionId \}\) \{[\s\S]*?^    \}/m)?.[0];
assert.ok(definition, 'conversation dock component must remain extractable');
const actions = [];
const styleProperties = new Map();
const element = {
  dataset: {},
  style: { setProperty: (name, value) => styleProperties.set(name, value) },
};
const context = {
  React: {
    useState: initial => [initial(), () => {}],
    useEffect: effect => effect(),
  },
  document: {
    documentElement: element,
    querySelector: () => ({ getBoundingClientRect: () => ({ width: 280 }) }),
  },
  localStorage: { getItem: () => null, setItem: () => {} },
  innerWidth: 1200,
  requestAnimationFrame: callback => { callback(); return 1; },
  cancelAnimationFrame: () => {},
  console: { info: message => actions.push(JSON.parse(message.slice('LAN_OVERLAY_UI:'.length))) },
  h: (type, props) => ({ type, props }),
};
const component = vm.runInNewContext(`(${definition})`, context);
component({ sessionId: 'fixture-session' });
assert.equal(element.dataset.lanConversationDocked, 'false');
assert.equal(element.dataset.lanConversationDockedReady, 'true');
assert.equal(styleProperties.get('--lan-sidebar-width'), '280px');
assert.equal(actions.at(-1).action, 'undock');
assert.equal(actions.at(-1).conversation_width, 408);
const actionCountAfterFirstMount = actions.length;
component({ sessionId: 'fixture-session' });
assert.equal(actions.length, actionCountAfterFirstMount,
  'remounting the header must not trigger a second native relayout');

assert.match(native, /SetPageBackground\(bool transparent\)/);
assert.match(native, /DockedSidebarBounds\(\)/);
assert.match(native, /DockedConversationBounds\(\)/);
assert.match(native, /return IsInteractivePoint\(point\)/);
assert.match(native, /\*action != "dock"/);
assert.match(native, /\*action != "undock"/);
assert.match(native, /\*action != "dock-mode"/);
assert.match(native, /fixed_right_/);
assert.match(native, /right_reserved/);
assert.match(native, /canvas->DrawRect\(gfx::RectF\(DockedSidebarBounds\(\)\)/);
assert.match(native, /canvas->DrawRect\(gfx::RectF\(DockedConversationBounds\(\)\)/);

console.log('PASS: conversation dock button, persisted mode, transparent middle, and native hit regions are wired together.');
