"""Verify same-origin iframe and open Shadow DOM observation and click coordinates."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from browser_backend import BrowserBackend  # noqa: E402

OUT = ROOT / ".work" / "deep-observe" / time.strftime("native-%Y%m%d-%H%M%S")
PROFILE = OUT / "Profile"
PROFILE.mkdir(parents=True)
PAGE = '''<!doctype html><iframe id="frame"></iframe>
<div id="host"></div><script>const frameDoc=frame.contentDocument;frameDoc.open();frameDoc.write(`<button id="inside" onclick="document.body.dataset.clicked='yes'">Inside frame</button>`);frameDoc.close();const root=host.attachShadow({mode:'open'});root.innerHTML=`<button id="shadow">Shadow button</button>`;root.querySelector('#shadow').onclick=()=>host.dataset.clicked='yes';</script>'''.encode()


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    debug_port = sock.getsockname()[1]
chrome = Path(r"E:\chromium\src\out\Release-pgo\chrome.exe")
env_id = "deep-observe-fixture"
url = f"http://127.0.0.1:{server.server_port}/?e={env_id}"
process = subprocess.Popen([
    str(chrome), f"--user-data-dir={PROFILE}", f"--remote-debugging-port={debug_port}",
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", url,
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
backend = BrowserBackend(port=debug_port)
backend.cursor_event = lambda *args, **kwargs: True
backend.live_state = lambda: {"active": env_id, "envs": []}
backend.env_state = lambda: {"active": env_id, "envs": []}
report = {"passed": False, "checks": [], "scope": "private headless browser and local fixture"}

try:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if next((x for x in backend.targets() if f"e={env_id}" in x.get("url", "")), None):
            break
        time.sleep(.05)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        ready = backend._cdp(env_id).evaluate("!!document.querySelector('iframe')?.contentDocument?.querySelector('#inside') && !!document.querySelector('#host')?.shadowRoot?.querySelector('#shadow')")
        if ready:
            break
        time.sleep(.1)
    observed = backend.observe(env_id)
    frame_button = next(item for item in observed['elements'] if item['selector'] == '#inside')
    shadow_button = next(item for item in observed['elements'] if item['selector'] == '#shadow')
    assert frame_button['frame_path'] == ['#frame']
    assert shadow_button['shadow_path'] == ['#host']
    report['checks'].append('observe_returns_frame_and_shadow_context_paths')
    backend.act(env_id, frame_button['id'])
    backend.act(env_id, shadow_button['id'])
    state = json.loads(backend._cdp(env_id).evaluate(
        "JSON.stringify({frame:frame.contentDocument.body.dataset.clicked,shadow:host.dataset.clicked})"))
    assert state == {'frame':'yes','shadow':'yes'}
    report['checks'].append('context_paths_map_native_clicks_to_nested_controls')
    report['passed'] = True
finally:
    path = OUT / 'report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report':str(path), **report}, ensure_ascii=False), flush=True)
    backend.close_sessions()
    process.terminate()
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        process.kill()
    server.shutdown()
    server.server_close()
