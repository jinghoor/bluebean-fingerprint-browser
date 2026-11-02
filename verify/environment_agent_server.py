"""Temporary-profile management fixture for plugin and installed DSH integration tests."""
import json
import sys
import tempfile
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from verify.test_env_management import fixture
from lan_bridge import make_handler
from session_browser_scope import SessionBrowserScopes

tmp=tempfile.TemporaryDirectory(prefix='bluebean-agent-host-test-',dir=Path(sys.argv[1]).parent if len(sys.argv)>1 else None)
bridge=fixture(tmp.name,probe=lambda p:{'ok':True,'exit_ip':'203.0.113.2','latency_ms':37,'city':'Fixture','timezone':'Etc/UTC'})
bridge.permissions={'kill_switch':False}
bridge.sessions=type('Sessions',(),{'turn_started':False})()
bridge.execution_lock=threading.RLock()
bridge.browser_scopes=SessionBrowserScopes(Path(tmp.name)/'scopes.json')
server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(bridge,'isolated-agent-tests'))
if len(sys.argv)>1: Path(sys.argv[1]).write_text(json.dumps({'port':server.server_port,'profile':tmp.name}),encoding='utf-8')
else: print(json.dumps({'port':server.server_port}),flush=True)
try: server.serve_forever()
finally: server.server_close();tmp.cleanup()
