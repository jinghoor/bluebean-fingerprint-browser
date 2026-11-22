"""Exercise virtualized rows, scroll-refresh observation, and long-form filling."""
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

OUT = ROOT / ".work" / "virtual-form-controls" / time.strftime("native-%Y%m%d-%H%M%S")
PROFILE = OUT / "Profile"
PROFILE.mkdir(parents=True)

# Only about twelve rows exist in the DOM at a time. Scrolling replaces them,
# which is the behavior that previously left Agent with stale or missing IDs.
PAGE = r'''<!doctype html>
<style>
  body { margin: 0; font: 16px sans-serif; }
  #viewport { width: 420px; height: 280px; overflow: auto; border: 1px solid #888; position: relative; }
  #spacer { height: 5040px; pointer-events: none; }
  #rows { position: absolute; inset: 0 0 auto 0; }
  .row { position: absolute; left: 0; right: 0; top: 0; height: 42px; display: flex; align-items: center; gap: 12px; padding: 0 12px; box-sizing: border-box; }
  .row input { width: 220px; height: 25px; }
</style>
<div id="viewport"><div id="spacer"></div><div id="rows"></div></div>
<pre id="result"></pre>
<script>
const total = 120, rowHeight = 42, values = Object.create(null);
const viewport = document.querySelector('#viewport'), rows = document.querySelector('#rows');
function render() {
  const first = Math.max(0, Math.floor(viewport.scrollTop / rowHeight) - 2);
  const last = Math.min(total - 1, first + 13);
  rows.replaceChildren();
  for (let i = first; i <= last; i++) {
    const row = document.createElement('div');
    row.className = 'row'; row.dataset.index = i; row.style.transform = `translateY(${i * rowHeight}px)`;
    const label = document.createElement('label'); label.textContent = `第 ${i + 1} 行`;
    const input = document.createElement('input'); input.id = `row-${i}`; input.name = `row-${i}`;
    input.value = values[i] || ''; input.placeholder = `填写第 ${i + 1} 行`;
    input.addEventListener('input', () => { values[i] = input.value; });
    row.append(label, input); rows.append(row);
  }
  result.textContent = JSON.stringify({first, last, rendered: rows.children.length});
}
let renderTimer;
viewport.addEventListener('scroll', () => {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(render, 55);
}, {passive: true});
render();
window.virtualState = () => ({values, first: Math.floor(viewport.scrollTop / rowHeight), rendered: rows.children.length});
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
    raise TimeoutError("virtual form fixture target did not start")


server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    debug_port = sock.getsockname()[1]

chrome = Path(r"E:\chromium\src\out\Release-pgo\chrome.exe")
env_id = "virtual-form-fixture"
url = f"http://127.0.0.1:{server.server_port}/?e={env_id}"
process = subprocess.Popen([
    str(chrome), f"--user-data-dir={PROFILE}",
    f"--remote-debugging-port={debug_port}", "--headless=new", "--disable-gpu",
    "--no-first-run", "--no-default-browser-check", url,
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
report = {"passed": False, "checks": [], "timings_ms": {},
          "scope": "private headless browser and local virtualized-form fixture"}
backend = BrowserBackend(port=debug_port)
backend.cursor_event = lambda *args, **kwargs: True
backend.live_state = lambda: {"active": env_id, "envs": []}
backend.env_state = lambda: {"active": env_id, "envs": []}

try:
    wait_for_target(backend, env_id)
    initial = backend.observe(env_id)
    initial_rows = {item["selector"]: item for item in initial["elements"]
                    if item.get("selector", "").startswith("#row-")}
    assert "#row-0" in initial_rows and len(initial_rows) >= 8
    report["checks"].append("initial_virtual_window_is_observed")

    # Fill every virtual row once. This is deliberately longer than the
    # rendered window, so the test cannot pass by exercising only static DOM.
    targets = list(range(120)) + [119, 80, 40, 0]
    filled = {}
    current = initial
    previous_ids = set(initial_rows)
    scroll_timings = []
    fill_timings = []
    for round_no, row_index in enumerate(targets, 1):
        selector = f"#row-{row_index}"
        started = time.perf_counter()
        # Move in small wheel increments until this virtual row is mounted.
        # The returned observation is the authoritative post-scroll window.
        attempts = 0
        while selector not in {item.get("selector") for item in current["elements"]}:
            # Use the current window's highest/lowest row as a cheap direction
            # hint. The actual target is confirmed only by the fresh observe.
            rows = [int(item["selector"].split("-")[-1]) for item in current["elements"]
                    if item.get("selector", "").startswith("#row-")]
            if rows and row_index < min(rows):
                direction = -1
            else:
                direction = 1
            scrolled = backend.scroll(env_id, delta_y=direction * 420, x=190, y=130)
            scroll_timings.append(scrolled.get("render_settled", {}).get("elapsed_ms", 0))
            assert scrolled.get("observe_after_scroll") is True
            assert isinstance(scrolled.get("observation"), dict)
            current = scrolled["observation"]
            attempts += 1
            if attempts > 40:
                raise AssertionError(f"virtual row {row_index} never became observable")
        visible_rows = {item["selector"]: item for item in current["elements"]
                        if item.get("selector", "").startswith("#row-")}
        assert selector in visible_rows
        new_ids = set(visible_rows) - previous_ids
        assert new_ids or attempts == 0, f"row window did not advance before {selector}"
        previous_ids = set(visible_rows)
        if attempts or round_no in (1, len(targets)):
            report.setdefault("windows", []).append({
                "round": round_no, "target": selector, "rows": sorted(visible_rows),
                "new_rows": sorted(new_ids), "attempts": attempts,
            })
        scroll_timings.append(round((time.perf_counter() - started) * 1000, 2))

        started = time.perf_counter()
        typed = backend.type_text(env_id, visible_rows[selector]["id"],
                                  f"value-{row_index}", replace=True)
        fill_timings.append(round((time.perf_counter() - started) * 1000, 2))
        assert typed.get("typed") == f"value-{row_index}", typed
        filled[str(row_index)] = f"value-{row_index}"

    state = json.loads(backend._cdp(env_id).evaluate(
        "JSON.stringify(window.virtualState())"))
    assert all(state["values"].get(key) == value for key, value in filled.items())
    assert len(filled) == len(set(targets))
    report["timings_ms"] = {
        "scroll_render_samples": scroll_timings,
        "scroll_total_avg": round(sum(scroll_timings) / max(1, len(scroll_timings)), 2),
        "fill_samples": fill_timings,
        "fill_avg": round(sum(fill_timings) / max(1, len(fill_timings)), 2),
    }
    report["checks"].extend([
        "scroll_returns_fresh_observation_for_new_virtual_rows",
        "repeated_up_down_windows_never_use_stale_ids",
        "offscreen_virtual_rows_are_auto_scrolled_and_filled",
    ])
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
