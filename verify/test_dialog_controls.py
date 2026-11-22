"""Exercise JavaScript dialog capture and explicit handling."""
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

OUT = ROOT / ".work" / "dialog-controls" / time.strftime("native-%Y%m%d-%H%M%S")
PROFILE = OUT / "Profile"
PROFILE.mkdir(parents=True)
PAGE = b'''<!doctype html><button id="alert" onclick="alert('hello')">Alert</button>
<button id="confirm" onclick="document.body.dataset.answer=confirm('continue?')">Confirm</button>
<button id="prompt" onclick="document.body.dataset.answer=prompt('name?','default')">Prompt</button>'''


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    debug_port = sock.getsockname()[1]
chrome = Path(r"E:\chromium\src\out\Release-pgo\chrome.exe")
env_id = "dialog-fixture"
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
    observed = backend.observe(env_id)
    button = next(item for item in observed['elements'] if item['selector'] == '#confirm')
    backend.act(env_id, button['id'])
    dialog = backend.pending_dialog(env_id)
    assert dialog and dialog['dialog_type'] == 'confirm' and dialog['message'] == 'continue?'
    report['checks'].append('confirm_dialog_is_captured_before_follow_up_input')
    handled = backend.handle_dialog(env_id, accept=True)
    assert handled['ok']
    value = backend._cdp(env_id).evaluate('document.body.dataset.answer')
    assert value == 'true'
    report['checks'].append('confirm_dialog_acceptance_releases_the_page')
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
