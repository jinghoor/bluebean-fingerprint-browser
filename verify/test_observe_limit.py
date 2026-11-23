"""Ensure late form controls remain discoverable beyond the old 80-item window."""
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

OUT = ROOT / ".work" / "observe-limit" / time.strftime("native-%Y%m%d-%H%M%S")
PROFILE = OUT / "Profile"
PROFILE.mkdir(parents=True)

fields = "".join(
    f'<label>前置字段 {index}<input id="field-{index}" name="field-{index}"></label>'
    for index in range(130)
)
PAGE = (f'''<!doctype html><style>
body {{ margin: 0; font: 16px sans-serif; }}
label {{ display:block; height:34px; }}
</style><form>{fields}
<label>23. 性别 <input id="gender-late" type="radio" name="gender" value="male"></label>
<button id="location-late" type="button">点击获取位置</button>
<output id="result"></output></form><script>
document.querySelector('#location-late').onclick = () => document.querySelector('#result').textContent = 'location-clicked';
</script>''').encode()


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
env_id = "observe-limit-fixture"
url = f"http://127.0.0.1:{server.server_port}/?e={env_id}"
process = subprocess.Popen([
    str(chrome), f"--user-data-dir={PROFILE}",
    f"--remote-debugging-port={debug_port}", "--headless=new", "--disable-gpu",
    "--no-first-run", "--no-default-browser-check", url,
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
backend = BrowserBackend(port=debug_port)
backend.cursor_event = lambda *args, **kwargs: True
backend.live_state = lambda: {"active": env_id, "envs": []}
backend.env_state = lambda: {"active": env_id, "envs": []}
report = {"passed": False, "checks": [], "scope": "private headless browser and local fixture"}

try:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if next((item for item in backend.targets() if f"e={env_id}" in item.get("url", "")), None):
            break
        time.sleep(0.05)
    observed = backend.observe(env_id)
    by_selector = {item["selector"]: item for item in observed["elements"]}
    assert len(observed["elements"]) >= 132
    assert "#gender-late" in by_selector and "#location-late" in by_selector
    report["checks"].append("default_observe_reaches_controls_after_130_prior_fields")

    gender = backend.act(env_id, by_selector["#gender-late"]["id"])
    location = backend.act(env_id, by_selector["#location-late"]["id"])
    assert gender.get("trusted") and location.get("trusted")
    state = json.loads(backend._cdp(env_id).evaluate(
        "JSON.stringify({gender:document.querySelector('#gender-late').checked,result:document.querySelector('#result').textContent})"))
    assert state == {"gender": True, "result": "location-clicked"}, state
    report["checks"].append("late_gender_and_location_controls_auto_scroll_and_click")
    report["elements_returned"] = len(observed["elements"])
    report["passed"] = True
finally:
    report_path = OUT / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), **report}, ensure_ascii=False), flush=True)
    backend.close_sessions()
    process.terminate()
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        process.kill()
    server.shutdown()
    server.server_close()
