"""Independent headless Chromium with a separate browser context for each test account.

Only environment-to-target assignment is supplied by the fixture. The production
management gateway, account filling, CDP backend and scope checks run unchanged.
This does not claim to test the product kernel's native partition implementation.
"""
import json
import subprocess
import time
import urllib.request
from pathlib import Path
from browser_backend import BrowserBackend, BrowserCdp
from env_management import atomic_json
from environment_status import AgentActivity
from _nowindow import no_window, hidden_startup_info

LOGIN_HTML=b'''<!doctype html><meta charset="utf-8"><title>Isolated account login</title>
<h1>Account login verification</h1><form id="login"><label>Email<input name="email" autocomplete="username"></label>
<label>Password<input type="password" name="password" autocomplete="current-password"></label>
<button>Log in</button></form><p id="result"></p><script>window.submitCount=0;
login.addEventListener('submit',e=>{e.preventDefault();window.submitCount++;result.textContent='Submitted';});</script>'''

class BrowserSandbox:
    def __init__(self,bridge,home,server_port):
        self.bridge=bridge;self.home=Path(home);self.server_port=server_port;self.targets={};self.contexts={}
        self.native=bridge.backend;self.management=bridge.env_management
        self.profile=self.home/'headless-browser';self.profile.mkdir()
        self.process=subprocess.Popen([r'E:\chromium\src\out\Release-pgo\chrome.exe',
            '--headless=new','--no-first-run','--no-default-browser-check','--disable-extensions',
            '--remote-debugging-port=0','--remote-allow-origins=*','--user-data-dir='+str(self.profile),'about:blank'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=no_window(),startupinfo=hidden_startup_info())
        deadline=time.monotonic()+15;port_file=self.profile/'DevToolsActivePort'
        while not port_file.exists() and time.monotonic()<deadline: time.sleep(.05)
        lines=port_file.read_text().splitlines();self.port=int(lines[0])
        self.browser=BrowserCdp('ws://127.0.0.1:'+str(self.port)+lines[1])
        backend=BrowserBackend(self.port)
        backend.env_state=self.native.env_state
        backend.live_state=self.live
        backend.kernel_available=lambda:False
        backend.targets=lambda retry=True:self.pages() # Never discover a different product browser.
        bridge.backend=backend;bridge.ensure_live=self.activate
        bridge.__dict__.pop('run_step',None)
        bridge.agent_activity=AgentActivity();bridge.snapshots={}
        self.activations=[]
    def pages(self):
        with urllib.request.urlopen('http://127.0.0.1:'+str(self.port)+'/json/list',timeout=3) as response:
            return [p for p in json.load(response) if p.get('type')=='page']
    def live(self,max_age=0):
        state=self.native.env_state();pages={p['id']:p for p in self.pages()}
        for row in state['envs']:
            target=pages.get(self.targets.get(row['env_id']))
            row['tabs']=[{'target_id':target['id'],'url':target['url'],'title':target.get('title','')}] if target else []
        return state
    def activate(self,env):
        state=self.native.env_state();row=next(r for r in state['envs'] if r['env_id']==env)
        if env not in self.targets:
            context=self.browser.send('Target.createBrowserContext')['result']['browserContextId'];self.contexts[env]=context
            url=row.get('start_url') or 'http://127.0.0.1:'+str(self.server_port)+'/__login'
            if not url.startswith('http://127.0.0.1:'+str(self.server_port)+'/'): raise RuntimeError('Fixture forbids external navigation')
            target=self.browser.send('Target.createTarget',{'url':url,'browserContextId':context})['result']['targetId'];self.targets[env]=target
        state['active']=env;atomic_json(self.native.path,state);self.activations.append(env)
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            if self.bridge.backend._cdp(env).evaluate('document.readyState')=='complete': return
            time.sleep(.04)
        raise RuntimeError('Fixture page did not finish loading')
    def values(self,env):
        return self.bridge.backend._cdp(env).evaluate("({email:document.querySelector('[name=email]')?.value,password:document.querySelector('[name=password]')?.value,submitted:window.submitCount})")
    def close(self):
        self.bridge.backend.close_sessions()
        try: self.browser.send('Browser.close',timeout=2)
        except Exception: pass
        self.browser.close()
        try: self.process.wait(timeout=5)
        except subprocess.TimeoutExpired: self.process.terminate();self.process.wait(timeout=5)
