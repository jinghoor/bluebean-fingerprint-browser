// Anonymous public-site verification on the disposable native test browser.
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const ROOT = path.resolve(__dirname, '..');
const OUT = path.join(ROOT, '.work/media-playback');
(async () => {
  const privateOut=path.resolve(process.argv[3]);
  if (!privateOut.startsWith(OUT+path.sep+'native-')) throw Error('not a private media test directory');
  const state=JSON.parse(fs.readFileSync(path.join(privateOut,'session.json'),'utf8'));
  if (process.argv[2]!==`http://127.0.0.1:${state.port}`||path.resolve(state.profile)!==path.join(privateOut,'Profile')) throw Error('private browser mismatch');
  const browser = await chromium.connectOverCDP(process.argv[2]);
  const siteFilter = process.env.BLUEBEAN_MEDIA_SITE || '';
  const results = [];
  try {
    for (const [name, url] of [
      ['bilibili', 'https://www.bilibili.com/video/BV1bkxy6kEDi/'],
      ['douyin', 'https://www.douyin.com/jingxuan?modal_id=7674492386612972809'],
    ]) {
      if (siteFilter && name !== siteFilter) continue;
      const page = browser.contexts().flatMap(c=>c.pages()).at(-1);
      const network = [], errors = [];
      page.on('response', r => { const u = new URL(r.url()); if (/\.mp4|\.m4s|mime_type=video|video\/tos/.test(u.href)) network.push({host: u.host, status:r.status()}); });
      page.on('requestfailed', r => { const u = new URL(r.url()); if (/\.mp4|\.m4s|mime_type=video|video\/tos/.test(u.href)) errors.push({host:u.host, failure:r.failure()?.errorText}); });
      try {
        await page.goto(url, {waitUntil:'domcontentloaded', timeout:45000});
        await page.waitForFunction(() => document.querySelector('video') || /浏览器版本过低|不支持\s*HTML5/.test(document.body?.innerText || ''), {timeout:30000}).catch(()=>{});
        const closeLogin = page.locator('[data-e2e="douyin-login-close"], .douyin-login__close, .dy-account-close').first();
        if (await closeLogin.isVisible().catch(()=>false)) await closeLogin.click();
        await page.evaluate(() => { for(const v of document.querySelectorAll('video')) {v.muted=true; v.play().catch(()=>{});} });
        const sample = async () => { for (let attempt=0; attempt<8; attempt++) { try { return await page.evaluate(() => ({
          unsupported: /浏览器版本过低|不支持\s*HTML5/.test(document.body?.innerText || ''),
          loginRequired: /登录后免费畅享|登录后观看/.test(document.body?.innerText || ''),
          videos:[...document.querySelectorAll('video')].map(v=>({time:v.currentTime,duration:Number.isFinite(v.duration)?v.duration:null,
            readyState:v.readyState, paused:v.paused,width:v.videoWidth,height:v.videoHeight,frames:v.getVideoPlaybackQuality().totalVideoFrames,
            error:v.error?{code:v.error.code,message:v.error.message}:null}))
        })); } catch (error) { if(attempt===7) throw error; await page.waitForTimeout(300); } } };
        const deadline=Date.now()+30000;
        let before=await sample(), after=before;
        while(Date.now()<deadline){await page.waitForTimeout(1500);after=await sample();if(after.unsupported||after.videos.some(v=>v.time>1&&v.frames>10))break;}
        const started = after;
        await page.waitForTimeout(3000);
        after=await sample();
        const playing=after.videos.some(v=>{
          const previous=started.videos.find(s=>s.duration===v.duration && s.width===v.width && s.height===v.height);
          return previous && v.frames>previous.frames+10 && Math.abs(v.time-previous.time)>.5;
        });
        await page.screenshot({path:path.join(OUT,`site-${name}.png`)});
        const closeControls = await page.locator('[class*="close"], [data-e2e*="close"]').evaluateAll(es => es.filter(e=>e.getBoundingClientRect().width).slice(0,15).map(e=>({tag:e.tagName,class:e.className,data:e.getAttribute('data-e2e'),label:e.getAttribute('aria-label')})));
        results.push({name,before,started,after,playing,closeControls,network:network.slice(-10),errors:errors.slice(-10)});
      } catch(e) { results.push({name,error:e.message.split('\n')[0],network:network.slice(-10),errors:errors.slice(-10)}); }
    }
    fs.writeFileSync(path.join(OUT,siteFilter ? `site-results-${siteFilter}.json` : 'site-results.json'),JSON.stringify(results,null,2));
    console.log(JSON.stringify({sites:results}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1});
