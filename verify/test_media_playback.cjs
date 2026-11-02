// Real decode/playback regression; local Chromium media fixtures, no user profile.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const ROOT = path.resolve(__dirname, '..');
const OUT = path.join(ROOT, '.work/media-playback');
const MEDIA = 'E:/chromium/src/media/test/data';
const option = key => process.argv.includes(key) ? process.argv[process.argv.indexOf(key) + 1] : '';
const baseline = process.argv.includes('--baseline');
const executablePath = option('--chrome') || 'E:/chromium/src/out/Release-pgo/chrome.exe';
const cdp = option('--cdp');
const types = {
  h264: 'video/mp4; codecs="avc1.64001e"',
  aac: 'audio/mp4; codecs="mp4a.40.2"',
  mp4: 'video/mp4; codecs="avc1.64001e, mp4a.40.2"',
  vp9: 'video/webm; codecs="vp9, opus"',
};
const fixtures = new Set(['bear.mp4', 'bear-1280x720.mp4', 'bear-640x360-av_frag.mp4',
  'bear-640x360-v_frag.mp4', 'bear-640x360-a_frag.mp4', 'bear-vp9-opus.webm']);
const server = http.createServer((req, res) => {
  const file = decodeURIComponent(new URL(req.url, 'http://localhost').pathname.slice(1));
  if (!file) {
    res.writeHead(200, { 'Content-Type': 'text/html' });
    return res.end('<!doctype html><html><body><h1>Local media decode verification</h1></body></html>');
  }
  if (!fixtures.has(file)) { res.writeHead(404); return res.end(); }
  const bytes = fs.readFileSync(path.join(MEDIA, file));
  const range = /bytes=(\d+)-(\d*)/.exec(req.headers.range || '');
  const start = range ? Number(range[1]) : 0;
  const end = range && range[2] ? Math.min(Number(range[2]), bytes.length - 1) : bytes.length - 1;
  const headers = { 'Content-Type': file.endsWith('.webm') ? 'video/webm' : 'video/mp4',
    'Accept-Ranges': 'bytes', 'Content-Length': end - start + 1 };
  if (range) headers['Content-Range'] = `bytes ${start}-${end}/${bytes.length}`;
  res.writeHead(range ? 206 : 200, headers);
  res.end(bytes.subarray(start, end + 1));
});

