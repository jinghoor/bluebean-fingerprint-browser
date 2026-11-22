"""Exercise pointer paths used by infinite-canvas Agent actions."""
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

OUT = ROOT / ".work" / "canvas-controls" / time.strftime("native-%Y%m%d-%H%M%S")
PROFILE = OUT / "Profile"
PROFILE.mkdir(parents=True)
PAGE = b'''<!doctype html><style>#board{width:700px;height:400px;background:#eef}.node{position:absolute;width:80px;height:40px;background:#acf}.a{left:80px;top:80px}.b{left:420px;top:180px}</style>
<div id="board" tabindex="0"><div class="node a">A</div><div class="node b">B</div><pre id="events"></pre></div><script>
for(const t of ['mousedown','mousemove','mouseup','dblclick','wheel','keydown'])board.addEventListener(t,e=>events.textContent+=t+'\\n');
</script>'''


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(PAGE))); self.end_headers(); self.wfile.write(PAGE)


server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0)); debug_port = sock.getsockname()[1]
chrome = Path(r"E:\chromium\src\out\Release-pgo\chrome.exe")
env_id = "canvas-fixture"
url = f"http://127.0.0.1:{server.server_port}/?e={env_id}"
process = subprocess.Popen([str(chrome), f"--user-data-dir={PROFILE}", f"--remote-debugging-port={debug_port}", "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
backend = BrowserBackend(port=debug_port); backend.cursor_event=lambda *a,**k:True
backend.live_state=lambda:{'active':env_id,'envs':[]}; backend.env_state=lambda:{'active':env_id,'envs':[]}
report={'passed':False,'checks':[],'scope':'private headless browser and local fixture'}
try:
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if next((x for x in backend.targets() if f"e={env_id}" in x.get('url','')),None): break
        time.sleep(.05)
    for action,args in [
        ('connect',{'selector':'#board','from_point':[120,110],'to_point':[460,210]}),
        ('pan',{'selector':'#board','from_point':[250,200],'to_point':[300,230]}),
        ('zoom',{'selector':'#board','point':[300,200],'delta_y':-120}),
        ('edit',{'selector':'#board','point':[120,110],'text':'Node','replace':True}),
        ('disconnect',{'selector':'#board','point':[290,150]}),
        ('cancel',{}),
    ]:
        result=backend.canvas_action(env_id,action,**args)
        assert result['ok'], (action,result)
    events=backend._cdp(env_id).evaluate('events.textContent')
    for event in ('mousedown','mousemove','mouseup','wheel','dblclick','keydown'):
        assert event in events, event
    report['checks'].append('canvas_pointer_paths_and_keyboard_cancellation')
    report['passed']=True
finally:
    path=OUT/'report.json'; path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps({'report':str(path),**report},ensure_ascii=False),flush=True)
    backend.close_sessions(); process.terminate()
    try: process.wait(10)
    except subprocess.TimeoutExpired: process.kill()
    server.shutdown(); server.server_close()
