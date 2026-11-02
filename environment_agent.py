"""Session-bound DSH management gateway with durable operations and human risk gates."""
from __future__ import annotations
import base64
import copy
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from contextlib import ExitStack, nullcontext
from pathlib import Path

from environment_api import BY_NAME, catalogue
from env_management import ManagementError, atomic_json, definition, revision, validate_definition
from local_account_vault import _dpapi
from session_browser_scope import SessionBrowserScopes


def validate(value, schema, path='arguments'):
    kind = schema.get('type')
    types = {'object':dict, 'array':list, 'string':str, 'boolean':bool, 'integer':int, 'number':(int,float)}
    if kind in types and (not isinstance(value,types[kind]) or kind in ('integer','number') and isinstance(value,bool)):
        raise ManagementError('参数类型不正确：'+path)
    if 'enum' in schema and value not in schema['enum']: raise ManagementError('参数选项无效：'+path)
    if kind == 'object':
        fields=schema.get('properties',{})
        if schema.get('additionalProperties') is False and set(value)-set(fields):
            raise ManagementError('包含不支持的参数：'+path)
        if any(k not in value for k in schema.get('required',[])): raise ManagementError('缺少必要参数：'+path)
        for key,item in value.items():
            if key in fields: validate(item,fields[key],path+'.'+key)
    elif kind == 'array':
        if len(value)>schema.get('maxItems',5000): raise ManagementError('条目过多：'+path)
        for item in value: validate(item,schema.get('items',{}),path+'[]')
    elif kind == 'string' and len(value)>schema.get('maxLength',4*1024*1024): raise ManagementError('内容过长：'+path)
    elif kind in ('number','integer') and (value<schema.get('minimum',-float('inf')) or value>schema.get('maximum',float('inf'))):
        raise ManagementError('数值超出范围：'+path)