async function playback(page, scenario) {
  return page.evaluate(async scenario => {
    const video = document.createElement('video');
    video.muted = true;
    video.style.width = '320px';
    document.body.append(video);
    let objectUrl;
    const timeout = new Promise((_, reject) => setTimeout(() => reject(Error('playback timed out')), 12000));
    try {
      return await Promise.race([timeout, (async () => {
        const mediaError = new Promise((_, reject) => video.addEventListener('error', () => reject(Error(`${video.error?.code}: ${video.error?.message}`)), { once: true }));
        await Promise.race([mediaError, (async () => {
          if (scenario.mse) {
            const source = new MediaSource();
            objectUrl = URL.createObjectURL(source);
            video.src = objectUrl;
            await new Promise(resolve => source.addEventListener('sourceopen', resolve, { once: true }));
            // DASH establishes both tracks before either init segment sets
            // HAVE_METADATA; adding a new track afterwards may be rejected.
            const tracks = scenario.tracks.map(track => ({ ...track, buffer: source.addSourceBuffer(track.type) }));
            for (const track of tracks) {
              const buffer = track.buffer;
              const bytes = new Uint8Array(await (await fetch('/' + track.file)).arrayBuffer());
              // Split arbitrary boundaries to exercise progressive stream parsing.
              for (let offset = 0; offset < bytes.length; offset += 16384) {
                await new Promise((resolve, reject) => {
                  buffer.addEventListener('updateend', resolve, { once: true });
                  buffer.addEventListener('error', () => reject(Error('MSE append/decode failed')), { once: true });
                  buffer.appendBuffer(bytes.subarray(offset, offset + 16384));
                });
              }
            }
            source.endOfStream();
          } else video.src = '/' + scenario.file;
          await video.play();
          const deadline = performance.now() + 7000;
          while (video.currentTime < .55 && performance.now() < deadline) await new Promise(r => setTimeout(r, 50));
          const beforeSeek = video.currentTime;
          const frames = video.getVideoPlaybackQuality().totalVideoFrames;
          if (beforeSeek < .55) throw Error(`time did not advance (${beforeSeek})`);
          if (!video.videoWidth || frames < 2) throw Error(`no decoded frames (${frames})`);
          video.currentTime = Math.min(video.duration * .5, .9);
          await new Promise((resolve, reject) => {
            video.addEventListener('seeked', resolve, { once: true });
            setTimeout(() => reject(Error('seek timed out')), 3000);
          });
          return { ok: true, beforeSeek, afterSeek: video.currentTime, duration: video.duration,
            width: video.videoWidth, height: video.videoHeight, frames, readyState: video.readyState };
        })()]);
        return { ok: true, time: video.currentTime, duration: video.duration, frames: video.getVideoPlaybackQuality().totalVideoFrames,
          width: video.videoWidth, height: video.videoHeight };
      })()]);
    } catch (e) { return { ok: false, error: e.message, code: video.error?.code || null }; }
    finally { video.pause(); video.removeAttribute('src'); video.load(); video.remove(); if (objectUrl) URL.revokeObjectURL(objectUrl); }
  }, scenario);
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const report = { baseline, executablePath, modes: [], passed: false };
  try {
    for (const software of baseline || cdp ? [false] : [false, true]) {
      const browser = cdp ? await chromium.connectOverCDP(cdp) : await chromium.launch({ executablePath, headless: true,
        args: software ? ['--disable-accelerated-video-decode'] : [] });
      try {
        // Reuse the native environment tab. Target.createTarget would create an
        // unassigned tab and test the wrong storage partition.
        const page = cdp ? browser.contexts().flatMap(c => c.pages()).at(-1) : await browser.newPage();
        assert(page, 'native environment tab exists');
        await page.goto(`http://127.0.0.1:${server.address().port}`);
        const decoderNames = new Set();
        const session = await page.context().newCDPSession(page);
        session.on('Media.playerPropertiesChanged', e => e.properties.forEach(p => {
          if (/DecoderName/.test(p.name)) decoderNames.add(p.name + ':' + p.value);
        }));
        await session.send('Media.enable');
        const capabilities = await page.evaluate(types => Object.fromEntries(Object.entries(types).map(([key, type]) => [key,
          { element: document.createElement('video').canPlayType(type), mse: MediaSource.isTypeSupported(type) }])), types);
        const scenarios = [
          { name: 'H264 + AAC MP4', file: 'bear.mp4' },
          { name: '720p H264 + AAC MP4', file: 'bear-1280x720.mp4' },
          { name: 'MSE muxed H264 + AAC', mse: true, tracks: [{ file: 'bear-640x360-av_frag.mp4', type: types.mp4 }] },
          { name: 'MSE separate DASH video + audio', mse: true, tracks: [
            { file: 'bear-640x360-v_frag.mp4', type: types.h264 }, { file: 'bear-640x360-a_frag.mp4', type: types.aac }] },
          { name: 'VP9 + Opus regression', file: 'bear-vp9-opus.webm' },
        ];
        const results = [];
        for (const scenario of scenarios) results.push({ name: scenario.name, ...await playback(page, scenario) });
        report.modes.push({ software, capabilities, results, decoderNames: [...decoderNames] });
        if (baseline) {
          assert.equal(capabilities.h264.mse, false);
          assert.equal(capabilities.aac.mse, false);
          assert(results.slice(0, 4).every(r => !r.ok));
          assert(results[4].ok);
        } else {
          assert(Object.values(capabilities).every(c => c.element && c.mse));
          assert(results.every(r => r.ok), JSON.stringify(results));
        }
      } finally { await browser.close(); }
    }
    report.passed = true;
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    const name = option('--report') || (baseline ? 'decode-before.json' : 'decode-after.json');
    assert.equal(path.basename(name), name);
    fs.writeFileSync(path.join(OUT, name), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  }
})().catch(e => { console.error(e.message); process.exitCode = 1; });
