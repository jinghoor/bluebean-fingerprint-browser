// Private headless browser + disposable vault. Never touch the user's browser.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium,request}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const ROOT=path.resolve(__dirname,'..'),OUT=path.join(ROOT,'.work/environment-startup-check/accounts-ui');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const temp=fs.mkdtempSync(path.join(os.tmpdir(),'bluebean-account-ui-')),info=path.join(temp,'server.json');
 const child=spawn('python',['-B',path.join(__dirname,'startup_accounts_server.py'),info],{cwd:ROOT,windowsHide:true,stdio:'ignore'});
 let browser,api;const errors=[],checks=[];
 try{
  for(let i=0;i<150&&!fs.existsSync(info);i++)await pause(40);
  const fixture=JSON.parse(fs.readFileSync(info,'utf8')),base='http://127.0.0.1:'+fixture.port;
  fs.mkdirSync(OUT,{recursive:true});api=await request.newContext();
  const control=async data=>(await(await api.post(base+'/__test/control',{data})).json()).counts;
  browser=await chromium.launch({executablePath:'E:/chromium/src/out/Release-pgo/chrome.exe',headless:true,args:['--no-first-run','--disable-extensions']});
  const page=await browser.newPage({viewport:{width:1280,height:960}});page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(e.message));
  // Keep acceptance tests off the user's actual Windows clipboard.
  await page.addInitScript(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.fixtureClipboard=value;}}}));
  const localRequests=[];page.on('request',r=>{if(r.url().includes('/account'))localRequests.push({url:new URL(r.url()).pathname,body:r.postDataJSON()});});
  await page.goto(fixture.url);
  const first=page.locator('[data-account="'+fixture.account_id+'"]');
  await first.locator('.otp-code').filter({hasText:/^\d{6}$/}).waitFor();
  await page.locator('#status.success').waitFor();
  assert.equal(await page.locator('.account').count(),3);
  assert.equal(await first.locator('.account-login-value').first().textContent(),'operator@example.test');
  assert.equal(await first.locator('.credential-value').textContent(),'••••••••');
  assert.equal(localRequests.filter(r=>r.body?.action==='credential').length,0);
  assert.equal(await page.getByText('打开环境主页',{exact:true}).count(),0);
  checks.push('login shown; password masked without fetching; TOTP starts automatically');
  const aligned=await page.locator('.account').evaluateAll(cards=>cards.map(card=>({
    login:card.querySelector('.account-login-value').getBoundingClientRect().x,
    password:card.querySelector('.credential-value').getBoundingClientRect().x,
    otp:card.querySelector('.otp-code')?.getBoundingClientRect().x,
    copy:card.querySelector('.account-login-copy')?.getBoundingClientRect().right,
    long:card.querySelector('.account-login-value').textContent.length>45,
    height:card.querySelector('.account-login-value').getBoundingClientRect().height,
    labels:[...card.querySelectorAll('.credential-label')].map(el=>({height:el.getBoundingClientRect().height,line:parseFloat(getComputedStyle(el).lineHeight)}))
  })));
  for(const card of aligned){assert.equal(card.login,card.password);if(card.otp)assert.equal(card.otp,card.password);assert.equal(card.copy,aligned[0].copy);assert.ok(card.labels.every(l=>l.height<=l.line+1));if(card.long)assert.ok(card.height>30);}
  await page.getByRole('button',{name:'复制 Facebook 登录账号',exact:true}).click();
  await page.waitForFunction(()=>window.fixtureClipboard==='operator@example.test');
  const longCard=page.locator('.account').filter({hasText:'very-long-account-name-for-layout-check@example.test'});
  await longCard.getByRole('button',{name:'复制 TikTok 登录账号',exact:true}).click();
  await page.waitForFunction(()=>window.fixtureClipboard==='very-long-account-name-for-layout-check@example.test');
  checks.push('full wrapping login copy and aligned fixed field/button columns');
  await page.waitForFunction(()=>[...document.querySelectorAll('.account-login-copy')].every(el=>el.textContent==='复制'));
  await page.screenshot({path:path.join(OUT,'portal-desktop.png'),animations:'disabled'});
  await page.locator('.account-panel').screenshot({path:path.join(OUT,'account-panel.png'),animations:'disabled'});
  await first.getByRole('button',{name:'显示或隐藏 Facebook 密码'}).click();
  await first.locator('.credential-value').filter({hasText:'fixture-local-password'}).waitFor();
  await first.getByRole('button',{name:'复制 Facebook 密码',exact:true}).click();
  assert.equal(await page.evaluate(()=>window.fixtureClipboard),'fixture-local-password');
  await first.getByRole('button',{name:'复制 Facebook 2FA 验证码',exact:true}).click();
  await page.waitForFunction(()=>/^\d{6}$/.test(window.fixtureClipboard));
  assert.match(await page.evaluate(()=>window.fixtureClipboard),/^\d{6}$/);
  await first.getByRole('button',{name:'显示或隐藏 Facebook 密码'}).click();
  assert.equal(await first.locator('.credential-value').textContent(),'••••••••');
  checks.push('password reveal and manual hide');
  await first.getByRole('button',{name:'显示或隐藏 Facebook 密码'}).click();
  await first.locator('.credential-value').filter({hasText:'fixture-local-password'}).waitFor();
  await first.locator('.credential-value').filter({hasText:'••••••••'}).waitFor({timeout:17000});
  checks.push('password automatically clears after real 15-second expiry');
  // Shorten only the browser timer: the next HTTP request still generates real server-side TOTP.
  const before=(await control({})).totp;
  await page.evaluate(()=>{for(const state of codes.values())state.deadline=performance.now()-1;});
  await first.locator('.otp-code').filter({hasText:/^\d{6}$/}).waitFor();
  for(let i=0;i<50&&(await control({})).totp<=before;i++)await pause(50);
  assert.ok((await control({})).totp>before);checks.push('expired TOTP automatically refetches');
  await control({credential_delay:.4});
  await first.getByRole('button',{name:'显示或隐藏 Facebook 密码'}).click();
  await page.evaluate(()=>window.dispatchEvent(new Event('blur')));
  await pause(550);assert.equal(await first.locator('.credential-value').textContent(),'••••••••');
  checks.push('late password response cannot reveal after loss of focus');
  await control({credential_delay:0,fail_totp:true});
  await page.evaluate(()=>{for(const state of codes.values())state.deadline=performance.now()-1;});
  await first.getByRole('button',{name:'重试',exact:true}).waitFor();
  await control({fail_totp:false});await first.getByRole('button',{name:'重试',exact:true}).click();
  await first.locator('.otp-code').filter({hasText:/^\d{6}$/}).waitFor();
  checks.push('TOTP failure has explicit retry and recovers');
  const empty=page.locator('[data-account="'+fixture.empty_account_id+'"]');
  assert.match(await empty.innerText(),/未保存/);assert.match(await empty.innerText(),/未设置/);
  for(const width of [800,480]){
   await page.setViewportSize({width,height:960});
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'no horizontal overflow at '+width);
   await page.screenshot({path:path.join(OUT,'portal-'+width+'.png'),animations:'disabled',fullPage:true});
  }
  await first.getByRole('button',{name:/打开 Facebook/}).click();
  await page.getByText('已切换到 Facebook 账号网站。',{exact:true}).waitFor();
  const counts=await control({});assert.equal(counts.open,1);assert.equal(counts.probes,8);checks.push('open uses account service; direct exit and six sites queried once');
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(OUT,'report.json'),JSON.stringify({passed:true,checks,counts,page_errors:errors},null,2));
  console.log('PASS: startup portal account UI, '+checks.length+' checks; '+OUT);
 }finally{if(browser)await browser.close();if(api)await api.dispose();child.kill();fs.rmSync(temp,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
