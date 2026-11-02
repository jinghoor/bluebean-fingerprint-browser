// Real DSH UI + disposable management/vault data. Never touch the user's profile.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium,request}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const ROOT=path.resolve(__dirname,'..'),OUT=path.join(ROOT,'.work/sidebar-catalog-sync');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const temp=fs.mkdtempSync(path.join(os.tmpdir(),'bluebean-sidebar-catalog-')),info=path.join(temp,'server.json');
 const child=spawn('python',['-B',path.join(__dirname,'env_management_server.py'),info],{cwd:ROOT,windowsHide:true,stdio:'ignore'});
 let browser,api,heldList;const errors=[],calls=[],checks=[];
 try{
  for(let i=0;i<150&&!fs.existsSync(info);i++)await pause(40);
  const fixture=JSON.parse(fs.readFileSync(info,'utf8')),base='http://127.0.0.1:'+fixture.port;
  api=await request.newContext({extraHTTPHeaders:{Authorization:'Bearer isolated-tests'}});
  browser=await chromium.launch({executablePath:'E:/chromium/src/out/Release-pgo/chrome.exe',headless:true,args:['--no-first-run','--disable-extensions']});
  const page=await browser.newPage({viewport:{width:1400,height:1000}});page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/**',route=>/\/(describe|list|inventory|syncInspectManifest|modelCatalog|catalog|listProviders)$/.test(new URL(route.request().url()).pathname)?route.continue():route.fulfill({json:{ok:false,error:'isolated test'}}));
  const installed=fs.readFileSync(path.join(ROOT,'DSH/profiles/web/node_modules/lan-seats/lib/client.js'),'utf8');
  await page.route('**/plugins/**',async route=>{if(route.request().url().endsWith('/events'))return route.abort();const response=await route.fetch(),body=await response.text();if(body.includes("id: 'lan-seats'"))assert(body.includes(installed),'exact installed plugin boundary');return route.fulfill({response,body:body.replace(installed,fs.readFileSync(path.join(ROOT,'lan_seats/lib/client.js'),'utf8'))});});
  await page.route('http://127.0.0.1:8799/**',async route=>{
   const u=new URL(route.request().url()),data=route.request().postDataJSON()||{};calls.push({path:u.pathname,action:data.action});
   if(u.pathname.startsWith('/v1/lan/manage/')){const res=await api.post(base+u.pathname,{data});if(u.pathname.endsWith('/list')&&heldList){const plan=heldList;heldList=null;plan.captured();await plan.gate;}return route.fulfill({response:res});}
   return route.fulfill({json:{ok:true}});
  });
  const cfg=JSON.parse(fs.readFileSync(path.join(ROOT,'.dsh-web.json'),'utf8'));
  await page.goto(cfg.url);await page.locator('.lan-env-sidebar-toggle').click();
  const sidebar=page.locator('.lan-env-sidebar-view');await sidebar.locator('.lan-env-sidebar-env').first().waitFor();
  assert.equal(await sidebar.locator('.lan-env-sidebar-env').count(),3);
  await page.locator('.lan-env-sidebar-create button').click();
  await page.locator('[name="display_name"]').fill('本地 123 同步验收');
  const id=await page.locator('.lm-enter [data-env-id]').getAttribute('data-env-id');
  assert.ok(id);
  let releaseOld,captured;
  const oldCaptured=new Promise(resolve=>{captured=resolve;});
  heldList={gate:new Promise(resolve=>{releaseOld=resolve;}),captured};
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await oldCaptured;
  await page.getByRole('button',{name:'保存环境',exact:true}).click();
  const row=sidebar.locator('.lan-env-sidebar-env[data-env-id="'+id+'"]');
  await row.waitFor();assert.match(await row.innerText(),/本地 123 同步验收/);
  checks.push('ordinary environment save appears immediately without mode switching');
  const oldResponse=page.waitForResponse(r=>r.url().endsWith('/v1/lan/manage/list'));
  releaseOld();await oldResponse;
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  assert.equal(await row.count(),1);
  checks.push('a late pre-save sidebar response cannot erase the new environment');
  await page.getByText('环境设置',{exact:true}).click();
  await page.locator('[name="display_name"]').fill('本地 123 已改名');
  await page.getByRole('button',{name:'保存环境',exact:true}).click();
  await row.filter({hasText:'本地 123 已改名'}).waitFor();
  checks.push('editing the name updates the existing sidebar catalog');
  // Out-of-band edits are picked up by the management page's existing list
  // read and shared with the sidebar; there is no new fast poll or proxy probe.
  const saved=await(await api.post(base+'/v1/lan/manage/accounts',{data:{action:'save',env_id:id,accounts:[{client_id:'catalog-account',platform_id:'facebook',login:'catalog@example.test'}]}})).json();
  assert.equal(saved.ok,true,JSON.stringify(saved));
  await sidebar.locator('.lan-env-sidebar-account[data-env-id="'+id+'"]').filter({hasText:'catalog@example.test'}).waitFor({timeout:20000});
  checks.push('saved accounts are synchronized by the shared catalog read');
  const accountNode=sidebar.locator('.lan-env-sidebar-account[data-env-id="'+id+'"]');
  await accountNode.evaluate(e=>{window.savedCatalogAccount=e;e.focus();});
  const listResponse=page.waitForResponse(r=>r.url().endsWith('/v1/lan/manage/list'),{timeout:20000});
  await listResponse;
  assert.equal(await accountNode.evaluate(e=>e===window.savedCatalogAccount&&document.activeElement===e),true);
  checks.push('unchanged catalog reads preserve row DOM and keyboard focus');
  fs.mkdirSync(OUT,{recursive:true});await page.screenshot({path:path.join(OUT,'created-environment.png'),animations:'disabled'});
  await page.getByText('更多维护操作',{exact:true}).click();
  page.once('dialog',d=>d.accept());
  await page.getByRole('button',{name:'移出列表，保留网站数据',exact:true}).click();
  await row.waitFor({state:'detached'});
  assert.equal(await sidebar.locator('.lan-env-sidebar-account[data-env-id="'+id+'"]').count(),0);
  checks.push('removing the environment also removes its sidebar account rows');
  assert.equal(calls.some(c=>c.path.endsWith('/proxy-test')||c.path.endsWith('/proxy-job')&&c.action==='start'),false,'catalog synchronization must not test proxies');
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(OUT,'report.json'),JSON.stringify({passed:true,checks,page_errors:errors,proxy_tests:0},null,2));
  console.log('PASS: sidebar catalog synchronization '+checks.length+' checks; '+OUT);
 }finally{if(browser)await browser.close();if(api)await api.dispose();child.kill();fs.rmSync(temp,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
