const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

(async()=>{
 const root=path.resolve(__dirname,'..'),cfg=JSON.parse(fs.readFileSync(path.join(root,'.dsh-web.json'),'utf8'));
 const browser=await chromium.launch({executablePath:'E:/chromium/src/out/Release-pgo/chrome.exe',headless:true,args:['--no-first-run','--disable-extensions']});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),calls=[],errors=[],overlayEvents=[],activationPlans=[];
  let listGate=null,statusSnapshot=null;
  const holdActivation=(reply={ok:true,message:'opened'})=>{let release;const gate=new Promise(resolve=>{release=resolve;});activationPlans.push({gate,reply});return release;};
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',message=>{if(message.text().startsWith('LAN_OVERLAY_UI:'))overlayEvents.push(message.text());});
  await page.route('**/api/**',route=>/\/(describe|list|inventory|syncInspectManifest|modelCatalog|catalog|listProviders)$/.test(new URL(route.request().url()).pathname)?route.continue():route.fulfill({json:{ok:false,error:'isolated test'}}));
  await page.route('**/plugins/**',async route=>{if(route.request().url().endsWith('/events'))return route.abort();const response=await route.fetch(),body=await response.text();return route.fulfill({response,body:body.includes("id: 'lan-seats'")?fs.readFileSync(path.join(root,'lan_seats/lib/client.js'),'utf8'):body});});
  await page.route('http://127.0.0.1:8799/**',async route=>{
   const url=new URL(route.request().url()),body=route.request().postDataJSON()||{};calls.push({path:url.pathname,body});
   if(url.pathname==='/v1/lan/manage/status')return route.fulfill({json:statusSnapshot||{ok:true,states:{},live_status:'connecting'}});
   if(url.pathname==='/v1/lan/manage/list'){if(listGate)await listGate;return route.fulfill({json:{ok:true,active:'fb-020',envs:[
    {env_id:'fb-020',display_name:'fb-020',account_count:3,selected_account_session:'fb-020 / Facebook',accounts:[{account_id:'acc-fb',platform:'Facebook',login:'operator@example.test',session_env_id:'fb-020 / Facebook'},
     {account_id:'acc-instagram',platform_id:'instagram',platform:'Instagram',login:'photo@example.test',session_env_id:'fb-020 / Instagram'},
     {account_id:'acc-custom',platform_id:'custom',platform:'自定义网站',url:'https://example.test/',login:'store@example.test',session_env_id:'fb-020 / Custom'}]},
    {env_id:'fb-008',display_name:'海外运营',account_count:0,accounts:[]}
   ]}});}
   if(url.pathname==='/v1/lan/manage/open'||url.pathname==='/v1/lan/manage/accounts'&&body.action==='open'){
    const plan=activationPlans.shift();if(plan)await plan.gate;else await new Promise(resolve=>setTimeout(resolve,350));
    return route.fulfill({json:plan?.reply||{ok:true,session_env_id:'fb-020 / Facebook'}});
   }
   return route.fulfill({json:{ok:true}});
  });
  await page.goto(cfg.url);await page.locator('.GAkraW_root').waitFor();
  const toggle=page.locator('.lan-env-sidebar-toggle');await toggle.waitFor();
  assert.equal(await toggle.getAttribute('aria-label'),'切换到我的环境');
  assert.equal(await toggle.locator('svg').count(),1,'mode switch should be an icon button');
  assert.equal(await page.locator('.GAkraW_sectionLabel').evaluate(e=>e.nextElementSibling?.classList.contains('lan-env-sidebar-toggle')),true,'mode switch icon should sit directly after the Workspaces heading');
  const heading=page.locator('.GAkraW_sectionLabel'),workspaceHeadingBox=await heading.boundingBox();
  const workspaceActions=['搜索会话','视图选项','添加工作区'].map(name=>page.getByRole('button',{name,exact:true}));
  for(const action of workspaceActions)assert.equal(await action.isVisible(),true,'native workspace tools should remain visible in workspace mode');
  await page.locator('.mP3x-q_sidebarCol').screenshot({path:path.join(root,'.work/env-management-proxy-upgrade/workspace-sidebar.png'),animations:'disabled'});
  await toggle.click();
  const view=page.getByLabel('环境和账号');await view.waitFor();
  await page.getByRole('button',{name:/海外运营/}).waitFor();
  assert.equal(await view.locator('.lan-env-sidebar-env img[data-platform-icon="environment"]').count(),2,'every environment should have a browser icon');
  for(const key of ['facebook','instagram','custom'])await view.locator('.lan-env-sidebar-account img[data-platform-icon="'+key+'"]').evaluate(img=>img.decode());
  const environmentHeadingBox=await heading.boundingBox(),toggleBox=await toggle.boundingBox();
  assert.equal(await heading.textContent(),'我的环境');
  assert.ok(Math.abs(environmentHeadingBox.x-workspaceHeadingBox.x)<1,'environment title should share the original workspace left edge');
  assert.ok(Math.abs(environmentHeadingBox.y-workspaceHeadingBox.y)<1,'mode change should preserve the heading baseline');
  assert.ok(toggleBox.x>=environmentHeadingBox.x+environmentHeadingBox.width&&toggleBox.x-environmentHeadingBox.x-environmentHeadingBox.width<12,'mode icon should remain next to the heading');
  for(const action of workspaceActions)assert.equal(await action.isVisible(),false,'workspace tools must not appear in environment mode');
  const focusableWorkspaceTools=await page.locator('.GAkraW_sectionHeader').evaluate(header=>[...header.querySelectorAll('button')].filter(button=>button.getClientRects().length>0).length);
  assert.equal(focusableWorkspaceTools,2,'mode switch and new environment should be visible in the environment heading');
  await page.getByRole('button',{name:/海外运营/}).evaluate(e=>{window.savedEnvNode=e;});
  const titleMutations=await page.locator('.GAkraW_sectionLabel').evaluate(async label=>{
   let changes=0;const observer=new MutationObserver(records=>{changes+=records.length;});observer.observe(label,{childList:true,subtree:true,characterData:true});
   for(let i=0;i<12;i++)await new Promise(requestAnimationFrame);
   observer.disconnect();return changes;
  });
  assert.equal(titleMutations,0,'idle sidebar must not rewrite its own heading every animation frame');
  await page.screenshot({path:path.join(root,'.work/env-management-proxy-upgrade/environment-sidebar.png'),animations:'disabled'});
  const envOpen=page.waitForRequest(request=>request.url().endsWith('/v1/lan/manage/open'));
  await page.getByRole('button',{name:/海外运营/}).click();await envOpen;
  assert.equal(await page.getByRole('button',{name:/海外运营/}).getAttribute('data-selected'),'true','selection should respond before activation finishes');
  assert.equal(await page.getByRole('button',{name:/海外运营/}).getAttribute('aria-current'),'false','pending selection must not be announced as the current environment');
  assert.equal(await page.getByRole('button',{name:/海外运营/}).getAttribute('data-pending'),'true','activation should show immediate pending feedback');
  await page.waitForFunction(()=>document.querySelector('.lan-env-sidebar-toggle')?.getAttribute('aria-pressed')==='true');
  assert.ok(calls.some(c=>c.path==='/v1/lan/manage/open'&&c.body.env_id==='fb-008'),'environment shortcut should activate its environment');
  const accountOpen=page.waitForRequest(request=>request.url().endsWith('/v1/lan/manage/accounts')&&request.postDataJSON()?.action==='open');
  await page.getByRole('button',{name:/Facebook.*operator@example.test/}).click();await accountOpen;
  assert.equal(await page.getByRole('button',{name:/Facebook.*operator@example.test/}).getAttribute('data-pending'),'true','account activation should show immediate pending feedback');
  assert.ok(calls.some(c=>c.path==='/v1/lan/manage/accounts'&&c.body.action==='open'&&c.body.account_id==='acc-fb'),'account shortcut should activate its isolated account session: '+JSON.stringify(calls));
  await page.waitForFunction(()=>document.querySelector('.lan-env-sidebar-view').dataset.busy==='false');
  assert.equal(await page.getByRole('button',{name:/海外运营/}).evaluate(e=>e===window.savedEnvNode),true,'activation must preserve row DOM');
  assert.equal(await page.getByRole('button',{name:/Facebook.*operator@example.test/}).locator('.lan-env-sidebar-dot').getAttribute('data-active'),'true','confirmed account must remain marked after activation');

  // Hold a request to check immediate feedback, duplicate coalescing, and last-choice wins.
  const release=holdActivation();
  const rapidStart=page.waitForRequest(r=>r.url().endsWith('/v1/lan/manage/open'));
  const feedback=await page.getByRole('button',{name:/海外运营/}).evaluate(async e=>{const start=performance.now();e.focus();e.click();await new Promise(requestAnimationFrame);return{ms:performance.now()-start,pending:e.dataset.pending,focused:document.activeElement===e};});
  await rapidStart;
  assert.equal(feedback.pending,'true');assert.equal(feedback.focused,true);assert.ok(feedback.ms<250,'pending feedback should be visible in the next frame');
  const countBeforeBurst=calls.filter(c=>c.path==='/v1/lan/manage/open'||c.path==='/v1/lan/manage/accounts'&&c.body.action==='open').length;
  await view.evaluate(el=>{const env=el.querySelector('[data-env-id="fb-008"].lan-env-sidebar-env'),account=el.querySelector('.lan-env-sidebar-account');env.click();account.click();env.click();account.click();});
  const lastChoice=page.waitForRequest(r=>r.url().endsWith('/v1/lan/manage/accounts')&&r.postDataJSON()?.action==='open');
  release();await lastChoice;await page.waitForFunction(()=>document.querySelector('.lan-env-sidebar-view').dataset.busy==='false');
  assert.equal(calls.filter(c=>c.path==='/v1/lan/manage/open'||c.path==='/v1/lan/manage/accounts'&&c.body.action==='open').length,countBeforeBurst+1,'rapid clicks should only send the running request and the final choice');

  // Success followed by queued failure must restore the actual successful environment.
  const releaseSuccess=holdActivation(),releaseFailure=holdActivation({ok:false,error:'模拟切换失败'});
  const successRequest=page.waitForRequest(r=>r.url().endsWith('/v1/lan/manage/open'));
  await page.getByRole('button',{name:/海外运营/}).click();await successRequest;
  await page.getByRole('button',{name:/Facebook.*operator@example.test/}).click();
  const failureRequest=page.waitForRequest(r=>r.url().endsWith('/v1/lan/manage/accounts')&&r.postDataJSON()?.action==='open');
  releaseSuccess();await failureRequest;releaseFailure();
  await page.getByRole('alert').filter({hasText:'模拟切换失败'}).waitFor();
  assert.equal(await page.getByRole('button',{name:/海外运营/}).getAttribute('aria-current'),'true','failed queued request must restore the last confirmed environment');
  assert.equal(await view.getAttribute('aria-busy'),'false');
  assert.equal(calls.filter(call=>call.path==='/v1/lan/manage/list').length,1,'activation should not reload the full environment list');

  // An older list response must not undo a newer activation, and focus events share one read.
  let releaseList;listGate=new Promise(resolve=>{releaseList=resolve;});
  const listRequest=page.waitForRequest(r=>r.url().endsWith('/v1/lan/manage/list'));
  await page.evaluate(()=>{for(let i=0;i<8;i++)document.dispatchEvent(new Event('visibilitychange'));});await listRequest;
  const activateDuringRefresh=page.waitForRequest(r=>r.url().endsWith('/v1/lan/manage/open'));
  await page.getByRole('button',{name:/海外运营/}).click();await activateDuringRefresh;
  await page.waitForFunction(()=>document.querySelector('.lan-env-sidebar-view').dataset.busy==='false');
  const listResponse=page.waitForResponse(r=>r.url().endsWith('/v1/lan/manage/list'));
  releaseList();await listResponse;listGate=null;
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  assert.equal(await page.getByRole('button',{name:/海外运营/}).getAttribute('aria-current'),'true','stale list response must not override activation');
  assert.equal(calls.filter(call=>call.path==='/v1/lan/manage/list').length,2,'concurrent visibility refreshes should coalesce');
  // Real polling updates only state: current is independent of color, no remount or focus loss.
  const firstEnv=view.locator('.lan-env-sidebar-env[data-env-id="fb-020"]'),otherEnv=view.locator('.lan-env-sidebar-env[data-env-id="fb-008"]');
  await firstEnv.evaluate(e=>{window.statusNode=e;e.focus();});
  const beforeBox=await firstEnv.boundingBox();
  const nameLayout=await view.evaluate(view=>{
   const result=[];
   for(const selector of ['.lan-env-sidebar-env','.lan-env-sidebar-account']){
    const row=view.querySelector(selector),name=row.querySelector(selector.endsWith('-env')?'.lan-env-sidebar-name>span:first-of-type':'span:first-of-type'),original=name.textContent;
    const measure=()=>{const dot=row.querySelector('.lan-env-sidebar-dot').getBoundingClientRect(),badge=row.querySelector('.lan-current-badge').getBoundingClientRect();return{dot:dot.x,badge:badge.x,height:row.getBoundingClientRect().height};};
    name.textContent='短';const short=measure();
    name.textContent='Facebook跨境运营环境超长名称不应挤压当前标签和状态指示灯'.repeat(5);const long=measure();
    result.push({short,long,truncated:name.scrollWidth>name.clientWidth,overflow:view.scrollWidth>view.clientWidth+1});name.textContent=original;
   }
   return result;
  });
  for(const layout of nameLayout){assert.deepEqual(layout.short,layout.long,'name length must not move the current badge/dot or change row height');assert.ok(layout.truncated);assert.equal(layout.overflow,false);}
  const sidebarName=firstEnv.locator('.lan-env-sidebar-name>span:first-of-type'),savedName=await sidebarName.textContent();
  await sidebarName.evaluate(el=>{el.textContent='Facebook美国跨境运营环境 · 这是一个很长很长的环境名称';});
  await page.locator('.mP3x-q_sidebarCol').screenshot({path:path.join(root,'.work/environment-runtime-status/long-names-sidebar.png'),animations:'disabled'});
  await sidebarName.evaluate((el,text)=>{el.textContent=text;},savedName);
  const setStatuses=async(first,second='running',extra={})=>{
   statusSnapshot={ok:true,live_status:'ready',active:'fb-020',active_session:'fb-020 / Facebook',states:{
    'fb-020':{state:first,label:{'agent-running':'Agent 正在操作','agent-complete':'Agent 操作完成','network-error':'网络异常',stopped:'未运行',running:'运行中',unknown:'状态未知'}[first],...extra},
    'fb-008':{state:second,label:second==='running'?'运行中':'未运行'},
    'fb-020 / Facebook':{state:first,label:'账号会话状态',...extra}
   }};
   await page.waitForFunction(state=>document.querySelector('.lan-env-sidebar-env[data-env-id="fb-020"] .lan-env-sidebar-dot')?.dataset.state===state,first);
  };
  await setStatuses('agent-running','running',{network_warning:true,agent_active_count:3,agent_completed_count:1,agent_failed_count:1,agent_warning:true});
  assert.equal(await firstEnv.locator('.lan-current-badge').getAttribute('data-current'),'true');
  assert.equal(await otherEnv.locator('.lan-current-badge').getAttribute('data-current'),'false');
  const expectDotColor=async(row,color)=>{await page.waitForFunction(({id,color})=>getComputedStyle(document.querySelector('.lan-env-sidebar-env[data-env-id="'+id+'"] .lan-env-sidebar-dot')).backgroundColor===color,{id:await row.getAttribute('data-env-id'),color});};
  await expectDotColor(otherEnv,'rgb(22, 179, 100)');
  assert.match(await firstEnv.innerText(),/网络异常/);
  assert.match(await firstEnv.innerText(),/3项/);
  assert.match(await firstEnv.getAttribute('title'),/3 项进行中，1 项完成，1 项失败/);
  assert.match(await firstEnv.getAttribute('title'),/1项失败/);
  assert.equal(await firstEnv.locator('.lan-env-sidebar-dot').evaluate(e=>getComputedStyle(e).animationName),'lan-agent-pulse');
  await page.emulateMedia({reducedMotion:'reduce'});
  assert.equal(await firstEnv.locator('.lan-env-sidebar-dot').evaluate(e=>getComputedStyle(e).animationName),'none');
  await page.emulateMedia({reducedMotion:'no-preference'});
  await page.locator('.mP3x-q_sidebarCol').screenshot({path:path.join(root,'.work/env-management-proxy-upgrade/environment-agent-running.png'),animations:'disabled'});
  await setStatuses('network-error');
  await expectDotColor(firstEnv,'rgb(245, 158, 11)');
  await setStatuses('agent-complete','running',{agent_completed_count:3});
  assert.equal(await firstEnv.locator('.lan-env-sidebar-dot').evaluate(e=>getComputedStyle(e).animationName),'none');
  await expectDotColor(firstEnv,'rgb(75, 114, 255)');
  await setStatuses('stopped');
  await expectDotColor(firstEnv,'rgb(138, 148, 163)');
  await setStatuses('unknown');
  assert.equal(await firstEnv.evaluate(e=>e===window.statusNode&&document.activeElement===e),true,'polling must retain the row and keyboard focus');
  const afterBox=await firstEnv.boundingBox();assert.equal(afterBox.height,beforeBox.height,'status transitions must not change row height');
  assert.equal(calls.filter(call=>call.path==='/v1/lan/manage/list').length,2,'status polling cannot reload the environment list');
  const list=page.locator('.GAkraW_listArea');
  await toggle.click();
  await page.waitForFunction(()=>document.querySelector('.lan-env-sidebar-toggle')?.getAttribute('aria-pressed')==='false');
  assert.notEqual(await list.evaluate(e=>getComputedStyle(e).display),'none','returning to Workspaces should restore the native session list');
  assert.equal(await view.isVisible(),false,'environment view should actually hide when returning to native workspaces');
  for(const action of workspaceActions)assert.equal(await action.isVisible(),true,'native workspace tools should return after switching back');
  const statusCount=calls.filter(call=>call.path==='/v1/lan/manage/status').length;
  await page.waitForTimeout(2300);
  assert.equal(calls.filter(call=>call.path==='/v1/lan/manage/status').length,statusCount,'hidden environment view must stop polling');
  assert.ok(Math.abs((await heading.boundingBox()).x-workspaceHeadingBox.x)<1,'returning to workspace should preserve the original title alignment');
  assert.equal(overlayEvents.some(line=>line.includes('"action":"expand"')),false,'environment shortcuts must not expand the DSH conversation pane');
  assert.deepEqual(errors,[]);
  console.log('PASS: sidebar activation, DOM/focus preservation, no idle heading mutations, coalesced rapid clicks, failure rollback, stale refresh protection, no conversation expansion. Next-frame feedback: '+feedback.ms.toFixed(1)+' ms (isolated backend).');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
