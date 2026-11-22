"""Exercise native single- and multi-select controls through the browser bridge."""
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

OUT = ROOT / ".work" / "select-controls" / time.strftime("native-%Y%m%d-%H%M%S")
PROFILE = OUT / "Profile"
PROFILE.mkdir(parents=True)
PAGE = b'''<!doctype html><select id="single">
<option value="a">Alpha</option><option value="b">Beta</option><option value="c">Gamma</option>
</select><select id="multi" multiple>
<option value="a">Alpha</option><option value="b">Beta</option><option value="c">Gamma</option>
</select><pre id="events"></pre><script>
for(const x of [single,multi]) for(const t of ['input','change'])
  x.addEventListener(t,e=>events.textContent+=t+':'+e.isTrusted+':' +
    [...x.selectedOptions].map(o=>o.value).join(',')+'\\n');
</script>'''


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
    raise TimeoutError("select fixture target did not start")


server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    debug_port = sock.getsockname()[1]

chrome = Path(r"E:\chromium\src\out\Release-pgo\chrome.exe")
env_id = "select-fixture"
url = f"http://127.0.0.1:{server.server_port}/?e={env_id}"
process = subprocess.Popen([
    str(chrome), f"--user-data-dir={PROFILE}",
    f"--remote-debugging-port={debug_port}", "--headless=new", "--disable-gpu",
    "--no-first-run", "--no-default-browser-check", url,
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
report = {"passed": False, "checks": [], "timings_ms": {},
          "scope": "private headless browser and local fixture"}
backend = BrowserBackend(port=debug_port)
backend.cursor_event = lambda *args, **kwargs: True
backend.live_state = lambda: {"active": env_id, "envs": []}
backend.env_state = lambda: {"active": env_id, "envs": []}

try:
    wait_for_target(backend, env_id)
    observed = backend.observe(env_id)
    controls = {item["selector"]: item for item in observed["elements"]
                if item.get("tag") == "select"}
    assert controls["#single"]["multiple"] is False
    assert controls["#multi"]["multiple"] is True
    assert [option["value"] for option in controls["#multi"]["options"]] == ["a", "b", "c"]
    report["checks"].append("observe_returns_option_values_labels_and_multiple_state")

    started = time.perf_counter()
    single = backend.select_options(env_id, controls["#single"]["id"], "Beta")
    report["timings_ms"]["single_select"] = round((time.perf_counter() - started) * 1000, 2)
    assert single["ok"] and single["trusted"]
    assert [option["value"] for option in single["selected"]] == ["b"]
    started = time.perf_counter()
    multi = backend.select_options(env_id, controls["#multi"]["id"], ["Alpha", "Gamma"])
    report["timings_ms"]["multi_select"] = round((time.perf_counter() - started) * 1000, 2)
    assert multi["ok"] and multi["trusted"]
    assert [option["value"] for option in multi["selected"]] == ["a", "c"]
    report["checks"].append("single_and_multi_select_apply_by_value_or_label")

    cdp = backend._cdp(env_id)
    state = json.loads(cdp.evaluate(
        "JSON.stringify({single:single.value,multi:Array.from(multi.selectedOptions).map(o=>o.value),events:events.textContent})"))
    assert state["single"] == "b" and state["multi"] == ["a", "c"]
    assert ":true:" in state["events"]
    report["checks"].append("selection_events_are_trusted_and_final_state_matches")
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