class EnvironmentAgentService:
    def __init__(self, bridge, signing_key):
        self.bridge=bridge; self.manager=bridge.env_management
        self.key=signing_key.encode(); self.lock=threading.RLock()
        self.directory=self.manager.profile/'env-agent-operations'
        self.active=set()

    def busy(self):
        with self.lock: return bool(self.active)

    @staticmethod
    def _key(value):
        if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value):
            raise ManagementError('管理请求编号须为64位十六进制')
        return value

    def _write(self, plan):
        plan['updated_at']=time.time()
        atomic_json(self.directory/(plan['id']+'.json'),{'version':1,
            'ciphertext':base64.b64encode(_dpapi(json.dumps(plan,ensure_ascii=False).encode(),protect=True)).decode()})

    def _read(self, request_id, session):
        path=self.directory/(self._key(request_id)+'.json')
        if not path.exists(): raise ManagementError('未找到该管理请求；查询可重试，写入请先核对列表',404)
        try: plan=json.loads(_dpapi(base64.b64decode(json.loads(path.read_text(encoding='utf-8'))['ciphertext']),protect=False))
        except Exception: raise ManagementError('本机管理请求记录不可读，未重放操作',409,outcome_unknown=True) from None
        if plan['session_id']!=session: raise ManagementError('此请求不属于当前DSH会话',403)
        if plan['status']=='executing' and request_id not in self.active:
            plan['status']='uncertain'; self._write(plan)
        return plan

    @staticmethod
    def public(plan):
        result={'ok':plan.get('result',{}).get('ok',True) and plan['status']!='cancelled','request_id':plan['id'],'status':plan['status'],'tool':plan['tool'],
            'requires_approval':bool(plan['risks']),'risks':plan['risks'],'summary':plan['summary'],
            'warnings':plan['warnings'],'digest':plan['digest'],'expires_at':plan['expires_at']}
        result['changes']=plan.get('changes',[])
        if 'result' in plan: result['result']=plan['result']
        if plan['status']=='uncertain': result.update(ok=False,outcome_unknown=True,
            error='上次请求的最终结果尚未确认；请查看环境/账号或批量任务结果，不要自动重复写入')
        return result

    def _grant(self, plan):
        message=json.dumps([plan['session_id'],plan['id'],plan['digest'],'allow'],ensure_ascii=False,separators=(',',':')).encode()
        return hmac.new(self.key,message,hashlib.sha256).hexdigest()

    def _rows(self):
        internal=self.manager.internal_session_ids()
        return {r['env_id']:r for r in self.manager._state()['envs'] if r['env_id'] not in internal}

    @staticmethod
    def failure(exc, tool=None, arguments=None):
        """Controlled, actionable errors, without returning supplied credentials."""
        reply=dict(exc.reply);args=arguments if isinstance(arguments,dict) else {}
        code=('ACCOUNT_TARGET_CHANGED' if reply.get('account_target_changed') else 'SCOPE_DENIED' if reply.get('scope_denied') else 'REVISION_CONFLICT' if reply.get('conflict')
            else 'APPROVAL_REQUIRED' if reply.get('approval_required') else 'NOT_FOUND' if exc.status==404
            else 'CONFLICT' if exc.status==409 else 'SERVICE_UNAVAILABLE' if exc.status>=500 else 'VALIDATION_ERROR')
        reply['error_code']=code
        reply['retryable']=code in ('REVISION_CONFLICT','SERVICE_UNAVAILABLE') and not reply.get('outcome_unknown')
        if code=='ACCOUNT_TARGET_CHANGED': reply['next_action']={'tool':'environment_accounts','arguments':{'env_id':args.get('env_id'),'action':'list'},'message':'账号已删除或更换身份，刷新账号列表并按用户目标重新选择；不要重复打开旧目标或回退到其它账号。'}
        elif code=='SCOPE_DENIED': reply['next_action']={'kind':'user_action','message':'由用户在本会话浏览器环境选择器中授权；配置管理无需网页授权。'}
        elif code in ('REVISION_CONFLICT','CONFLICT') and isinstance(args.get('env_id'),str):
            reply['next_action']={'tool':'environment_get','arguments':{'env_id':args['env_id']},'message':'先读取最新资料，仅重新提交用户要求的字段，不覆盖外部修改。'}
        else: reply['next_action']={'kind':'inspect' if code=='NOT_FOUND' else 'correct_input','message':'核对目标编号、必填字段和服务状态；不要重复提交已完成项目。'}
        return reply

    @staticmethod
    def _platform(value):
        from environment_accounts import catalog
        aliases={'fb':'facebook','ig':'instagram','twitter':'x','推特':'x','脸书':'facebook','亚马逊':'amazon'}
        candidate=aliases.get(value.strip().casefold(),value.strip().casefold())
        return next((p['id'] for p in catalog() if candidate in (p['id'].casefold(),p['name'].casefold())),value)

    def _scope(self, session, env):
        if not hasattr(self.bridge,'browser_scopes'): raise ManagementError('请先选择本会话允许操作的浏览器环境',403)
        try: self.bridge.browser_scopes.require(session,[env])
        except (PermissionError,ValueError): raise ManagementError('网页操作仅限本会话已选择的环境；请在浏览器环境选择器中授权',403,scope_denied=True) from None

    def _prepare(self, tool, arguments, request_id, session):
        spec=BY_NAME.get(tool)
        if not spec: raise ManagementError('未知的环境管理工具',404)
        validate(arguments,spec['parameters'])
        a=copy.deepcopy(arguments); action=spec['action']; risks=[]; warnings=[]; envs=[]; checks={}; account_checks={}
        for item in a.get('accounts',[]) + a.get('initial_accounts',[]):
            if 'platform_id' in item: item['platform_id']=self._platform(item['platform_id'])
        sensitive=False; read=False; browser=False; changes=[]
        rows=self._rows()
        def known(env):
            if env not in rows: raise ManagementError('环境不存在或属于内部账号会话，请使用父环境编号',404)
            if env not in envs: envs.append(env)
            return rows[env]
        def risk(code, message):
            if not any(r['code']==code for r in risks): risks.append({'code':code,'message':message})
        if action in ('catalog','list','get','status','maintenance','operation'):
            read=True
            if action=='get': known(a['env_id'])
        elif action=='save':
            create=a.get('create_only',not a.get('env_id'))
            if create:
                a.setdefault('env_id','env-'+secrets.token_hex(12));a['create_only']=True
                if a['env_id'] in rows: raise ManagementError('环境编号已经存在，不能覆盖新建',409)
                if not a.get('fingerprint_seed'): a['fingerprint_seed']=secrets.token_hex(32)
                if any(r.get('fingerprint_seed')==a['fingerprint_seed'] for r in rows.values()):
                    risk('shared_fingerprint','指定的指纹种子已被其它环境使用，可能削弱环境隔离。')
                desired=validate_definition(a); envs.append(a['env_id']);checks[a['env_id']]=None
            else:
                row=known(a.get('env_id'));desired=validate_definition(a,row)
                if a.get('revision')!=revision(row): raise ManagementError('编辑前请读取最新revision',409,conflict=True)
                if any(desired[k]!=definition(row)[k] for k in ('fingerprint_seed','locale','accept_languages')):
                    risk('browser_identity','修改指纹或语言身份可能影响网站登录和风控；网站数据保留，需重启后完整生效。')
                if desired['proxy']!=definition(row)['proxy']:
                    warnings.append('代理变更需重启蓝豆浏览器后完整生效，请先检测出口。')
                if desired['ip_mode'] == 'dynamic' and definition(row)['ip_mode'] != 'dynamic':
                    risk('dynamic_ip', '改为动态 IP 后将不再提示出口 IP 变化，请确认此环境确实使用动态代理。')
            if a.get('relink_preserved'): risk('relink','此编号可能保留旧账号和网站数据，将重新关联旧资料。')
            a={**a,**desired}
            if a.get('initial_accounts'):
                if not create: raise ManagementError('initial_accounts仅用于新建；已有环境的账号请使用environment_accounts')
                validated=[];identities=set()
                for index,item in enumerate(a['initial_accounts']):
                    try:
                        account=self.manager.imports.validate_account(item)
                        account['site']=item.get('site','')
                        if len(account['site'])>80: raise ValueError('站点名称过长')
                        identity=(account['platform_id'],account['login'],account['url'])
                        if identity in identities: raise ValueError('初始账号重复')
                        identities.add(identity)
                    except ValueError as exc: raise ManagementError('初始账号第'+str(index+1)+'项：'+str(exc)) from None
                    validated.append({**account,'client_id':'agent-'+request_id[:32]+'-'+str(index)})
                a['initial_accounts']=validated
            changes.append({'env_id':a['env_id'],'operation':'create' if create else 'edit','count':len(a.get('initial_accounts',[])),
                'name':desired['display_name'],'fields':[k for k in desired if create or desired[k]!=definition(rows[a['env_id']])[k]]})
        elif action=='accounts':
            env=a['env_id']; known(env); verb=a.setdefault('action','list')
            store=self.manager._workspace(); old={r['account_id']:r for r in store.list(env)}
            if verb=='list': read=True
            elif verb=='save':
                updates=a.get('accounts')
                if not updates: raise ManagementError('请提供1–100条账号资料')
                for i,u in enumerate(updates):
                    current=old.get(u.get('account_id'))
                    if u.get('account_id') and not current: raise ManagementError('待编辑账号不存在',404)
                    if current:
                        if u.get('revision')!=current['revision']: raise ManagementError('账号已修改，请读取最新revision',409)
                        account_checks[env+':'+current['account_id']]=current['revision']
                        if any(u.get(k,current.get(k))!=current.get(k) for k in ('platform_id','login')):
                            risk('account_identity','修改账号或平台将更换隔离会话，并清除旧密码和2FA。')
                            u['confirm_identity_change']=True
                        if u.get('url',current.get('url'))!=current.get('url'):
                            risk('account_site_change','将修改已有账号的网站地址；之后本机填写会使用新网站，请确认域名可信。')
                        if any(u.get(k+'_action') in ('replace','clear') or u.get(k) for k in ('password','otp_secret')):
                            risk('credential_overwrite','将替换或清除已保存账号的密码/2FA，旧密钥不会自动恢复。')
                    else: u.setdefault('client_id','agent-'+request_id[:32]+'-'+str(i))
                    changes.append({'env_id':env,'account_id':u.get('account_id','new'),
                        'platform':u.get('platform_id',(current or {}).get('platform_id','')),
                        'login':u.get('login',(current or {}).get('login','')),
                        'fields':[k for k in u if k not in ('revision','client_id','account_id','confirm_identity_change')]})
            else:
                account=old.get(a.get('account_id'))
                if not account: raise ManagementError('请提供有效的account_id',404)
                account_checks[env+':'+account['account_id']]=account['revision']
                changes.append({'env_id':env,'account_id':account['account_id'],'platform':account['platform_id'],
                    'login':account['login'],'operation':verb})
                if verb=='delete':
                    if a.get('revision')!=account['revision']: raise ManagementError('删除前请读取账号最新revision',409)
                    risk('account_delete','删除账号资料及本机保存的密码/2FA；原网站登录数据仍保留。')
                elif verb in ('credential','totp'):
                    sensitive=True; risk('credential_read','将向当前DSH会话提供密码、2FA密钥或临时验证码；它可能保留在会话记录中。优先使用本机fill填写。')
                elif verb in ('open','fill','check'): browser=True;self._scope(session,env)
                elif verb=='confirm': warnings.append('只记录用户确认的登录状态，不能代替网站实际登录校验。')
        elif action=='remove':
            row=known(a['env_id'])
            if a['revision']!=revision(row): raise ManagementError('删除前请读取最新revision',409)
            risk('environment_delete','将移除环境定义并先备份；保留账号和网站数据。当前活动环境不能移除。')
        elif action=='bulk':
            if not a.get('env_ids') or not a.get('fields'): raise ManagementError('请明确列出环境和要修改的字段')
            for env in dict.fromkeys(a['env_ids']): known(env)
            warnings.append('批量修改只作用于明确列出的环境，返回逐项结果。')
        elif action=='proxy-test':
            if a.get('env_id'): a.setdefault('proxy',known(a['env_id']).get('proxy','direct'))
            if 'proxy' not in a: raise ManagementError('请提供env_id或完整proxy地址')
            read=True
        elif action=='proxy-job':
            verb=a.setdefault('action','status');read=verb=='status'
            if verb=='start':
                if not a.get('env_ids'): raise ManagementError('批量检测需提供env_ids')
                for env in a['env_ids']: known(env)
                a.setdefault('request_id','agent-'+request_id[:40])
        elif action=='import':
            verb=a.setdefault('action','status');read=verb in ('status','history')
            if verb=='preview':
                a.setdefault('request_id',request_id[:32])
                if a.get('generate'): a.setdefault('kind','environments')
            elif verb not in ('status','history'):
                with self.manager.imports.lock: job=copy.deepcopy(self.manager.imports._load(a.get('job_id')))
                envs.extend(dict.fromkeys(r['env_id'] for r in job['rows'] if r.get('env_id')))
                changes.append({'batch':job['kind'],'job_id':job['id'],'operation':verb,'rows':len(job['rows'])})
                if verb in ('apply','retry') and job['kind']=='proxies':
                    risk('proxy_batch_overwrite','批量替换这些环境的代理配置，可能改变出口IP；需重启后完整生效，请检查对应关系。')
                if verb=='source':
                    sensitive=True; risk('import_source','原始导入文本可能含账号密码或2FA；读取会把它提供给当前DSH会话。')
                if verb=='discard' and job['status']!='complete': risk('discard_input','将删除未完成批次的输入和进度记录，已成功创建的数据保留。')
        elif action=='history':
            known(a['env_id']);read=a.get('action')!='restore'
            if not read:
                if a.get('revision')!=revision(rows[a['env_id']]): raise ManagementError('恢复前请读取最新revision',409)
                risk('restore_version','将使用历史版本覆盖当前环境配置，网站数据与账号保留。')
        elif action=='restore':
            read=a.get('action')!='apply'
            if not read:
                preview=self.manager.restore({**a,'action':'preview'})
                if a.get('preview_token')!=preview['preview_token']: raise ManagementError('恢复内容已变化，请重新预览',409)
                if not a.get('env_ids') or not set(a['env_ids'])<={r['env_id'] for r in preview['changes']}:
                    raise ManagementError('请选择预览中的恢复环境')
                envs.extend(a['env_ids']);risk('restore_configuration','恢复将新增或覆盖所选环境的配置；网站数据保留，代理/身份变更需重启。')
        elif action=='open': known(a['env_id']);browser=True;self._scope(session,a['env_id'])
        elif action=='draft':
            sensitive=True;risk('workspace_draft','草稿可能包含密码；读取会进入当前会话，保存/清除会影响管理页尚未完成的输入。')
        elif action=='portable':
            verb=a['action'];read=verb=='preview'
            if not read: risk('account_transfer','将导出本机加密账号包或向本机导入账号，包包含密码/2FA但不包含网站登录数据；请妥善保管迁移密码。')
            if verb=='import':
                preview=self.manager.portable({**a,'action':'preview'})
                for item in preview['environments']:
                    if item['exists']:
                        known(item['env_id']);changes.append({'env_id':item['env_id'],'operation':'import_accounts','count':item['count']})
        elif action=='account':
            known(a['env_id']);risk('legacy_credentials','将覆盖或清除旧版单账号凭据，不能自动恢复；新账号应使用多账号接口。')
        elif action=='proxy-attach': known(a['env_id'])
        if not read:
            for env in envs:
                checks.setdefault(env,revision(rows[env]) if env in rows else None)
        return {'action':action,'arguments':a,'risks':risks,'warnings':warnings,'read':read and not sensitive,
                'sensitive':sensitive,'browser':browser,'env_ids':list(dict.fromkeys(envs)),
                'env_checks':checks,'account_checks':account_checks,'changes':changes}

    def _read_result(self, action, args):
        if action=='catalog': return catalogue()
        if action in ('list','get'):
            result=self.manager.list(); rows=result['envs']
            if action=='get':
                return {'ok':True,'environment':self._compact(next(r for r in rows if r['env_id']==args['env_id']))}
            query=args.get('query','').casefold()
            rows=[r for r in rows if (not args.get('env_ids') or r['env_id'] in args['env_ids'])
                  and (not args.get('group') or r.get('group')==args['group'])
                  and query in ' '.join(str(r.get(k,'')) for k in ('env_id','display_name','group','tags','notes')).casefold()]
            offset=args.get('offset',0);limit=args.get('limit',50);page=[self._compact(r) for r in rows[offset:offset+limit]]
            if not args.get('include_accounts',True):
                for row in page: row.pop('accounts',None)
            return {**{k:v for k,v in result.items() if k in ('ok','active','warning','account_warning','live_status')},'envs':page,'total':len(rows),
                    'offset':offset,'next_offset':offset+len(page) if offset+len(page)<len(rows) else None}
        return self._job_result(action,args,self.manager.dispatch(action,args))

    @staticmethod
    def _compact(row):
        # History and platform catalogues have dedicated tools; omit repeated
        # metadata from ordinary lookups while preserving configuration/revisions.
        return copy.deepcopy({k:v for k,v in row.items() if k not in ('versions','restart_pending_pid')})

    def _job_result(self,action,args,result):
        if action not in ('import','proxy-job') or not isinstance(result.get('job'),dict): return result
        job=result['job'];verb=args.get('action','status')
        deadline=time.monotonic()+args.get('wait_ms',1500)/1000
        if verb in ('start','apply','retry','status'):
            while job.get('status') in ('running','cancelling') and time.monotonic()<deadline:
                time.sleep(min(.075,max(0,deadline-time.monotonic())))
                try:
                    result=self.manager.dispatch(action,{'action':'status','job_id':job['id']});job=result.get('job') or job
                except Exception:
                    result={**result,'progress_warning':'任务已启动，但附加进度读取暂不可用；保留此任务编号继续查询，勿重新启动。'}
                    break
        status=job.get('status');terminal=status not in ('preview','running','cancelling')
        succeeded=status=='complete' and not job.get('failed',0) and not job.get('invalid',0)
        if action=='proxy-job': succeeded=succeeded and all(r.get('ok') for r in job.get('results',[])) and job.get('done')==job.get('total')
        result={**result,'task_complete':terminal,'all_succeeded':succeeded}
        tool='environment_import' if action=='import' else 'environment_proxy_job'
        if status in ('running','cancelling'):
            result['next_action']={'tool':tool,'arguments':{'action':'status','job_id':job['id'],'wait_ms':5000},'message':'任务仍在执行；按此查询等待结果，不要重新start/apply。'}
        elif status=='preview':
            result['next_action']=({'kind':'correct_input','message':'先修正invalid错误并重新预览，不能执行。'} if job.get('invalid')
                else {'tool':tool,'arguments':{'action':'apply','job_id':job['id']},'message':'校验通过，仅预览尚未写入；用户要求完成创建时继续执行。'})
        elif not succeeded: result['next_action']={'kind':'inspect_results','message':'检查逐项结果，仅重试可恢复条目；已成功项目不可重建。'}
        return result

    def _check(self, plan):
        if self.bridge.permissions.get('kill_switch'): raise ManagementError('Agent操作已被停止开关禁用',403)
        rows=self._rows()
        for env,expected in plan['env_checks'].items():
            actual=revision(rows[env]) if env in rows else None
            if expected!=actual: raise ManagementError('确认期间环境配置已变化，请重新读取并准备操作',409,conflict=True)
        for key,expected in plan['account_checks'].items():
            env,aid=key.split(':',1)
            current=next((a for a in self.manager._workspace().list(env) if a['account_id']==aid),None)
            if not current or current.get('revision')!=expected: raise ManagementError('确认期间账号已变化，请重新读取',409,conflict=True)
        if plan['browser']: self._scope(plan['session_id'],plan['arguments']['env_id'])

    def _execute(self, plan):
        tracker=getattr(self.bridge,'agent_activity',None);token=None
        try:
            if plan['browser'] and tracker:
                env=plan['arguments']['env_id'];a=plan['arguments']
                effective=(self.manager._workspace().get(env,a['account_id'])['session_env_id'] if a.get('account_id') else self.manager.agent_session(self.bridge.browser_scopes,plan['session_id'],env))
                token=tracker.begin(effective)
            arguments=plan['arguments'];initial=arguments.get('initial_accounts')
            if plan['action']=='open':
                effective=self.manager.agent_session(self.bridge.browser_scopes,plan['session_id'],arguments['env_id'])
                result=self.manager.workspace_open(arguments,target_session=effective)
            else:
                result=self.manager.dispatch(plan['action'],{k:v for k,v in arguments.items() if k!='initial_accounts'})
            if plan['action']=='accounts' and arguments.get('action') in ('open','fill','check') and result.get('ok'):
                self.bridge.browser_scopes.bind_account(plan['session_id'],arguments['env_id'],result['session_env_id'])
            if plan['action']=='save' and result.get('ok'):
                env=arguments['env_id']
                if initial:
                    accounts=self.manager.account_services({'env_id':env,'action':'save','accounts':initial})
                    result.update(ok=accounts['ok'],initial_account_results=accounts['results'],
                        partial=not accounts['ok'],config_saved=True)
                    if not accounts['ok']: result.update(error_code='PARTIAL_RESULT',next_action={
                        'tool':'environment_accounts','arguments':{'env_id':env,'action':'list'},
                        'message':'环境已保存，只修正或重试initial_account_results中的失败账号，不要重建环境或重发成功账号。'})
                row=next((r for r in self.manager._state()['envs'] if r['env_id']==env),None)
                if row: result['environment']={**definition(row),'revision':revision(row),'accounts':self.manager._workspace().list(env)}
            if plan['action']=='accounts' and arguments.get('action')=='save' and not result.get('ok'):
                result.update(error_code='PARTIAL_RESULT',next_action={'tool':'environment_accounts',
                    'arguments':{'env_id':arguments['env_id'],'action':'list'},
                    'message':'保留逐项成功结果，刷新资料后只修正或重试失败账号，不要整批重发。'})
            if token: tracker.finish(token,result.get('ok',False));token=None
            return result
        finally:
            if token: tracker.finish(token,False)

    def dispatch(self, payload):
        if not isinstance(payload,dict): raise ManagementError('请求必须是对象')
        session=SessionBrowserScopes.session_key(payload.get('session_id'))
        request_id=self._key(payload.get('request_id'));phase=payload.get('phase')
        if phase=='prepare':
            args=payload.get('arguments',{});tool=payload.get('tool')
            identity=hashlib.sha256(json.dumps([tool,args],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            with self.lock:
                if (self.directory/(request_id+'.json')).exists():
                    plan=self._read(request_id,session)
                    if plan['input_digest']!=identity: raise ManagementError('同一请求编号不能用于不同操作',409)
                    return self.public(plan)
                if request_id in self.active: raise ManagementError('此请求正在准备，请稍后查询，勿重复提交',409)
                self.active.add(request_id)
            try:
                # Imports take their own lock before reading config. Never invert that
                # order, or hold the gateway lock across a slow network probe.
                with (nullcontext() if tool=='environment_import' else self.manager.lock):
                    prepared=self._prepare(tool,args,request_id,session)
                if prepared['action']=='operation':
                    with self.lock: return self.public(self._read(args['request_id'],session))
                if prepared['read']:
                    result=self._read_result(prepared['action'],prepared['arguments'])
                    return {'ok':result.get('ok',False),'status':'complete','result':result,'request_id':request_id,'requires_approval':False}
                if self.bridge.permissions.get('kill_switch'): raise ManagementError('Agent操作已被停止开关禁用',403)
                digest=hashlib.sha256(json.dumps(prepared,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                rows=self._rows()
                labels=[rows.get(env,{}).get('display_name',env) for env in prepared['env_ids'][:8]]
                plan={**prepared,'id':request_id,'tool':tool,'session_id':session,'input_digest':identity,'digest':digest,
                      'summary':BY_NAME[tool]['description'].split('。')[0]+' · '+str(len(prepared['env_ids']))+'个环境'+('：'+'、'.join(labels) if labels else ''),
                      'status':'prepared','created_at':time.time(),'expires_at':time.time()+900}
                with self.lock:
                    if (self.directory/(request_id+'.json')).exists():
                        existing=self._read(request_id,session)
                        if existing['input_digest']!=identity: raise ManagementError('同一请求编号不能用于不同操作',409)
                        return self.public(existing)
                    if self.directory.exists():
                        for path in self.directory.glob('*.json'):
                            if path.stat().st_mtime<time.time()-7*86400 and path.stem not in self.active: path.unlink(missing_ok=True)
                        if len(list(self.directory.glob('*.json')))>=2000: raise ManagementError('本机管理请求记录过多，请稍后维护',503)
                    self._write(plan);return self.public(plan)
            finally:
                with self.lock: self.active.discard(request_id)
        with self.lock:
            if phase=='status': return self.public(self._read(request_id,session))
            if phase=='cancel':
                plan=self._read(request_id,session)
                if plan['status']=='prepared': plan['status']='cancelled';self._write(plan)
                return self.public(plan)
            if phase!='execute': raise ManagementError('不支持的管理请求阶段')
            plan=self._read(request_id,session)
            if plan['status']!='prepared': return self.public(plan)
            if time.time()>plan['expires_at']: raise ManagementError('确认请求已过期，请重新准备',409)
            if plan['risks'] and not hmac.compare_digest(str(payload.get('approval_grant','')),self._grant(plan)):
                raise ManagementError('需要DSH可信宿主取得用户确认；confirm=true不能代替用户批准',403,approval_required=True)
            # Check and dispatch use the same configuration/account locks as native management.
            self.active.add(request_id)
            plan['status']='executing'
            try: self._write(plan)
            except Exception:
                self.active.discard(request_id);raise ManagementError('请求记录未能保存，未执行写入',503) from None
        try:
            with ExitStack() as stack:
                if plan['browser']: stack.enter_context(getattr(self.bridge,'execution_lock',nullcontext()))
                # Import workers validate each saved revision themselves; never
                # hold config/account locks while calling their job dispatcher.
                if plan['action']!='import':
                    stack.enter_context(self.manager.lock)
                    stack.enter_context(self.manager._workspace().lock)
                self._check(plan); result=self._execute(plan)
            result=self._job_result(plan['action'],plan['arguments'],result)
        except ManagementError as exc:
            result={**self.failure(exc,plan['tool'],plan['arguments']),'request_id':request_id}
        except (ValueError,TypeError): result={'ok':False,'error_code':'VALIDATION_ERROR',
            'error':'配置或账号参数无效，请检查目录、必填项与revision后重试',
            'next_action':{'tool':'environment_catalog','arguments':{},'message':'检查参数格式与必填项；批量预览需kind与generate或含表头text，勿盲目重试。'}}
        except Exception: result={'ok':False,'outcome_unknown':True,'error':'操作结果待核实；请先查询请求状态、环境或批量任务，不要重复写入'}
        with self.lock:
            plan['status']='uncertain' if result.get('outcome_unknown') else 'complete'
            plan['result']=({'ok':True,'sensitive_result_delivered':True,'note':'敏感结果不缓存到请求状态；再次读取需重新取得用户确认'} if plan['sensitive'] and result.get('ok') else result)
            try: self._write(plan)
            except Exception:
                result={**result,'outcome_unknown':True,'warning':'操作已返回但请求记录保存失败；先核对实际结果，不要重放'}
            finally: self.active.discard(request_id)
            response={**self.public(plan),'result':result,'ok':result.get('ok',False)}
        try:
            with self.manager.lock: self.manager._record('agent_management',status=plan['status'],tool=plan['tool'],count=len(plan['env_ids']))
        except OSError: pass
        return response
