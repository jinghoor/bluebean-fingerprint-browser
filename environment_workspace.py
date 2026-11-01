"""Workspace services layered on the existing native environment owner."""
from __future__ import annotations
import base64
import hashlib
import json
import secrets
import threading
import time
from contextlib import nullcontext
from urllib.parse import urlsplit
from environment_accounts import AccountStore, catalog, totp
from local_account_vault import _dpapi

class WorkspaceServices:
    def _workspace(self):
        if not hasattr(self,'accounts_v2'): self.accounts_v2=AccountStore(self.bridge.account_vault)
        return self.accounts_v2
    def session_parent(self,env_id):
        for parent,bucket in self._workspace().read().items():
            if any(a.get('session_env_id')==env_id for a in bucket.get('accounts',[])): return parent
        return env_id
    def internal_session_ids(self):
        ids=set()
        data=self._workspace().metadata()
        for bucket in data.values():
            ids.update(a.get('session_env_id') for a in bucket.get('accounts',[]) if a.get('session_env_id'))
            ids.update(bucket.get('retained_sessions',[]))
        ids-=set(data)
        return ids
    def selected_session(self,parent):
        return self._meta().get('envs',{}).get(parent,{}).get('selected_account_session') or parent
    def agent_session(self,scopes,session,parent):
        # Persist the first selection, so other chats and UI focus cannot retarget it.
        with self.lock, self._workspace().lock:
            child=scopes.selected_account(session,parent)
            if child is None: child=self.selected_session(parent)
            valid=child==parent or any(a.get('session_env_id')==child for a in self._workspace().list(parent))
            if not valid:
                from env_management import ManagementError
                raise ManagementError('所选账号已删除或更换身份，请重新选择账号；未转向其它账号',409,account_target_changed=True)
            scopes.bind_account(session,parent,child)
            return child
    def account_services(self,payload):
        env=payload.get('env_id'); action=payload.get('action','list')
        known={e['env_id'] for e in self._state()['envs']}
        if env not in known or self.session_parent(env)!=env: raise ValueError('请选择有效环境')
        store=self._workspace()
        if action=='list': return {'ok':True,'accounts':store.list(env),'platforms':catalog()}
        if action=='save':
            reply=store.save_many(env,payload.get('accounts'))
            self._record('account_save',env)
            return reply
        aid=payload.get('account_id'); a=store.get(env,aid)
        if action=='delete':
            store.delete(env,aid,payload.get('revision'))
            self._record('account_clear',env)
            return {'ok':True,'website_data_preserved':True}
        if action=='credential':
            field=payload.get('field')
            if field not in ('password','otp_secret'): raise ValueError('凭据字段无效')
            return {'ok':True,'value':a.get(field,'')}
        if action=='totp':
            if not a.get('otp_secret'): raise ValueError('此账号未保存验证器密钥')
            return {'ok':True,**totp(a['otp_secret'])}
        if action=='confirm':
            store.set_status(env,aid,'user_confirmed')
            meta=self._meta();meta['envs'].setdefault(env,{})['account_status_at']=time.time();self._write_meta(meta)
            return {'ok':True,'status':'user_confirmed','message':'已记录你确认的网站登录状态'}
        if action not in ('open','fill','check'): raise ValueError('账号操作无效')
        if not a.get('url'): raise ValueError('请先为此账号补充网站地址')
        if payload.get('code') and not __import__('re').fullmatch(r'\d{6,8}',payload['code']): raise ValueError('本次验证码请输入 6–8 位数字')
        lock=getattr(self.bridge,'execution_lock',None) or nullcontext()
        with lock:
            child=self._ensure_account_session(env,a)
            if action=='open':
                self.bridge.ensure_live(child)
                self._invalidate_live()
                # New sessions start at their entry; existing sessions keep their page.
                meta=self._meta(); meta['envs'].setdefault(env,{})['selected_account_session']=child;self._write_meta(meta)
                return {'ok':True,'session_env_id':child,'status':a.get('status','unknown'),'message':'账号网站已打开；登录状态以网站为准'}
            self.bridge.ensure_live(child)
            self._invalidate_live()
            meta=self._meta();meta['envs'].setdefault(env,{})['selected_account_session']=child;self._write_meta(meta)
            backend=self.bridge.backend
            strict=getattr(backend,'strict_resolution',False);backend.strict_resolution=True
            try:
                cdp=backend._cdp(child)
                raw=cdp.evaluate('location.href')
                current=raw if not isinstance(raw,str) or not raw.startswith('"') else json.loads(raw)
                if urlsplit(str(current)).hostname!=urlsplit(a['url']).hostname:
                    raise ValueError('当前页面与保存的网站地址不同，请先打开对应登录页面；不会向其它域名填入凭据')
                script=LOGIN_SCRIPT.replace('__PAYLOAD__',json.dumps({
                    'login':a['login'],'password':a.get('password',''),
                    'code':payload.get('code','') or (totp(a['otp_secret'])['code'] if a.get('otp_secret') else ''),
                    'fill':action=='fill'},ensure_ascii=True))
                result=cdp.evaluate(script)
                if isinstance(result,str): result=json.loads(result)
                status=result.get('status','unknown');store.set_status(env,aid,status)
                return {'ok':True,**result,'session_env_id':child}
            finally: backend.strict_resolution=strict
    def _ensure_account_session(self,parent,a):
        from env_management import definition
        state=self._state();source=next(e for e in state['envs'] if e['env_id']==parent)
        child=a['session_env_id']
        if child==parent: return child
        existing=next((e for e in state['envs'] if e['env_id']==child),None)
        row={**definition(source),'env_id':child,'display_name':source['display_name']+' / '+a['platform']+' / '+a['login'],
             'start_url':a['url'],'homepage':a['url'],'group':'账号会话',
             'fingerprint_seed':''}
        if not existing or any(existing.get(k,'')!=row[k] for k in row):
            self._send({'command':'create',**row});self._invalidate_live()
        return child
    def workspace_open(self,p,*,target_session=None):
        env=p.get('env_id')
        if env not in {e['env_id'] for e in self._state()['envs']}: raise ValueError('环境不存在')
        with getattr(self.bridge,'execution_lock',nullcontext()):
            session = target_session or self.selected_session(env)
            self.bridge.ensure_live(session)
            self._invalidate_live()
        return {'ok':True,'message':'已切换到环境','session_env_id':session}
    def draft(self,p):
        from env_management import atomic_json
        path=self.profile/'env-form-draft.dpapi.json'
        action=p.get('action','read')
        if action=='clear': path.unlink(missing_ok=True);return {'ok':True}
        if action=='save':
            value=p.get('draft')
            if not isinstance(value,dict) or len(json.dumps(value))>200000: raise ValueError('草稿过大或格式不正确')
            atomic_json(path,{'ciphertext':base64.b64encode(_dpapi(json.dumps(value,ensure_ascii=False).encode(),protect=True)).decode()})
            return {'ok':True}
        if action!='read': raise ValueError('草稿操作无效')
        return {'ok':True,'draft':json.loads(_dpapi(base64.b64decode(json.loads(path.read_text())['ciphertext']),protect=False)) if path.exists() else None}
    def bulk(self,p):
        from env_management import definition,revision
        ids=p.get('env_ids'); fields=p.get('fields')
        if not isinstance(ids,list) or not ids or len(ids)>5000 or not isinstance(fields,dict) or not set(fields)<={'group','tags'}: raise ValueError('批量操作参数无效')
        out=[]
        for env in dict.fromkeys(ids):
            row=next((e for e in self._state()['envs'] if e['env_id']==env),None)
            try:
                if not row: raise ValueError()
                self.save({**definition(row),**fields,'revision':revision(row),'allow_duplicate_name':True})
                out.append({'env_id':env,'ok':True})
            except Exception: out.append({'env_id':env,'ok':False,'error':'未保存，请检查环境和连接后重试'})
        return {'ok':all(r['ok'] for r in out),'results':out}
    def history(self,p):
        from env_management import definition,revision
        env=p.get('env_id');meta=self._meta();rows=meta['envs'].get(env,{}).get('versions',[])
        if p.get('action')!='restore': return {'ok':True,'versions':rows}
        v=next((x for x in rows if x['id']==p.get('version_id')),None)
        current=next((e for e in self._state()['envs'] if e['env_id']==env),None)
        if not v or not current: raise ValueError('历史版本不存在')
        return self.save({**v['config'],'revision':p.get('revision'),'allow_duplicate_name':True})
    def portable(self,p):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import os
        password=p.get('password','')
        if not isinstance(password,str) or len(password)<10: raise ValueError('迁移密码至少 10 个字符')
        action=p.get('action')
        if action=='export':
            salt=os.urandom(16);nonce=os.urandom(12)
            key=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)
            accounts=self._workspace().read()
            portable={env:{'accounts':[{k:v for k,v in account.items() if k!='session_env_id'}
                                       for account in bucket.get('accounts',[])]}
                      for env,bucket in accounts.items()}
            plain=json.dumps({'accounts':portable,'format':'bluebean-local-accounts','version':1},ensure_ascii=False).encode()
            data=AESGCM(key).encrypt(nonce,plain,b'bluebean-accounts-v1')
            return {'ok':True,'document':{'format':'bluebean-encrypted-accounts','version':1,'salt':base64.b64encode(salt).decode(),'nonce':base64.b64encode(nonce).decode(),'data':base64.b64encode(data).decode()}}
        doc=p.get('document',{})
        if doc.get('format')!='bluebean-encrypted-accounts' or doc.get('version')!=1: raise ValueError('迁移包格式不支持')
        try:
            key=hashlib.scrypt(password.encode(),salt=base64.b64decode(doc['salt']),n=16384,r=8,p=1,dklen=32)
            raw=AESGCM(key).decrypt(base64.b64decode(doc['nonce']),base64.b64decode(doc['data']),b'bluebean-accounts-v1')
            data=json.loads(raw)['accounts']
        except Exception: raise ValueError('密码不正确或迁移包已损坏')
        if not isinstance(data,dict): raise ValueError('迁移包格式无效')
        known={e['env_id'] for e in self._state()['envs']}
        preview=[{'env_id':env,'count':len(b.get('accounts',[])),'exists':env in known} for env,b in data.items()]
        if action=='preview': return {'ok':True,'environments':preview}
        if action!='import': raise ValueError('迁移操作无效')
        store=self._workspace();results=[]
        for env,b in data.items():
            if env not in known: continue
            existing={a['account_id']:a for a in store.records(env)}
            for a in b.get('accounts',[]):
                if a['account_id'] in existing:
                    results.append({'env_id':env,'account_id':a['account_id'],'ok':False,'error':'已有同编号账号，保留本机资料'});continue
                try:
                    store.save(env,{**a,'password_action':'replace','otp_secret_action':'replace'})
                    results.append({'env_id':env,'account_id':a['account_id'],'ok':True})
                except Exception: results.append({'env_id':env,'account_id':a.get('account_id'),'ok':False,'error':'账号格式无效'})
        return {'ok':all(r['ok'] for r in results),'results':results}

