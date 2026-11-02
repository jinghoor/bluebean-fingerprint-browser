// Six-site reachability and domestic/foreign exits; real HTTP + headless Chromium.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium,request}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const ROOT=path.resolve(__dirname,'..'),OUT=path.join(ROOT,'.work/environment-startup-check/network-ui');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const temp=fs.mkdtempSync(path.join(os.tmpdir(),'bluebean-network-ui-')),info=path.join(temp,'server.json');
 const child=spawn('python',['-B',path.join(__dirname,'startup_accounts_server.py'),info],{cwd:ROOT,windowsHide:true,stdio:'ignore'});
 let browser,api;const errors=[],checks=[];
 try{
  for(let i=0;i<150&&!fs.existsSync(info);i++)await pause(40);
  const fixture=JSON.parse(fs.readFileSync(info,'utf8')),base='http://127.0.0.1:'+fixture.port;
  fs.mkdirSync(OUT,{recursive:true});api=await request.newContext();
  const control=async data=>(await(await api.post(base+'/__test/control',{data})).json()).counts;
  browser=await chromium.launch({executablePath:'E:/chromium/src/out/Release-pgo/chrome.exe',headless:true,args:['--no-first-run','--disable-extensions']});
  const page=await browser.newPage({viewport:{width:1280,height:960}});page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(e.message));
  await control({network_delay:1.8});await page.goto(fixture.url);
  // Domestic IP arrives while a foreign endpoint is still pending. Direct
  // browsing is already enabled and the refresh control correctly stays busy.
  await page.waitForFunction(()=>document.getElementById('ipnum').textContent==='180.101.49.12'&&result.checking_network&&result.unlocked);
  assert.equal(await page.locator('[data-open-account]').first().isDisabled(),false);
  assert.equal(await page.locator('#refresh').isDisabled(),true);
  checks.push('domestic exit appears progressively; direct browsing is not blocked');
  await page.waitForFunction(()=>result?.status==='direct'&&!result.checking_network);
  assert.equal(await page.locator('.svc').count(),6);
  assert.match(await page.locator('#services').innerText(),/百度 · 可连接/);
  assert.match(await page.locator('#services').innerText(),/哔哩哔哩 · 站点限制 · 23 ms · HTTP 412/);
  assert.equal(await page.locator('#current').textContent(),'180.101.49.12');
  assert.equal(await page.locator('#status.error').count(),0);
  checks.push('six sites show truthful per-site status without hiding mainland exit');
  await page.screenshot({path:path.join(OUT,'mainland-desktop.png'),fullPage:true,animations:'disabled'});
  await page.setViewportSize({width:480,height:960});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.screenshot({path:path.join(OUT,'mainland-narrow.png'),fullPage:true,animations:'disabled'});
  await page.setViewportSize({width:1280,height:960});
  const again=async mode=>{
   await control({network_mode:mode,network_delay:0});
   await page.locator('#refresh').click();
   await page.waitForFunction(()=>result?.status==='direct'&&!result.checking_network&&document.getElementById('refresh').disabled===false);
  };
  await again('usa');assert.equal(await page.locator('#ipnum').textContent(),'203.0.113.1');
  assert.match(await page.locator('#geo').textContent(),/US/);checks.push('overseas exit and geolocation remain visible');
  await again('ip_unavailable');assert.match(await page.locator('#status').textContent(),/网络已连通.*IP 查询服务/);
  assert.equal(await page.locator('#ipnum').textContent(),'出口 IP 暂未取得');
  assert.equal(await page.locator('[data-open-account]').first().isDisabled(),false);
  checks.push('online with blocked IP services is distinguished from network failure');
  await page.screenshot({path:path.join(OUT,'ip-lookup-unavailable.png'),animations:'disabled'});
  await again('offline');assert.match(await page.locator('#status').textContent(),/网络检测未能连通/);
  assert.equal(await page.locator('#ipnum').textContent(),'出口 IP 暂未取得');
  checks.push('unreachable probes show an explicit warning and no fabricated IP');
  await control({network_mode:'cold_recovery',network_delay:0,reset_network:true});
  const cold=await browser.newPage({viewport:{width:1280,height:960}}),checkRequests=[];
  cold.setDefaultTimeout(12000);cold.on('pageerror',e=>errors.push(e.message));
  cold.on('request',r=>{if(new URL(r.url()).pathname==='/check')checkRequests.push(r.postDataJSON());});
  await cold.goto(fixture.url);
  await cold.waitForFunction(()=>Object.values(result?.probes||{}).some(site=>site.retrying));
  assert.match(await cold.locator('#services').innerText(),/正在自动复测/);
  assert.equal(await cold.locator('#ipnum').textContent(),'180.101.49.12');
  assert.equal(await cold.locator('[data-open-account]').first().isDisabled(),false);
  assert.equal(await cold.locator('#refresh').isDisabled(),true);
  await cold.screenshot({path:path.join(OUT,'cold-connection-retesting.png'),animations:'disabled'});
  await cold.waitForFunction(()=>result?.status==='direct'&&!result.checking_network);
  assert.equal(await cold.locator('#services').getByText(/复测通过/).count(),4);
  assert.equal(await cold.locator('#services').getByText(/站点限制/).count(),1);
  assert.equal(await cold.locator('#services').getByText(/未连通/).count(),0);
  assert.equal(checkRequests.length,1);assert.equal(checkRequests[0].force,false);
  assert.equal((await control({})).probes,12,'only four transient site failures should be retried');
  checks.push('the first page load recovers cold connections automatically; no manual refresh or repeat IP lookup');
  await cold.screenshot({path:path.join(OUT,'cold-connection-recovered.png'),animations:'disabled'});
  await cold.reload();
  await cold.waitForFunction(()=>result?.status==='direct'&&!result.checking_network);
  assert.equal((await control({})).probes,12,'reload must reuse recovered results');
  assert.equal(await cold.locator('#services').getByText(/复测通过/).count(),4);
  checks.push('recovered results survive page reload without retesting the network');
  assert.deepEqual(errors,[]);fs.writeFileSync(path.join(OUT,'report.json'),JSON.stringify({passed:true,checks,page_errors:errors},null,2));
  console.log('PASS: startup network UI '+checks.length+' checks; '+OUT);
 }finally{if(browser)await browser.close();if(api)await api.dispose();child.kill();fs.rmSync(temp,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
