"""Exercise and verify the pointer paths used by infinite-canvas Agent actions.

The fixture implements a small graph editor instead of merely recording DOM
events. Each round checks the resulting graph state, which catches coordinate
drift and keyboard-focus regressions that an event-only test would miss.
"""
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

PAGE = b'''<!doctype html><style>
html,body{margin:0;padding:0;background:#fff}
#board{position:relative;width:700px;height:400px;overflow:hidden;background:#eef;border:1px solid #99a;outline:none}
#scene{position:absolute;left:0;top:0;transform-origin:0 0}
.node{position:absolute;width:100px;height:50px;box-sizing:border-box;border:2px solid #357;background:#acf;border-radius:6px;display:flex;align-items:center;justify-content:center;font:16px sans-serif;user-select:none}
#edges{position:absolute;left:0;top:0;width:700px;height:400px;overflow:visible;pointer-events:none}
#edges line{stroke:#246;stroke-width:5;pointer-events:stroke}
#state{position:absolute;left:-10000px;top:-10000px}
</style>
<div id="board" tabindex="0" aria-label="infinite canvas">
  <svg id="edges"><line id="edge-0"/></svg>
  <div id="scene"><div id="node-a" class="node" data-node="a" style="left:80px;top:80px">A</div><div id="node-b" class="node" data-node="b" style="left:420px;top:180px">B</div></div>
  <pre id="state"></pre>
</div>
<script>
const board=document.querySelector('#board'), scene=document.querySelector('#scene'), stateEl=document.querySelector('#state');
const graph={scale:1,panX:0,panY:0,edges:[],selectedNode:null,selectedEdge:null,editing:null,connecting:null,panDrag:null,zoomEvents:0,zoomAnchor:null,events:[]};
const nodes={a:{x:80,y:80,w:100,h:50,label:'A'},b:{x:420,y:180,w:100,h:50,label:'B'}};
function boardPoint(e){const r=board.getBoundingClientRect();return{x:(e.clientX-r.left-graph.panX)/graph.scale,y:(e.clientY-r.top-graph.panY)/graph.scale};}
function center(id){const n=nodes[id];return{x:n.x+n.w/2,y:n.y+n.h/2};}
function distToSegment(p,a,b){const dx=b.x-a.x,dy=b.y-a.y,t=Math.max(0,Math.min(1,((p.x-a.x)*dx+(p.y-a.y)*dy)/(dx*dx+dy*dy)));const x=a.x+t*dx,y=a.y+t*dy;return Math.hypot(p.x-x,p.y-y);}
function nodeAt(p){return Object.keys(nodes).find(id=>{const n=nodes[id];return p.x>=n.x&&p.x<=n.x+n.w&&p.y>=n.y&&p.y<=n.y+n.h})||null;}
function edgeAt(p){for(let i=0;i<graph.edges.length;i++){const e=graph.edges[i],d=distToSegment(p,center(e.from),center(e.to));if(d<18)return i;}return -1;}
function render(){
  scene.style.transform='translate('+graph.panX+'px,'+graph.panY+'px) scale('+graph.scale+')';
  for(const id of Object.keys(nodes)){const el=document.querySelector('#node-'+id);el.textContent=nodes[id].label;el.style.left=nodes[id].x+'px';el.style.top=nodes[id].y+'px';el.contentEditable=graph.editing===id?'true':'false';}
  const edge=graph.edges[0];const line=document.querySelector('#edge-0');
  if(edge){const a=center(edge.from),b=center(edge.to);line.setAttribute('x1',a.x);line.setAttribute('y1',a.y);line.setAttribute('x2',b.x);line.setAttribute('y2',b.y);line.style.display='block';line.style.transform='translate('+graph.panX+'px,'+graph.panY+'px) scale('+graph.scale+')';}
  else line.style.display='none';
  stateEl.textContent=JSON.stringify({scale:graph.scale,panX:graph.panX,panY:graph.panY,edges:graph.edges,selectedNode:graph.selectedNode,selectedEdge:graph.selectedEdge,editing:graph.editing,connecting:graph.connecting,zoomEvents:graph.zoomEvents,zoomAnchor:graph.zoomAnchor,events:graph.events});
}
function record(name){graph.events.push(name);if(graph.events.length>100)graph.events.shift();render();}
function resetGraph(){graph.scale=1;graph.panX=0;graph.panY=0;graph.edges=[];graph.selectedNode=null;graph.selectedEdge=null;graph.editing=null;graph.connecting=null;graph.panDrag=null;graph.zoomEvents=0;graph.zoomAnchor=null;graph.events=[];nodes.a.label='A';nodes.b.label='B';render();}
board.addEventListener('mousedown',e=>{
  record('mousedown');board.focus();const p=boardPoint(e);
  if(e.button===1){graph.panDrag={x:e.clientX,y:e.clientY,panX:graph.panX,panY:graph.panY};return;}
  const id=nodeAt(p);if(id){graph.selectedNode=id;graph.connecting=id;graph.selectedEdge=null;return;}
  graph.selectedEdge=edgeAt(p);graph.selectedNode=null;
});
board.addEventListener('mousemove',e=>{record('mousemove');if(graph.panDrag){graph.panX=graph.panDrag.panX+e.clientX-graph.panDrag.x;graph.panY=graph.panDrag.panY+e.clientY-graph.panDrag.y;render();}});
board.addEventListener('mouseup',e=>{record('mouseup');if(graph.panDrag){graph.panDrag=null;return;}const p=boardPoint(e),id=nodeAt(p);if(graph.connecting&&id&&id!==graph.connecting){graph.edges=[{from:graph.connecting,to:id}];graph.selectedEdge=null;}graph.connecting=null;render();});
board.addEventListener('dblclick',e=>{record('dblclick');const id=nodeAt(boardPoint(e));if(id){graph.editing=id;const el=document.querySelector('#node-'+id);el.contentEditable='true';el.focus();const sel=getSelection();sel.selectAllChildren(el);sel.collapse(el,false);render();}});
board.addEventListener('input',e=>{const id=graph.editing;if(id){nodes[id].label=e.target.textContent;render();}});
board.addEventListener('wheel',e=>{record('wheel');e.preventDefault();const r=board.getBoundingClientRect(),before={x:(e.clientX-r.left-graph.panX)/graph.scale,y:(e.clientY-r.top-graph.panY)/graph.scale};graph.scale=Math.max(.25,Math.min(4,graph.scale*(e.deltaY<0?1.1:.9)));graph.panX=e.clientX-r.left-before.x*graph.scale;graph.panY=e.clientY-r.top-before.y*graph.scale;graph.zoomAnchor={x:e.clientX-r.left,y:e.clientY-r.top};graph.zoomEvents++;render();},{passive:false});
board.addEventListener('keydown',e=>{record('keydown:'+e.key);if(e.key==='Escape'){graph.connecting=null;graph.editing=null;render();}if(e.key==='Delete'&&graph.selectedEdge>=0){graph.edges.splice(graph.selectedEdge,1);graph.selectedEdge=null;render();}});
resetGraph();
</script>'''


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
env_id = "canvas-fixture"
url = f"http://127.0.0.1:{server.server_port}/?e={env_id}"
process = subprocess.Popen(
    [str(chrome), f"--user-data-dir={PROFILE}", f"--remote-debugging-port={debug_port}",
     "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", url],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
backend = BrowserBackend(port=debug_port)
backend.cursor_event = lambda *a, **k: True
backend.live_state = lambda: {'active': env_id, 'envs': []}
backend.env_state = lambda: {'active': env_id, 'envs': []}
report = {'passed': False, 'checks': [], 'rounds': 0,
          'scope': 'private headless browser and local graph-editor fixture'}


def read_state():
    raw = backend._cdp(env_id).evaluate('stateEl.textContent')
    return json.loads(raw or '{}')


try:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if next((x for x in backend.targets() if f"e={env_id}" in x.get('url', '')), None):
            break
        time.sleep(.05)
    assert next((x for x in backend.targets() if f"e={env_id}" in x.get('url', '')), None), 'fixture target did not start'

    for round_no in range(1, 11):
        layout_shift = 14 if round_no % 2 else 0
        backend._cdp(env_id).evaluate(
            "document.querySelector('#board').style.marginLeft='%dpx'; document.querySelector('#board').style.marginTop='%dpx';" %
            (layout_shift, layout_shift // 2))
        backend._cdp(env_id).evaluate('resetGraph()')

        result = backend.canvas_action(env_id, 'select', selector='#board', point=[470, 205], duration_ms=0)
        assert result['ok'], result
        state = read_state()
        assert state['selectedNode'] == 'b', state

        result = backend.canvas_action(env_id, 'connect', selector='#board',
                                       from_point=[130, 105], to_point=[470, 205], duration_ms=24)
        assert result['ok'], result
        state = read_state()
        assert state['edges'] == [{'from': 'a', 'to': 'b'}], state

        result = backend.canvas_action(env_id, 'edit', selector='#board', point=[130, 105],
                                       text=f'Node {round_no}', replace=True, duration_ms=0)
        assert result['ok'], result
        state = read_state()
        assert state['editing'] == 'a' and state['events'].count('dblclick') >= 1, state
        assert read_state()['events'], 'edit produced no observable events'

        result = backend.canvas_action(env_id, 'cancel', selector='#board')
        assert result['ok'], result
        state = read_state()
        assert state['editing'] is None and state['connecting'] is None, state

        result = backend.canvas_action(env_id, 'disconnect', selector='#board', point=[300, 155], duration_ms=0)
        assert result['ok'], result
        state = read_state()
        assert state['edges'] == [] and state['selectedEdge'] is None, state

        result = backend.canvas_action(env_id, 'pan', selector='#board',
                                       from_point=[250, 200], to_point=[300, 230], duration_ms=24)
        assert result['ok'], result
        state = read_state()
        assert state['panX'] == 50 and state['panY'] == 30, state

        result = backend.canvas_action(env_id, 'zoom', selector='#board', point=[300, 200], delta_y=-120, duration_ms=0)
        assert result['ok'], result
        state = read_state()
        assert state['scale'] > 1 and state['zoomEvents'] == 1, state
        assert state['zoomAnchor'] == {'x': 300, 'y': 200}, state
        assert abs(state['panX'] - 25) < 0.01 and abs(state['panY'] - 13) < 0.01, state
        report['rounds'] = round_no

    invalid = backend.canvas_action(env_id, 'connect', selector='#board',
                                    from_point=[float('nan'), 1], to_point=[2, 3])
    assert invalid['ok'] is False and invalid['error_code'] == 'INVALID_CANVAS_POINT', invalid
    invalid = backend.canvas_action(env_id, 'zoom', selector='#board', point=[10, 10], delta_y=1,
                                    duration_ms='fast')
    assert invalid['ok'] is False and invalid['error_code'] == 'INVALID_CANVAS_DURATION', invalid
    invalid = backend.canvas_action(env_id, 'select', selector='#missing', point=[10, 10], duration_ms=0)
    assert invalid['ok'] is False and invalid['error_code'] == 'CANVAS_SELECTOR_NOT_FOUND', invalid

    report['checks'].extend([
        'node_selection_and_connection',
        'double_click_edit_and_keyboard_cancel',
        'edge_selection_and_delete_disconnect',
        'middle_button_pan',
        'wheel_zoom',
        'ten_repeated_state_verified_rounds',
        'layout_shift_relative_coordinates',
        'invalid_canvas_input_rejected',
    ])
    report['passed'] = True
finally:
    path = OUT / 'report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(path), **report}, ensure_ascii=False), flush=True)
    backend.close_sessions()
    process.terminate()
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        process.kill()
    server.shutdown()
    server.server_close()
