// Real DSH shell, production plugin, disposable API fixture, no user input/profile.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium,request}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const ROOT=path.resolve(__dirname,'..'),OUT=path.join(ROOT,'.work/stability-speed');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const temp=fs.mkdtempSync(path.join(os.tmpdir(),'bluebean-catalog-poll-')),info=path.join(temp,'server.json');
 const child=spawn('python',['-B',path.join(__dirname,'env_management_server.py'),info],{cwd:ROOT,windowsHide:true,stdio:'ignore'});
 let browser,api;const errors=[],calls=[],checks=[];
 try{
  for(let i=0;i<150&&!fs.existsSync(info);i++)await pause(40);
  const base='http://127.0.0.1:'+JSON.parse(fs.readFileSync(info,'utf8')).port;
  api=await request.newContext({extraHTTPHeaders:{Authorization:'Bearer isolated-tests'}});
  browser=await chromium.launch({executablePath:'E:/chromium/src/out/Release-pgo/chrome.exe',headless:true,args:['--no-first-run','--disable-extensions']});
  const page=await browser.newPage({viewport:{width:1400,height:1000}});page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/**',route=>/\/(describe|list|inventory|syncInspectManifest|modelCatalog|catalog|listProviders)$/.test(new URL(route.request().url()).pathname)?route.continue():route.fulfill({json:{ok:false,error:'isolated test'}}));
  const installed=fs.readFileSync(path.join(ROOT,'DSH/profiles/web/node_modules/lan-seats/lib/client.js'),'utf8');
  const source=fs.readFileSync(path.join(ROOT,process.argv.includes('--baseline')?'.work/stability-speed/client.before.js':'lan_seats/lib/client.js'),'utf8');
  await page.route('**/plugins/**',async route=>{
   if(route.request().url().endsWith('/events'))return route.abort();
   const response=await route.fetch(),body=await response.text();
   if(body.includes("id: 'lan-seats'"))assert(body.includes(installed),'exact installed plugin boundary');
   return route.fulfill({response,body:body.replace(installed,source)});
  });
  await page.route('http://127.0.0.1:8799/**',async route=>{
   const u=new URL(route.request().url()),data=route.request().postDataJSON()||{};
   calls.push({path:u.pathname,action:data.action,at:Date.now()});
   if(u.pathname.startsWith('/v1/lan/manage/'))return route.fulfill({response:await api.post(base+u.pathname,{data})});
   return route.fulfill({json:{ok:true}});
  });
  await page.goto(JSON.parse(fs.readFileSync(path.join(ROOT,'.dsh-web.json'),'utf8')).url);
  const preview=page.getByRole('button',{name:'继续',exact:true});if(await preview.count())await preview.click();
  await page.locator('nav button[aria-label="我的环境"]').click();
  await page.locator('.lan-manager').waitFor();
  await page.waitForFunction(()=>document.querySelector('.lm-row'));
  await page.waitForTimeout(500);
  const count=()=>calls.filter(c=>c.path.endsWith('/manage/list')).length;
  let before=count();
  await page.waitForTimeout(16000);
  const idleReads=count()-before;
  before=count();
  await page.evaluate(()=>{
   Object.defineProperty(document,'hidden',{configurable:true,value:true});
   Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});
   document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForTimeout(16000);
  const hiddenReads=count()-before;
  before=count();
  const resumed=page.waitForResponse(r=>r.url().endsWith('/v1/lan/manage/list'),{timeout:20000});
  await page.evaluate(()=>{
   delete document.hidden;delete document.visibilityState;
   document.dispatchEvent(new Event('visibilitychange'));
  });
  await resumed;
  const resumedReads=count()-before;
  assert.deepEqual(errors,[]);
  const baseline=process.argv.includes('--baseline');
  if(!baseline){
   assert.equal(idleReads,1,'full catalog should refresh once per 15 seconds');
   assert.equal(hiddenReads,0,'hidden management page must stop polling');
   assert.equal(resumedReads,1,'visibility restores the catalog immediately');
  }
  assert.equal(calls.some(c=>c.path.endsWith('/proxy-test')||c.path.endsWith('/proxy-job')&&c.action==='start'),false);
  checks.push('16 second visible idle window','16 second hidden window','immediate refresh on return','no proxy tests or page errors');
  fs.mkdirSync(OUT,{recursive:true});
  const report={passed:true,baseline,idle_window_ms:16000,idle_reads:idleReads,hidden_window_ms:16000,hidden_reads:hiddenReads,resumed_reads:resumedReads,checks};
  fs.writeFileSync(path.join(OUT,baseline?'polling-before.json':'polling-after.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
 }finally{if(browser)await browser.close();if(api)await api.dispose();child.kill();fs.rmSync(temp,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
