"""Local multi-account records. Secrets never enter list responses or audit logs."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from local_account_vault import LocalAccountVault, _dpapi

# Entry URLs are ordinary website entry points; no claim of vendor integration.
PLATFORMS = [
    ('facebook','Facebook','社交媒体','https://www.facebook.com/'),
    ('instagram','Instagram','社交媒体','https://www.instagram.com/'),
    ('tiktok','TikTok','社交媒体','https://www.tiktok.com/'),
    ('x','X','社交媒体','https://x.com/'),
    ('youtube','YouTube','社交媒体','https://www.youtube.com/'),
    ('linkedin','LinkedIn','社交媒体','https://www.linkedin.com/'),
    ('pinterest','Pinterest','社交媒体','https://www.pinterest.com/'),
    ('amazon','Amazon','跨境电商','https://sellercentral.amazon.com/'),
    ('ebay','eBay','跨境电商','https://www.ebay.com/'),
    ('shopee','Shopee','跨境电商','https://seller.shopee.com/'),
    ('lazada','Lazada','跨境电商','https://sellercenter.lazada.com/'),
    ('tiktok-shop','TikTok Shop','跨境电商','https://seller.tiktokglobalshop.com/'),
    ('etsy','Etsy','跨境电商','https://www.etsy.com/'),
    ('aliexpress','AliExpress','跨境电商','https://login.aliexpress.com/'),
    ('shopify','Shopify','跨境电商','https://accounts.shopify.com/'),
    ('google','Google','邮箱及其他','https://accounts.google.com/'),
    ('microsoft','Microsoft','邮箱及其他','https://login.live.com/'),
    ('custom','自定义网站','邮箱及其他',''),
]
SITES = {
 'amazon': [('美国','https://sellercentral.amazon.com/'),('欧洲','https://sellercentral-europe.amazon.com/'),('日本','https://sellercentral.amazon.co.jp/'),('澳大利亚','https://sellercentral.amazon.com.au/')],
 'shopee': [('新加坡','https://seller.shopee.sg/'),('马来西亚','https://seller.shopee.com.my/'),('菲律宾','https://seller.shopee.ph/'),('泰国','https://seller.shopee.co.th/'),('越南','https://banhang.shopee.vn/'),('印度尼西亚','https://seller.shopee.co.id/')],
 'lazada': [('新加坡','https://sellercenter.lazada.sg/'),('马来西亚','https://sellercenter.lazada.com.my/'),('菲律宾','https://sellercenter.lazada.com.ph/'),('泰国','https://sellercenter.lazada.co.th/'),('越南','https://sellercenter.lazada.vn/'),('印度尼西亚','https://sellercenter.lazada.co.id/')],
}
def catalog():
    return [dict(id=i,name=n,category=c,url=u,sites=[dict(name=a,url=b) for a,b in SITES.get(i,[])]) for i,n,c,u in PLATFORMS]
def totp(secret, now=None):
    now = time.time() if now is None else now
    raw = re.sub(r'\s','',secret).upper().rstrip('=')
    key = base64.b32decode(raw+'='*((-len(raw))%8))
    digest = hmac.new(key,struct.pack('>Q',int(now)//30),hashlib.sha1).digest()
    offset = digest[-1]&15
    return {'code':f'{(struct.unpack("!I",digest[offset:offset+4])[0]&0x7fffffff)%1000000:06d}',
            'remaining':30-int(now)%30,'expires_at':(int(now)//30+1)*30}

class AccountStore:
    def __init__(self, legacy):
        self.legacy=legacy
        self.path=legacy.path.with_name('env-accounts-v2.dpapi.json')
        self.cipher=LocalAccountVault(self.path)
        self.lock=threading.RLock()
    def _document(self):
        if not self.path.exists(): return {'version':2,'entries':{}}
        doc=json.loads(self.path.read_text(encoding='utf-8'))
        if doc.get('version') not in (1,2) or not isinstance(doc.get('entries'),dict): raise ValueError('multi-account vault format is unsupported')
        return doc
    def _write(self,data):
        entries={}
        for env,bucket in data.items():
            accounts=[]
            for account in bucket.get('accounts',[]):
                public=self.public(account)
                secret={k:account.get(k,'') for k in ('password','otp_secret')}
                raw=json.dumps(secret,ensure_ascii=False,separators=(',',':')).encode()
                accounts.append({'summary':public,'ciphertext':base64.b64encode(_dpapi(raw,protect=True)).decode()})
            entries[env]={'accounts':accounts,'retained_sessions':list(bucket.get('retained_sessions',[]))}
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix='.env-accounts-',suffix='.tmp',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                json.dump({'version':2,'entries':entries},stream,ensure_ascii=False,separators=(',',':'))
                stream.flush();os.fsync(stream.fileno())
            os.replace(tmp,self.path)
        finally:
            try:os.unlink(tmp)
            except FileNotFoundError:pass
    def metadata(self):
        doc=self._document();out={}
        if doc['version']==2:
            for env,bucket in doc['entries'].items():
                out[env]={'accounts':[dict(item.get('summary',{})) for item in bucket.get('accounts',[])],
                          'retained_sessions':list(bucket.get('retained_sessions',[]))}
        # The legacy vault exposes cached non-secret summaries without retaining credentials here.
        try: legacy_ids=list(json.loads(self.legacy.path.read_text(encoding='utf-8')).get('entries',{}))
        except (OSError,ValueError,TypeError): legacy_ids=[]
        try: legacy_summaries=self.legacy.summaries(legacy_ids)
        except Exception: legacy_summaries={}
        for env,summary in legacy_summaries.items():
            if env not in out and summary.get('has_account'):
                out[env]={'accounts':[{'account_id':'legacy-'+hashlib.sha256(env.encode()).hexdigest()[:16],
                    'platform_id':'custom','platform':summary.get('platform',''),'username':'',
                    'login':summary.get('username',''),'label':summary.get('label',''),'url':'',
                    'site':'','session_env_id':env,'status':'unknown','revision':1,
                    'has_password':summary.get('has_password',False),'has_otp':summary.get('has_otp',False)}],
                    'retained_sessions':[]}
        return out
    def read(self):
        doc=self._document();data={}
        if doc['version']==1:
            data=self.cipher._read()
        else:
            for env,bucket in doc['entries'].items():
                accounts=[]
                for item in bucket.get('accounts',[]):
                    raw=_dpapi(base64.b64decode(item['ciphertext'],validate=True),protect=False)
                    secrets_doc=json.loads(raw.decode('utf-8'))
                    accounts.append({**item['summary'],**secrets_doc})
                data[env]={'accounts':accounts,'retained_sessions':list(bucket.get('retained_sessions',[]))}
        # Legacy remains untouched and is imported deterministically once per env.
        for env,old in self.legacy._read().items():
            if env not in data and old:
                data[env]={'accounts':[{'account_id':'legacy-'+hashlib.sha256(env.encode()).hexdigest()[:16],
                    'platform_id':'custom','platform':old.get('platform',''),'username':'',
                    'login':old.get('username',''),'label':old.get('label',''),'url':'','site':'',
                    'password':old.get('password',''),'otp_secret':old.get('otp_secret',''),
                    'session_env_id':env,'status':'unknown','revision':1}]}
        return json.loads(json.dumps(data))
    def records(self,env):
        with self.lock: return self.read().get(env,{}).get('accounts',[])
    @staticmethod
    def public(a):
        return {**{k:v for k,v in a.items() if k not in ('password','otp_secret')},
                'has_password':bool(a.get('password')),'has_otp':bool(a.get('otp_secret'))}
    def list(self,env): return self.metadata().get(env,{}).get('accounts',[])
    def get(self,env,account_id):
        a=next((a for a in self.records(env) if a['account_id']==account_id),None)
        if a is None: raise ValueError('账号不存在，请刷新账号列表')
        return a
    def save_many(self,env,updates):
        if not isinstance(updates,list) or not 1<=len(updates)<=100: raise ValueError('每次可保存 1–100 条账号')
        results=[]
        for update in updates:
            if not isinstance(update,dict):
                results.append({'ok':False,'error':'账号格式无效'});continue
            try: results.append({'ok':True,'account':self.save(env,update),'client_id':update.get('client_id')})
            except ValueError as exc: results.append({'ok':False,'account_id':update.get('account_id'),'client_id':update.get('client_id'),'error':str(exc)})
            except OSError: results.append({'ok':False,'account_id':update.get('account_id'),'client_id':update.get('client_id'),'error':'本机加密保存失败，请保留输入后重试'})
        return {'ok':all(r['ok'] for r in results),'results':results}
    def save(self,env,u):
        if not isinstance(u,dict): raise ValueError('账号格式无效')
        with self.lock:
            data=self.read(); bucket=data.setdefault(env,{'accounts':[]}); rows=bucket['accounts']
            aid=u.get('account_id') or ('acc-'+hashlib.sha256((env+':'+str(u['client_id'])).encode()).hexdigest()[:24] if u.get('client_id') else 'acc-'+secrets.token_hex(10))
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',aid): raise ValueError('账号编号无效')
            old=next((a for a in rows if a['account_id']==aid),{})
            if old and u.get('revision')!=old.get('revision'):
                if not u.get('account_id') and u.get('client_id'): return self.public(old)
                raise ValueError('账号已被其他页面修改，请重新载入')
            a={**old,'account_id':aid}
            for k,limit in {'platform_id':40,'platform':80,'username':240,'login':240,'label':120,'url':2048,'site':80}.items():
                value=u.get(k,old.get(k,''))
                if not isinstance(value,str) or len(value)>limit: raise ValueError('账号字段无效或过长')
                a[k]=value.strip()
            p=next((p for p in catalog() if p['id']==a['platform_id']),None)
            if not p or not a['login']: raise ValueError('请选择平台并填写登录账号')
            a['platform']=p['name'] if p['id']!='custom' else a['platform'] or '自定义网站'
            a['url']=a['url'] or p['url']
            if a['url']:
                parsed=urlsplit(a['url'])
                if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in a['url']): raise ValueError('请输入有效网站地址')
            identity_changed=bool(old and any(a.get(k)!=old.get(k) for k in ('platform_id','login')))
            if identity_changed and not u.get('confirm_identity_change'): raise ValueError('更换账号或平台需确认，旧密码和2FA将清除')
            for k in ('password','otp_secret'):
                mode=u.get(k+'_action','replace' if u.get(k) else 'keep')
                if mode not in ('keep','replace','clear'): raise ValueError('凭据操作无效')
                a[k]='' if identity_changed or mode=='clear' else old.get(k,'')
                if mode=='replace':
                    value=u.get(k,'')
                    if not isinstance(value,str) or len(value)>2048: raise ValueError('凭据格式无效')
                    a[k]=value
            if a['otp_secret']:
                a['otp_secret']=re.sub(r'\s','',a['otp_secret']).upper()
                try: totp(a['otp_secret'])
                except Exception: raise ValueError('2FA 请填写验证器密钥，不是六位验证码')
            a['session_env_id']=(old.get('session_env_id') if not identity_changed else None) or 'acct-'+hashlib.sha256((env+':'+aid+(':'+str(time.time_ns()) if identity_changed else '')).encode()).hexdigest()[:24]
            a['revision']=old.get('revision',0)+1; a['updated_at']=time.time()
            a['status']='unknown' if identity_changed else old.get('status','saved')
            if identity_changed and old.get('session_env_id') and old['session_env_id']!=env:
                bucket.setdefault('retained_sessions',[]).append(old['session_env_id'])
            bucket['accounts']=[x for x in rows if x['account_id']!=aid]+[a]
            self._write(data)
            return self.public(a)
    def delete(self,env,aid,revision):
        with self.lock:
            data=self.read(); old=self.get(env,aid)
            if old['revision']!=revision: raise ValueError('账号已变化，请刷新后重试')
            if old.get('session_env_id') and old['session_env_id']!=env: data[env].setdefault('retained_sessions',[]).append(old['session_env_id'])
            data[env]['accounts']=[a for a in data[env]['accounts'] if a['account_id']!=aid]
            self._write(data)
    def set_status(self,env,aid,status):
        with self.lock:
            data=self.read()
            for a in data.get(env,{}).get('accounts',[]):
                if a['account_id']==aid: a['status']=status;a['status_at']=time.time()
            self._write(data)
