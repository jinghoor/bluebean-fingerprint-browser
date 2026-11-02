const fs = require('node:fs');
const {chromium} = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'E:/chromium/src/out/Release-pgo/chrome.exe',headless:true,args:['--no-first-run','--disable-extensions']});
 try {
  const page=await browser.newPage({viewport:{width:1280,height:1000}});
  const paths=[]; const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{const u=new URL(r.url()); if(u.protocol.startsWith('http')) paths.push({path:u.pathname,method:r.method()});});
  await page.route('http://127.0.0.1:8799/**',route=>route.fulfill({json:{ok:true,envs:[],active:'',scopes:[],api_version:2}}));
  await page.route('**/api/**',route=>/\/(describe|list|inventory|syncInspectManifest|modelCatalog|catalog|listProviders)$/.test(new URL(route.request().url()).pathname)?route.continue():route.fulfill({json:{ok:false,error:'Isolated verification: mutation blocked'}}));
  await page.route('**/plugins/**',async route=>{
   if(new URL(route.request().url()).pathname.endsWith('/events'))return route.abort();
   const response=await route.fetch();const body=await response.text();
   if(body.includes("id: 'lan-seats'")) {
    console.log('Replacing lan-seats script');
    await route.fulfill({response,body:fs.readFileSync('E:/FingerprintBrowser/lan_seats/lib/client.js','utf8')});
   } else { console.log('plugin response',response.status(),body.slice(0,80));await route.fulfill({response}); }
  });
  await page.goto(JSON.parse(fs.readFileSync('E:/FingerprintBrowser/.dsh-web.json','utf8')).url);
  await page.getByText('我的环境',{exact:true}).waitFor();
  await page.getByText('我的环境',{exact:true}).click();
  await page.locator('[data-lan-env-panel]').waitFor();
  fs.writeFileSync('E:/FingerprintBrowser/.work/env-management-upgrade/runtime-probe.json',JSON.stringify({paths,errors,html:await page.locator('[data-lan-env-panel]').innerText()},null,2));
  console.log(JSON.stringify({errors,paths:paths.filter(p=>/lan|react|module|client|plugin/.test(p.path)),text:await page.locator('[data-lan-env-panel]').innerText()}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
