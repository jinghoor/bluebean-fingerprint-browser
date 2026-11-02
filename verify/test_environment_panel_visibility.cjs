// Real DSH shell in an isolated headless browser. All management calls are fixtures.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const ROOT = path.resolve(__dirname, '..');
const OUT = path.join(ROOT, '.work/environment-panel-visibility');
const installedClient = process.env.BLUEBEAN_TEST_INSTALLED_CLIENT === '1';

(async () => {
  fs.mkdirSync(OUT, {recursive: true});
  const browser = await chromium.launch({executablePath: 'E:/chromium/src/out/Release-pgo/chrome.exe', headless: true,
    args: ['--no-first-run', '--disable-extensions']});
  const checks = [], events = [], errors = [];
  let page;
  try {
    page = await browser.newPage({viewport: {width: 1400, height: 900}});
    page.setDefaultTimeout(12000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {
      const prefix = 'LAN_OVERLAY_UI:';
      if (message.text().startsWith(prefix)) events.push(JSON.parse(message.text().slice(prefix.length)));
    });
    await page.route('**/api/**', route => /\/(describe|list|inventory|syncInspectManifest|modelCatalog|catalog|listProviders)$/.test(new URL(route.request().url()).pathname)
      ? route.continue() : route.fulfill({json: {ok: false, error: 'isolated visibility test'}}));
    await page.route('**/plugins/**', async route => {
      if (route.request().url().endsWith('/events')) return route.abort();
      if (installedClient) return route.continue();
      const response = await route.fetch(), body = await response.text();
      return route.fulfill({response, body: body.includes("id: 'lan-seats'")
        ? fs.readFileSync(path.join(ROOT, 'lan_seats/lib/client.js'), 'utf8') : body});
    });
    const bridgePort = installedClient ? JSON.parse(fs.readFileSync(path.join(ROOT, '.lan-bridge.json'), 'utf8')).port : 8799;
    await page.route('http://127.0.0.1:' + bridgePort + '/**', route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.endsWith('/list')) return route.fulfill({json: {ok: true, active: 'visibility-test', envs: [
        {env_id: 'visibility-test', display_name: '显示联动测试', proxy: 'direct', account_count: 1, accounts: [
          {account_id: 'visibility-account', platform_id: 'facebook', platform: 'Facebook', login: 'visibility@example.test'}]}
      ]}});
      if (pathname.endsWith('/platforms')) return route.fulfill({json: {ok: true, platforms: []}});
      if (pathname.endsWith('/accounts')) return route.fulfill({json: {ok: true, accounts: []}});
      if (pathname.endsWith('/draft')) return route.fulfill({json: {ok: true, draft: null}});
      if (pathname.endsWith('/proxy-job')) return route.fulfill({json: {ok: true, job: null}});
      return route.fulfill({json: {ok: true, states: {}, envs: [], active: 'visibility-test'}});
    });
    const config = JSON.parse(fs.readFileSync(path.join(ROOT, '.dsh-web.json'), 'utf8'));
    await page.goto(config.url);
    const navigation = page.locator('nav button[aria-label="我的环境"]');
    await navigation.waitFor();
    await page.locator('.lan-env-sidebar-toggle').waitFor();
    const expandCount = () => events.filter(event => event.action === 'expand').length;
    let count = expandCount();
    await navigation.click();
    await page.locator('.lan-manager').waitFor();
    assert.ok(expandCount() > count, 'first My Environments click must reveal the native panel without first opening a DSH conversation');
    checks.push('first_open_without_conversation');
    assert.equal(await page.locator('.mP3x-q_centerCol .lan-manager').isVisible(), true, 'environment navigation must select the management main panel');
    assert.equal(await page.locator('.mP3x-q_rightbarCol').evaluate(element => element.getBoundingClientRect().width), 0,
      'environment navigation must keep the Agent conversation column closed');
    checks.push('does_not_open_conversation_rightbar');
    // Clicking the already-selected entry must also reopen its native clip after click-away.
    count = expandCount();
    await navigation.click();
    assert.ok(expandCount() > count, 'selected My Environments entry must still send reveal intent');
    await page.locator('.lan-manager').waitFor();
    checks.push('reopen_already_selected_panel');
    // A compact icon rail uses the same navigation entry.
    const compact = page.locator('button[aria-keyshortcuts="Control+Alt+B"]');
    await compact.click();
    count = expandCount();
    await navigation.click();
    assert.ok(expandCount() > count, 'compact sidebar navigation must reveal the panel');
    await page.locator('.lan-manager').waitFor();
    checks.push('compact_sidebar_navigation');
    await compact.click();
    count = expandCount();
    await page.locator('.lan-env-sidebar-toggle').click();
    await page.locator('.lan-env-sidebar-env').waitFor();
    await page.locator('.lan-env-sidebar-env').click();
    await page.locator('.lan-env-sidebar-account').click();
    assert.equal(expandCount(), count, 'mode/environment/account switches must not reveal the conversation');
    checks.push('environment_account_and_mode_switches_stay_quiet');
    count = expandCount();
    await page.locator('.lan-env-sidebar-create button').click();
    await page.locator('[data-editor-section="basic"] [name="display_name"]').waitFor();
    assert.ok(expandCount() > count, 'new environment must reveal the editor without a DSH conversation');
    checks.push('new_environment_reveals_editor');
    await page.screenshot({path: path.join(OUT, 'environment-editor.png'), animations: 'disabled'});
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(OUT, installedClient ? 'installed-client-validation.json' : 'validation.json'), JSON.stringify({ok: true, installed_client: installedClient, checks, errors}, null, 2));
    console.log('PASS: ' + checks.join(', '));
  } catch (error) {
    if (page) await page.screenshot({path: path.join(OUT, 'failure.png')}).catch(() => {});
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
