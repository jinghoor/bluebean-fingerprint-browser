// Real DSH shell + production picker, with isolated environment/API fixtures.
// No real grants, accounts, input drafts, or foreground browser are changed.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const ROOT = path.resolve(__dirname, '..');
const OUT = path.join(ROOT, '.work/scope-picker-ui');
const read = p => fs.readFileSync(path.join(ROOT, p), 'utf8');

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: 'E:/chromium/src/out/Release-pgo/chrome.exe', headless: true });
  const errors = [], checks = [], writes = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
    page.setDefaultTimeout(12000);
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/api/**', r => /\/(describe|list|inventory|syncInspectManifest|modelCatalog|catalog|listProviders)$/.test(new URL(r.request().url()).pathname)
      ? r.continue() : r.fulfill({ json: { ok: false, error: 'isolated UI test' } }));
    const installed = read('DSH/profiles/web/node_modules/lan-seats/lib/client.js');
    const source = read('lan_seats/lib/client.js').replaceAll('}, LanEnvPanel);', "}, () => h(EnvScopePicker,{sessionId:'scope-ui-test'}));");
    await page.route('**/plugins/**', async r => {
      if (r.request().url().endsWith('/events')) return r.abort();
      const response = await r.fetch(), body = await response.text();
      if (body.includes("id: 'lan-seats'")) assert(body.includes(installed), 'exact installed plugin boundary');
      return r.fulfill({ response, body: body.replace(installed, source) });
    });
    let selected = ['fb-020'];
    const envs = [{ env_id: 'fb-008' }, { env_id: 'fb-012' }, { env_id: 'fb-020' },
      { env_id: 'env-6874d3ce', display_name: '本地 123' }];
    await page.route('http://127.0.0.1:8799/**', r => {
      const p = new URL(r.request().url()).pathname;
      if (p === '/v1/lan/session-scope') {
        const payload = r.request().postDataJSON();
        if (payload.env_ids) { selected = payload.env_ids; writes.push([...selected]); }
        return r.fulfill({ json: { ok: true, env_ids: selected, envs } });
      }
      return r.fulfill({ json: { ok: true, envs: [], states: {}, accounts: [], platforms: [], draft: null } });
    });
    await page.goto(JSON.parse(read('.dsh-web.json')).url);
    await page.locator('.GAkraW_root').waitFor();
    const preview = page.getByRole('button', { name: '继续', exact: true });
    if (await preview.count()) await preview.click();
    await page.locator('nav button[aria-label="我的环境"]').click();
    const trigger = page.locator('button[aria-label="浏览器环境"]:visible').first();
    const popup = page.locator('[data-lan-env-menu]:popover-open');
    const search = page.getByRole('searchbox', { name: '搜索浏览器环境' });
    await trigger.click();
    await popup.waitFor();
    assert.equal(await popup.evaluate(e => getComputedStyle(e).backgroundColor), 'rgb(255, 255, 255)');
    checks.push('real light theme uses opaque surface');

    await page.locator('[data-env-id="fb-008"]').click();
    await page.waitForFunction(() => document.querySelector('[data-lan-env-menu] [role="status"]')?.textContent === '已选 2 个');
    assert.deepEqual([...selected].sort(), ['fb-008', 'fb-020']);
    await search.fill('本地');
    await page.waitForFunction(() => document.querySelectorAll('[data-lan-env-menu] input[type="checkbox"]').length === 1);
    await popup.getByRole('button', { name: '清空', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[data-lan-env-menu] [role="status"]')?.textContent === '已选 0 个');
    assert.deepEqual(selected, []);
    checks.push('search, multi-selection and clear save only to fixture');
    await popup.getByRole('button', { name: '完成', exact: true }).click();
    assert.equal(await popup.count(), 0);

    // Put conspicuous text and controls directly behind the top-layer menu.
    await page.evaluate(() => {
      const backdrop = document.createElement('div');
      backdrop.id = 'scope-ui-backdrop';
      backdrop.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none;background:repeating-linear-gradient(0deg,#fce7f3 0px,#fce7f3 25px,#dbeafe 25px,#dbeafe 50px);color:#be185d;font:18px sans-serif;line-height:50px';
      backdrop.textContent = '底层消息 · Model · 发送按钮 · '.repeat(400);
      document.body.append(backdrop);
    });
    for (const [width, height, side] of [[1400,900,'bottom'], [360,640,'bottom'], [760,360,'top']]) {
      await page.setViewportSize({ width, height });
      await trigger.evaluate((e, side) => {
        e.parentElement.style.cssText = `position:fixed;right:16px;${side}:16px;z-index:2147483647`;
      }, side);
      await trigger.click();
      await popup.waitFor();
      await search.fill('');
      await page.waitForFunction(() => document.querySelectorAll('[data-lan-env-menu] input[type="checkbox"]').length === 4);
      await page.waitForTimeout(100);
      const box = await popup.boundingBox();
      assert(box.x >= 11 && box.y >= 11 && box.x + box.width <= width - 11 && box.y + box.height <= height - 11, JSON.stringify({ width, height, box }));
      assert(await search.evaluate(e => { const r=e.getBoundingClientRect(); return e === document.elementFromPoint(r.x+r.width/2,r.y+r.height/2); }), 'menu receives hits above high-z-index page');
      await search.fill('本地');
      await page.waitForFunction(() => document.querySelectorAll('[data-lan-env-menu] input[type="checkbox"]').length === 1);
      await page.waitForTimeout(100);
      const short = await popup.boundingBox(), anchor = await trigger.boundingBox();
      if (side === 'bottom') assert(Math.abs(short.y + short.height + 6 - anchor.y) < 2, 'short filtered list follows trigger');
      if (width === 1400) await page.screenshot({ path: path.join(OUT, 'after-light.png') });
      checks.push(`viewport ${width}x${height}: in bounds, real hit testing, filtered-list positioning`);
      await page.keyboard.press('Escape');
      assert.equal(await popup.count(), 0);
      assert(await trigger.evaluate(e => e === document.activeElement));
    }
    for (let i = 0; i < 40; i++) envs.push({ env_id: `test-${i}`, display_name: '较长环境名称'.repeat(5) });
    await trigger.click();
    await search.fill('');
    await page.waitForFunction(() => document.querySelectorAll('[data-lan-env-menu] input[type="checkbox"]').length === 44);
    await page.waitForTimeout(100);
    const longBox = await popup.boundingBox();
    assert(longBox.y >= 11 && longBox.y + longBox.height <= 349);
    assert(await popup.evaluate(e => e.scrollHeight > e.clientHeight), 'long menu can scroll');
    await popup.getByRole('button', { name: '完成', exact: true }).click();
    checks.push('44 long environment names: constrained height and scrollable menu');
    await trigger.evaluate(e => e.parentElement.style.setProperty('--dsw-alias-bg-base', '#18191c'));
    await trigger.click();
    assert.equal(await popup.evaluate(e => getComputedStyle(e).backgroundColor), 'rgb(24, 25, 28)');
    await page.screenshot({ path: path.join(OUT, 'after-dark-surface.png') });
    await page.mouse.click(20, 20);
    assert.equal(await popup.count(), 0);
    checks.push('opaque dark surface, Escape focus restoration, outside click dismissal');
    assert.deepEqual(errors, []);
    const report = { ok: true, checks, fixtureWrites: writes.length, pageErrors: errors };
    fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally { await browser.close(); }
})().catch(e => { console.error(e.message); process.exitCode = 1; });
