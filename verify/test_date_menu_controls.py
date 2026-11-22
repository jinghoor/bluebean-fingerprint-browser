"""Exercise native date/time fields and a four-level cascading menu."""
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

OUT = ROOT / ".work" / "date-menu-controls" / time.strftime("native-%Y%m%d-%H%M%S")
PROFILE = OUT / "Profile"
PROFILE.mkdir(parents=True)
PAGE = '''<!doctype html><input id="date" type="date"><input id="time" type="time">
<input id="datetime" type="datetime-local"><button id="category" aria-haspopup="menu">类别</button>
<div id="custom" class="select-content-wrap" style="cursor:pointer"><div class="select-content-box">请选择</div><div class="wj-select ws-select" style="opacity:0"></div></div>
<div id="customMenu" style="display:none"></div><div id="menus"></div><pre id="result"></pre><pre id="customResult"></pre><script>
const levels=[['Electronics'],['Computers'],['Laptops'],['Gaming']];
function show(level){const box=document.createElement('div');box.id='menu-'+level;box.setAttribute('role','menu');
  const item=document.createElement('button');item.setAttribute('role','menuitem');item.textContent=levels[level][0];item.dataset.level=level;box.append(item);menus.append(box);}
category.onclick=()=>{menus.textContent='';show(0)};
menus.onclick=e=>{const item=e.target.closest('[role=menuitem]');if(!item)return;const level=Number(item.dataset.level);
  if(level+1<levels.length)show(level+1);else result.textContent='selected:'+item.textContent;};
custom.onclick=()=>{customMenu.innerHTML='<div class="option-item">自定义选项</div><div class="option-item">第二选项</div>';customMenu.style.display='block'};
customMenu.onclick=e=>{const item=e.target.closest('.option-item');if(item)customResult.textContent='custom:'+item.textContent;};
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
    raise TimeoutError("date/menu fixture target did not start")


server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    debug_port = sock.getsockname()[1]

chrome = Path(r"E:\chromium\src\out\Release-pgo\chrome.exe")
env_id = "date-menu-fixture"
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
    controls = {item["selector"]: item for item in observed["elements"]}
    assert controls["#date"]["input_type"] == "date"
    assert controls["#time"]["input_type"] == "time"
    assert controls["#datetime"]["input_type"] == "datetime-local"
    assert controls["#custom"]["control_kind"] == "custom-dropdown"
    assert controls["#custom"]["role"] == "combobox"
    report["checks"].append("observe_identifies_date_time_input_types")

    started = time.perf_counter()
    date = backend.set_date_time(env_id, controls["#date"]["id"], "2026-12-25")
    report["timings_ms"]["date"] = round((time.perf_counter() - started) * 1000, 2)
    started = time.perf_counter()
    time_value = backend.set_date_time(env_id, controls["#time"]["id"], "14:30")
    report["timings_ms"]["time"] = round((time.perf_counter() - started) * 1000, 2)
    started = time.perf_counter()
    datetime_value = backend.set_date_time(env_id, controls["#datetime"]["id"], "2026-12-25T14:30")
    report["timings_ms"]["datetime"] = round((time.perf_counter() - started) * 1000, 2)
    assert date["ok"] and time_value["ok"] and datetime_value["ok"]
    assert date["value"] == "2026-12-25" and time_value["value"] == "14:30"
    assert datetime_value["value"] == "2026-12-25T14:30"
    report["checks"].append("date_time_values_apply_in_html_formats")

    started = time.perf_counter()
    menu = backend.menu_select(env_id, controls["#category"]["id"],
                               ["Electronics", "Computers", "Laptops", "Gaming"])
    report["timings_ms"]["four_level_menu"] = round((time.perf_counter() - started) * 1000, 2)
    assert menu["ok"] and menu["path"] == ["Electronics", "Computers", "Laptops", "Gaming"]
    started = time.perf_counter()
    custom_menu = backend.menu_select(env_id, controls["#custom"]["id"], ["自定义选项"])
    report["timings_ms"]["custom_menu"] = round((time.perf_counter() - started) * 1000, 2)
    assert custom_menu["ok"] and custom_menu["path"] == ["自定义选项"]
    state = json.loads(backend._cdp(env_id).evaluate(
        "JSON.stringify({date:date.value,time:time.value,datetime:datetime.value,result:result.textContent,custom:customResult.textContent})"))
    assert state == {"date": "2026-12-25", "time": "14:30",
                     "datetime": "2026-12-25T14:30", "result": "selected:Gaming",
                     "custom": "custom:自定义选项"}
    report["checks"].append("four_level_menu_relocates_after_each_click")
    report["checks"].append("custom_div_dropdown_observed_and_selected")
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