# Fill only visible fields; never auto-submit or treat absence of fields as login success.
LOGIN_SCRIPT = r'''(() => {const p=__PAYLOAD__;
 const visible=e=>e.getClientRects().length&&!e.disabled&&!e.readOnly;
 const fields=[...document.querySelectorAll('input')].filter(visible);
 const user=fields.find(e=>/username|email/i.test(e.autocomplete+' '+e.type+' '+e.name+' '+e.id))||fields.find(e=>e.type==='text'&&!/search|otp|code/i.test(e.name));
 const pass=fields.find(e=>e.type==='password');
 const otp=fields.find(e=>/one-time-code|otp|verification|totp/i.test(e.autocomplete+' '+e.name+' '+e.id));
 let filled=0;const put=(e,value)=>{if(!e||!value)return;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,value);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));filled++;};
 if(p.fill){put(user,p.login);put(pass,p.password);put(otp,p.code);}
 return {status:p.fill?(otp&&!user?'verification_required':(pass||user)?'login_required':'unknown'):(pass||user)?'login_required':'unknown',filled,
 message:p.fill?(filled?'已填入可识别字段，请在网站确认并提交；额外验证需自行完成':'未识别到登录字段，请在网站手动登录'):(pass||user)?'当前页面仍有登录字段':'无法可靠确认登录状态，请以网站页面为准'};})()'''
