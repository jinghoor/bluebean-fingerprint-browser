// The actual DSH host, a separate headless browser and disposable data only.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium,request}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const ROOT=path.resolve(__dirname,'..'),OUT=path.join(ROOT,'.work/env-workspace-navigation');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn,message){const end=Date.now()+10000;while(Date.now()<end){if(await fn())return;await pause(40);}throw Error(message);}
(async()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'bluebean-navigation-')),info=path.join(dir,'fixture.json');
 const server=spawn('python',[path.join(__dirname,'env_management_server.py'),info],{cwd:ROOT,windowsHide:true,stdio:'ignore'});
 let browser,api,page,phase='start';const errors=[];let failAccounts=false;
 try{
  await until(()=>fs.existsSync(info),'fixture startup');
  const fixture=JSON.parse(fs.readFileSync(info,'utf8')),base='http://127.0.0.1:'+fixture.port;
  api=await request.newContext({extraHTTPHeaders:{Authorization:'Bearer isolated-tests'}});
  const manage=async(action,data={})=>(await api.post(base+'/v1/lan/manage/'+action,{data})).json();
  for(let i=0;i<32;i++)assert.equal((await manage('save',{env_id:'scroll-'+String(i).padStart(2,'0'),display_name:'运营环境 '+String(i).padStart(2,'0'),create_only:true,proxy:i===0?'http://127.0.0.1:8899':'direct'})).ok,true);
  assert.equal((await manage('accounts',{env_id:'scroll-00',action:'save',accounts:Array.from({length:12},(_,i)=>({client_id:'nav-'+i,platform_id:'facebook',login:'operator-'+i+'@example.test',label:'日常运营账号 '+i}))})).ok,true);
  browser=await chromium.launch({executablePath:'E:/chromium/src/out/Release-pgo/chrome.exe',headless:true,args:['--no-first-run','--disable-extensions']});
  page=await browser.newPage({viewport:{width:1280,height:720}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/**',r=>/\/(describe|list|inventory|syncInspectManifest|modelCatalog|catalog|listProviders)$/.test(new URL(r.request().url()).pathname)?r.continue():r.fulfill({json:{ok:false,error:'isolated navigation test'}}));
  await page.route('**/plugins/**',async r=>{if(r.request().url().endsWith('/events'))return r.abort();const res=await r.fetch(),body=await res.text();return r.fulfill({response:res,body:body.includes("id: 'lan-seats'")?fs.readFileSync(path.join(ROOT,'lan_seats/lib/client.js'),'utf8'):body});});
  await page.route('http://127.0.0.1:8799/**',async r=>{const pathname=new URL(r.request().url()).pathname,body=r.request().postDataJSON()||{};if(pathname.startsWith('/v1/lan/manage/')){if(failAccounts&&pathname.endsWith('/accounts')&&body.action==='save'){failAccounts=false;return r.fulfill({json:{ok:false,error:'临时保存失败，输入已保留'}});}return r.fulfill({response:await api.post(base+pathname,{data:body})});}return r.fulfill({json:{ok:true,envs:[],scope_env_ids:[],active:''}});});
  const open=async()=>{await page.goto(JSON.parse(fs.readFileSync(path.join(ROOT,'.dsh-web.json'),'utf8')).url);await page.getByText('我的环境',{exact:true}).click();await page.locator('.lm-row').first().waitFor();};
  await open();const content=page.getByRole('main',{name:'我的环境内容'}),root=page.locator('.lan-manager');
  phase='scroll';
  await page.locator('.lm-enter').evaluate(el=>el.getAnimations().forEach(a=>a.finish()));
  const firstRow=page.locator('.lm-row').first(),rowY=await firstRow.evaluate(e=>e.getBoundingClientRect().y);
  await page.locator('.lm-toast-layer').evaluate(layer=>{const toast=document.createElement('div');toast.className='lm-notice lm-toast';toast.textContent='布局稳定性检查';layer.append(toast);});
  assert.equal(await firstRow.evaluate(e=>e.getBoundingClientRect().y),rowY,'notification shifted the environment list');
  await page.locator('.lm-toast-layer .lm-toast').last().evaluate(e=>e.remove());
  const metrics=await content.evaluate(e=>({client:e.clientHeight,scroll:e.scrollHeight,overflow:getComputedStyle(e).overflowY}));
  assert.ok(metrics.scroll>metrics.client+500);assert.equal(metrics.overflow,'auto');
  assert.ok(await page.locator('.lm-row').first().evaluate(e=>e.getBoundingClientRect().height<=60),'environment list rows should stay compact');
  assert.equal(await page.locator('.lm-row').filter({hasText:'operator-0@example.test'}).count(),0,'account logins belong in the account detail view, not the environment list');
  assert.notEqual(await content.evaluate(e=>getComputedStyle(e,'::-webkit-scrollbar').display),'none');
  assert.ok((await root.boundingBox()).height<=720);
  const rect=await content.boundingBox();await page.mouse.move(rect.x+rect.width/2,rect.y+100);await page.mouse.wheel(0,650);
  await until(async()=>await content.evaluate(e=>e.scrollTop)>150,'mouse wheel did not scroll content');
  const fixedHeader=await page.locator('.lm-shell-header').boundingBox();assert.ok(fixedHeader.y>=0&&fixedHeader.y<2);
  await content.focus();await page.keyboard.press('End');
  await until(async()=>await content.evaluate(e=>e.scrollTop+e.clientHeight>=e.scrollHeight-2),'End cannot reach page footer');
  assert.equal(await page.getByRole('button',{name:'下一页',exact:true}).isVisible(),true);
  await page.screenshot({path:path.join(OUT,'after-list-bottom.png'),animations:'disabled'});
  const selectedRow=page.locator('.lm-row').last(),selectedId=await selectedRow.getAttribute('data-env');
  await selectedRow.locator('input[type=checkbox]').check();
  await selectedRow.getByRole('button',{name:'管理',exact:true}).click();
  await page.locator('[data-environment-detail]').waitFor();
  await page.getByRole('button',{name:'返回上一级',exact:true}).click();
  await until(async()=>await content.evaluate(e=>e.scrollTop)>500,'return lost the list scroll position');
  assert.equal(await page.locator('[data-env="'+selectedId+'"]').locator('input[type=checkbox]').isChecked(),true);
  await content.focus();await page.keyboard.press('Home');await until(async()=>await content.evaluate(e=>e.scrollTop)===0,'Home cannot return to top');
  await page.screenshot({path:path.join(OUT,'after-list.png'),animations:'disabled'});

  phase='hierarchy';
  await page.locator('[data-env="scroll-00"]').getByRole('button',{name:'管理',exact:true}).click();
  await page.getByRole('button',{name:'环境设置',exact:true}).click();
  assert.equal(await page.locator('[name="proxy.host"]').count(),0);
  await page.locator('[name="notes"]').fill('分类切换时保留输入');
  await page.getByRole('button',{name:'网络代理',exact:true}).click();assert.equal(await page.locator('[name="notes"]').count(),0);
  await page.getByRole('button',{name:'基本信息',exact:true}).click();assert.equal(await page.locator('[name="notes"]').inputValue(),'分类切换时保留输入');
  await page.screenshot({path:path.join(OUT,'after-settings.png'),animations:'disabled'});
  await page.getByRole('button',{name:'返回上一级',exact:true}).click();await page.locator('[data-environment-detail="scroll-00"]').waitFor();
  await page.getByRole('button',{name:'返回上一级',exact:true}).click();
  await page.getByRole('tab',{name:'账号',exact:true}).click();await page.getByRole('combobox',{name:'选择环境'}).selectOption('scroll-00');
  await page.locator('[data-account]').first().waitFor();assert.equal(await page.locator('[name="account.login"]').count(),0);
  const accountRow=page.locator('[data-account]').first();await accountRow.getByRole('button',{name:'账号详情',exact:true}).click();
  const detail=page.getByRole('dialog',{name:'账号详情',exact:true});await detail.waitFor();
  await page.keyboard.press('Escape');await detail.waitFor({state:'hidden'});
  assert.equal(await page.evaluate(()=>document.activeElement.textContent),'账号详情');

  phase='account-modal';
  await page.getByRole('button',{name:'＋ 添加账号',exact:true}).click();
  const editor=page.getByRole('dialog',{name:'编辑平台账号',exact:true});await editor.waitFor();
  for(let i=0;i<4;i++){
   if(i)await editor.getByRole('button',{name:'＋ 继续添加账号',exact:true}).click();
   await editor.locator('[name="account.platform_id"]').last().selectOption('facebook');
   await editor.locator('[name="account.login"]').last().fill('new-'+i+'@example.test');
  }
  const modalBody=editor.locator('.lm-modal-body'),bgScroll=await content.evaluate(e=>e.scrollTop);
  const modalBox=await modalBody.boundingBox();await page.mouse.move(modalBox.x+modalBox.width/2,modalBox.y+50);await page.mouse.wheel(0,-10000);
  await until(async()=>await modalBody.evaluate(e=>e.scrollTop)===0,'modal cannot scroll to top');
  await page.mouse.wheel(0,650);await until(async()=>await modalBody.evaluate(e=>e.scrollTop)>100,'modal wheel did not scroll');
  assert.equal(await content.evaluate(e=>e.scrollTop),bgScroll);
  const saveBox=await editor.getByRole('button',{name:'保存全部账号',exact:true}).boundingBox();assert.ok(saveBox.y+saveBox.height<=720);
  await editor.locator('[name="account.otp_secret"]').last().focus();
  const lastField=await editor.locator('[name="account.otp_secret"]').last().boundingBox(),bodyBounds=await modalBody.boundingBox();
  assert.ok(lastField.y>=bodyBounds.y&&lastField.y+lastField.height<=bodyBounds.y+bodyBounds.height,'focused last field hidden behind the footer');
  await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.querySelector('dialog[open]').contains(document.activeElement)),true);
  await page.screenshot({path:path.join(OUT,'after-account-modal.png'),animations:'disabled'});
  failAccounts=true;await editor.getByRole('button',{name:'保存全部账号',exact:true}).click();
  await editor.getByText('临时保存失败，输入已保留',{exact:true}).waitFor();
  assert.equal(await editor.locator('[name="account.login"]').last().inputValue(),'new-3@example.test');
  await editor.getByRole('button',{name:'保存全部账号',exact:true}).click();await editor.waitFor({state:'hidden'});
  assert.equal((await manage('accounts',{env_id:'scroll-00'})).accounts.length,16);

  phase='draft-reload';
  await page.getByRole('button',{name:'＋ 添加账号',exact:true}).click();await editor.locator('[name="account.platform_id"]').selectOption('amazon');await editor.locator('[name="account.login"]').fill('resume@example.test');
  await editor.getByRole('button',{name:'稍后继续',exact:true}).click();await editor.waitFor({state:'hidden'});await open();
  assert.equal(await page.getByRole('button',{name:'继续编辑草稿',exact:true}).count(),0,'draft recovery must not appear on the workspace home');
  await page.getByRole('button',{name:'＋ 新建环境',exact:true}).click();
  const draftPrompt=page.getByRole('dialog',{name:'发现未完成草稿',exact:true});await draftPrompt.waitFor();
  await draftPrompt.getByRole('button',{name:'继续编辑草稿',exact:true}).click();await editor.waitFor();assert.equal(await editor.locator('[name="account.login"]').inputValue(),'resume@example.test');
  await editor.getByRole('button',{name:'保存全部账号',exact:true}).click();await editor.waitFor({state:'hidden'});

  phase='maintenance';
  await page.getByRole('tab',{name:'备份与恢复',exact:true}).click();
  assert.equal(await page.locator('[name="transfer-password"]').count(),0);assert.equal(await page.locator('input[type=file]').count(),0);
  await page.getByRole('button',{name:'账号加密迁移',exact:true}).click();await page.locator('[name="transfer-password"]').waitFor();
  assert.equal(await page.getByRole('button',{name:'备份并导出配置',exact:true}).count(),0);
  await page.getByRole('button',{name:'返回上一级',exact:true}).click();await page.getByRole('button',{name:'环境配置备份',exact:true}).click();
  assert.equal(await page.locator('[name="transfer-password"]').count(),0);
  await page.screenshot({path:path.join(OUT,'after-backups.png'),animations:'disabled'});
  await page.getByRole('button',{name:'返回上一级',exact:true}).click();

  phase='narrow-short';
  await page.setViewportSize({width:800,height:560});await root.evaluate(e=>e.style.width='480px');
  await page.screenshot({path:path.join(OUT,'after-maintenance-480.png'),animations:'disabled'});
  assert.equal(await root.evaluate(e=>e.scrollWidth>e.clientWidth+1),false);
  await page.getByRole('tab',{name:'账号',exact:true}).click();await page.getByRole('button',{name:'＋ 添加账号',exact:true}).click();
  const modalRect=await editor.boundingBox();assert.ok(modalRect.y>=0&&modalRect.y+modalRect.height<=560);
  await editor.locator('[name="account.platform_id"]').selectOption('facebook');
  assert.equal(await modalBody.evaluate(e=>e.scrollWidth>e.clientWidth+1),false);
  await page.screenshot({path:path.join(OUT,'after-modal-short.png'),animations:'disabled'});
  await page.keyboard.press('Escape');await editor.waitFor({state:'hidden'});
  await page.getByRole('button',{name:'＋ 新建环境',exact:true}).click();await draftPrompt.waitFor();
  await draftPrompt.getByRole('button',{name:'继续编辑草稿',exact:true}).click();await editor.waitFor();
  await editor.getByRole('button',{name:'移除此条',exact:true}).click();await editor.waitFor({state:'hidden'});
  assert.equal((await manage('draft')).draft,null);
  // A truly narrow WebContents still has a usable single-column modal.
  await page.setViewportSize({width:480,height:600});await root.evaluate(e=>e.style.width='');
  await page.getByRole('button',{name:'＋ 添加账号',exact:true}).click();await editor.waitFor();
  const narrow=await editor.boundingBox();assert.ok(narrow.x>=0&&narrow.x+narrow.width<=480);
  assert.equal(await modalBody.evaluate(e=>e.scrollWidth>e.clientWidth+1),false);
  await page.screenshot({path:path.join(OUT,'after-modal-480.png'),animations:'disabled'});
  await editor.getByRole('button',{name:'移除此条',exact:true}).click();await editor.waitFor({state:'hidden'});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(OUT,'validation.json'),JSON.stringify({ok:true,viewport:[1280,720],short_viewport:[800,560],mouse_wheel:true,keyboard_scroll:true,return_position:true,dialog_scroll:true,dialog_focus_return:true,failed_save_preserves_input:true,draft_reload:true,secondary_pages:true,errors},null,2));
  console.log('PASS: wheel/keyboard scrolling, position return, page hierarchy, modal isolation, failed-save retry, encrypted draft reload and short/narrow layout.');
 }catch(e){if(page)await page.screenshot({path:path.join(OUT,'failure.png')}).catch(()=>{});console.error('FAILED PHASE:',phase);throw e;}
 finally{if(browser)await browser.close();if(api)await api.dispose();server.kill();fs.rmSync(dir,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1});
