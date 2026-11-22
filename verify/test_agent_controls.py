"""Exercise core form actions added to the browser bridge."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from browser_backend import BrowserBackend  # noqa: E402

OUT = ROOT / ".work" / "agent-controls" / time.strftime("native-%Y%m%d-%H%M%S")
PROFILE = OUT / "Profile"
PROFILE.mkdir(parents=True)
PAGE = '''<!doctype html><input id="text" value="old"><button id="submit">提交</button>
<input id="check" type="checkbox"><input id="range" type="range" min="0" max="100" value="10">
<input id="file" type="file"><button id="drag" draggable="true">拖动</button><button id="drop">放置</button>
<div id="offscreen" style="margin-top:1800px">目标区域</div>
<pre id="state"></pre><script>drag.addEventListener('dragstart',e=>e.dataTransfer.setData('text/plain','ok'));drop.addEventListener('dragover',e=>e.preventDefault());drop.addEventListener('drop',e=>state.textContent+='drop:yes\\n');
for(const x of [text,check,range,file]) for(const t of ['input','change'])
  x.addEventListener(t,e=>state.textContent+=x.id+':'+t+':'+e.isTrusted+'\\n');
</script>'''.encode()


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


def wait_for_target(backend: BrowserBackend, env: str):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            target = next((item for item in backend.targets()
                           if f"e={env}" in item.get("url", "")), None)
            if target:
                return target
        except Exception:
            pass
        time.sleep(0.05)
    raise TimeoutError("controls fixture target did not start")


server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    debug_port = sock.getsockname()[1]

chrome = Path(r"E:\chromium\src\out\Release-pgo\chrome.exe")
env_id = "agent-controls-fixture"
url = f"http://127.0.0.1:{server.server_port}/?e={env_id}"
process = subprocess.Popen([
    str(chrome), f"--user-data-dir={PROFILE}",
    f"--remote-debugging-port={debug_port}", "--headless=new", "--disable-gpu",
    "--no-first-run", "--no-default-browser-check", url,
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
report = {"passed": False, "checks": [], "scope": "private headless browser and local fixture"}
backend = BrowserBackend(port=debug_port)
backend.cursor_event = lambda *args, **kwargs: True
backend.live_state = lambda: {"active": env_id, "envs": []}
backend.env_state = lambda: {"active": env_id, "envs": []}

try:
    wait_for_target(backend, env_id)
    observed = backend.observe(env_id)
    controls = {item["selector"]: item for item in observed["elements"]}
    assert "#text" in controls and "#check" in controls and "#range" in controls and "#file" in controls and "#drag" in controls and "#drop" in controls
    report["checks"].append("observe_finds_editable_check_range_file_controls")

    typed = backend.type_text(env_id, controls["#text"]["id"], "new", replace=True)
    assert typed["replaced"]
    keys = backend.send_keys(env_id, controls["#text"]["id"], ["CTRL+A", "x"])
    assert keys["ok"]
    checked = backend.set_checked(env_id, controls["#check"]["id"], True)
    assert checked["ok"] and checked["checked"] is True
    ranged = backend.set_range(env_id, controls["#range"]["id"], 75)
    assert ranged["ok"] and ranged["value"] == "75"
    dragged = backend.drag(env_id, controls["#drag"]["id"], controls["#drop"]["id"])
    assert dragged["ok"]
    report["checks"].append("replace_keyboard_check_and_range_actions_apply")

    with tempfile.NamedTemporaryFile(prefix="bluebean-upload-", suffix=".txt", delete=False) as temp:
        temp.write(b"agent fixture")
        upload_path = temp.name
    uploaded = backend.upload_file(env_id, controls["#file"]["id"], upload_path)
    assert uploaded["ok"] and uploaded["file_count"] == 1
    scrolled = backend.scroll_into_view(env_id, selector="#offscreen")
    assert scrolled["ok"] and scrolled["point"][1] > 0
    state = json.loads(backend._cdp(env_id).evaluate(
        "JSON.stringify({text:text.value,checked:check.checked,range:range.value,files:file.files.length,top:offscreen.getBoundingClientRect().top})"))
    assert state["text"] == "x" and state["checked"] and state["range"] == "75" and state["files"] == 1
    report["checks"].append("file_upload_selector_scroll_and_drag_complete")
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
