// Read-only production smoke test for the installed tabbed environment workspace.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium,request}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async()=>{
 const root=path.resolve(__dirname,'..'),cfg=JSON.parse(fs.readFileSync(path.join(root,'.lan-bridge.json')));
 const api=await request.newContext({extraHTTPHeaders:{Authorization:'Bearer '+cfg.token}});
 const browser=await chromium.launch({executablePath:'E:/chromium/src/out/Release-pgo/chrome.exe',headless:true,args:['--no-first-run','--disable-extensions']});
 try {
 const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[],mutations=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/**',route=>/\/(describe|list|inventory|syncInspectManifest|modelCatalog|catalog|listProviders)$/.test(new URL(route.request().url()).pathname)?route.continue():route.fulfill({json:{ok:false,error:'read-only smoke test'}}));
 await page.route('**/plugins/**',async route=>{const u=new URL(route.request().url());if(u.pathname.endsWith('/events'))return route.abort();const res=await route.fetch();const body=await res.text();return route.fulfill({response:res,body});});
 await page.route('http://127.0.0.1:'+cfg.port+'/**',async route=>{const u=new URL(route.request().url()),body=route.request().postDataJSON()||{};if(u.pathname==='/v1/lan/manage/list'||u.pathname==='/v1/lan/manage/maintenance'||u.pathname==='/v1/lan/manage/status')return route.fulfill({response:await api.post('http://127.0.0.1:'+cfg.port+u.pathname,{data:{}})});if(u.pathname==='/v1/lan/manage/import'&&body.action==='history')return route.fulfill({response:await api.post('http://127.0.0.1:'+cfg.port+u.pathname,{data:body})});if(u.pathname==='/v1/lan/manage/proxy-job'&&['status',undefined].includes(body.action))return route.fulfill({response:await api.post('http://127.0.0.1:'+cfg.port+u.pathname,{data:body})});if(u.pathname==='/v1/lan/manage/draft'&&['read',undefined].includes(body.action))return route.fulfill({response:await api.post('http://127.0.0.1:'+cfg.port+u.pathname,{data:body})});if(u.pathname==='/v1/lan/manage/accounts'&&['list',undefined].includes(body.action))return route.fulfill({response:await api.post('http://127.0.0.1:'+cfg.port+u.pathname,{data:body})});if(u.pathname.startsWith('/v1/lan/manage/')||u.pathname==='/v1/lan/activate')mutations.push(u.pathname);return route.fulfill({json:{ok:false,error:'blocked read-only mutation'}});});
 await page.goto(JSON.parse(fs.readFileSync(path.join(root,'.dsh-web.json'),'utf8')).url);
 await page.getByRole('button',{name:'添加工作区',exact:true}).waitFor();
 const nativeCreateBox=await page.getByRole('button',{name:'添加工作区',exact:true}).boundingBox();
 await page.getByText('我的环境',{exact:true}).click();await page.locator('.lm-row').first().waitFor();
 const rows=(await(await api.post('http://127.0.0.1:'+cfg.port+'/v1/lan/manage/list',{data:{}})).json()).envs;
 assert.equal(await page.locator('.lm-row').count(),Math.min(25,rows.length));
 await page.screenshot({path:path.join(root,'.work/env-management-proxy-upgrade/environment-list.png'),animations:'disabled'});
 assert.equal(await page.locator('.lan-manager').getAttribute('data-workspace-version'),'6');
 const metrics=await page.getByRole('main',{name:'我的环境内容'}).evaluate(e=>({height:e.clientHeight,scroll:e.scrollHeight,overflow:getComputedStyle(e).overflowY,bar:getComputedStyle(e,'::-webkit-scrollbar').display}));
 assert.ok(metrics.height>0&&metrics.height<1000);assert.equal(metrics.overflow,'auto');assert.notEqual(metrics.bar,'none');
 for(const tab of ['账号','代理检测','备份与恢复']){await page.getByRole('tab',{name:tab,exact:true}).click();await page.locator('.lan-manager').waitFor();if(tab==='代理检测'){assert.equal(await page.getByRole('button',{name:'测试全部代理',exact:true}).isEnabled(),true);await page.screenshot({path:path.join(root,'.work/env-management-proxy-upgrade/proxy-page.png'),animations:'disabled'});}}
 assert.equal(await page.locator('[name="transfer-password"]').count(),0);
 await page.getByRole('button',{name:'环境配置备份',exact:true}).click();
 await page.getByRole('button',{name:'备份并导出配置',exact:true}).waitFor();
 await page.getByRole('button',{name:'返回上一级',exact:true}).click();
 const out=path.join(root,'.work/env-workspace-navigation/deployed');fs.mkdirSync(out,{recursive:true});
 await page.screenshot({path:path.join(out,'maintenance.png'),animations:'disabled'});
 await page.locator('.lan-env-sidebar-toggle').click();
 const createIcon=page.getByRole('button',{name:'新建环境',exact:true});await createIcon.waitFor();
 const createBox=await createIcon.boundingBox();assert.ok(Math.abs(createBox.x-nativeCreateBox.x)<1&&Math.abs(createBox.y-nativeCreateBox.y)<1);
 await page.locator('.lan-env-sidebar-env').first().waitFor();
 const runtime=(await(await api.post('http://127.0.0.1:'+cfg.port+'/v1/lan/manage/status',{data:{}})).json());
 assert.ok(runtime.ok&&runtime.states,'installed bridge must supply real environment status');
 await page.waitForFunction(()=>[...document.querySelectorAll('.lan-env-sidebar-env .lan-env-sidebar-dot')].every(dot=>dot.dataset.state));
 await page.locator('.mP3x-q_sidebarCol').screenshot({path:path.join(root,'.work/environment-runtime-status/deployed-sidebar.png'),animations:'disabled'});
 await page.getByRole('button',{name:'批量新建 / 导入',exact:true}).click();
 await page.getByRole('region',{name:'批量新建与导入'}).getByRole('button',{name:'预览并检查',exact:true}).waitFor();
 await page.screenshot({path:path.join(root,'.work/environment-import/deployed-batch.png'),animations:'disabled'});
 await page.getByRole('region',{name:'批量新建与导入'}).getByRole('button',{name:'任务记录',exact:true}).click();
 await page.getByRole('region',{name:'本机导入任务记录'}).waitFor();
 await page.screenshot({path:path.join(root,'.work/environment-import-optimization/deployed-history.png'),animations:'disabled'});
 assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
 fs.writeFileSync(path.join(out,'ui-validation.json'),JSON.stringify({ok:true,installed_version:4,environment_count:rows.length,scroll_container:metrics,secondary_navigation:true,page_errors:errors,mutation_requests:mutations},null,2));
 console.log('PASS: installed workspace v6, compact environment list, preserved environments, scrolling container and secondary navigation; no page errors or mutation requests.');
 } finally {await browser.close();await api.dispose();}
})().catch(e=>{console.error(e);process.exitCode=1});
