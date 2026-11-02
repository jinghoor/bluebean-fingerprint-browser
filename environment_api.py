"""One typed catalogue for local REST documentation and the DSH tool registry."""
from __future__ import annotations
import json
from pathlib import Path

def obj(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {}, 'required': list(required), 'additionalProperties': False}

def text(description='', **extra): return {'type': 'string', 'description': description, **extra}
def enum(*values): return {'type': 'string', 'enum': list(values)}
def array(items, maximum=200): return {'type': 'array', 'items': items, 'maxItems': maximum}
ID = text('环境准确编号；不能用模糊名称代替', maxLength=64)
IDS = array(ID, 1000)
CONFIG = {key: text(label) for key, label in {
    'env_id':'环境编号；新建时可省略，自动生成独立编号', 'display_name':'环境名称',
    'group':'分组', 'tags':'逗号分隔的标签', 'notes':'备注',
    'proxy':'http(s)/socks4/socks5://主机:端口，或direct/system；当前不支持认证代理',
    'expected_exit_ip':'预期出口IP（选填）', 'locale':'区域语言，例如zh-CN',
    'accept_languages':'请求语言，例如zh-CN,zh', 'fingerprint_seed':'指纹种子；新建默认独立随机生成',
    'start_url':'首次打开的完整http(s)网址', 'homepage':'新标签页完整http(s)网址'}.items()}
CONFIG['ip_mode'] = {**enum('static', 'dynamic'), 'description': '静态 IP 在首次启动时核对已保存出口；动态 IP 不因出口变化提示风险'}
ACCOUNT = obj({**{k: text(v) for k,v in {
    'account_id':'编辑时必填现有账号编号；新建省略', 'client_id':'新建幂等编号；省略由工具生成',
    'platform_id':'目录中的平台id，例如facebook、instagram、amazon或custom',
    'platform':'自定义平台的显示名称', 'login':'登录账号', 'username':'用户名', 'url':'网站地址；默认使用平台目录地址',
    'site':'站点名称', 'label':'备注', 'password':'密码；优先保存后用fill在本机填写，勿在回答复述',
    'otp_secret':'2FA验证器密钥，不是六位验证码；勿在回答复述'}.items()},
    'revision':{'type':'integer','minimum':1},
    'password_action':enum('keep','replace','clear'), 'otp_secret_action':enum('keep','replace','clear')})
INITIAL_ACCOUNT = obj({k:v for k,v in ACCOUNT['properties'].items()
    if k not in ('account_id','client_id','revision','password_action','otp_secret_action')},['platform_id','login'])
WAIT = {'type':'integer','minimum':0,'maximum':10000,'description':'最多等待任务结果的毫秒数；默认1500，小批次通常直接返回最终结果。0立即返回，仍运行时按next_action查询。'}

def tool(name, action, description, properties=None, required=()):
    return {'name':name, 'action':action, 'description':description,
            'parameters':obj(properties, required)}

TOOLS = [
 tool('environment_catalog','catalog','查看我的环境API能力、平台目录、导入格式及风险规则，不操作浏览器。'),
 tool('environment_list','list','查询本机环境配置与账号摘要，可筛选分页。不会打开浏览器或返回密码/2FA。',
      {'query':text('按名称、编号、分组、标签搜索'),'env_ids':IDS,'group':text(),
       'limit':{'type':'integer','minimum':1,'maximum':200},'offset':{'type':'integer','minimum':0},'include_accounts':{'type':'boolean'}}),
 tool('environment_get','get','读取一个环境的完整配置、revision及账号摘要，编辑前先读取。',{'env_id':ID},['env_id']),
 tool('environment_status','status','查看运行、当前环境、网络与Agent操作状态，不激活环境。'),
 tool('environment_fingerprint','fingerprint','读取单个环境的浏览器指纹信息：Windows/平台、Chromium内核、User-Agent、屏幕窗口、字体探测、语言时区、WebGL/GPU、硬件和代理出口落地信息。环境未运行时返回基于指纹种子的配置推算值；不读取或返回Cookie、密码、2FA或网站登录数据。',
      {'env_id':ID},['env_id']),
 tool('environment_save','save','新建或编辑环境。新建可省略编号，自动生成独立指纹；新建同时添加账号用initial_accounts数组，一次调用完成。编辑必须提供env_id及当前revision。只写配置，不打开环境；账号部分失败会保留环境并返回逐项结果。',
      {**CONFIG,'create_only':{'type':'boolean'},'revision':text('编辑前由environment_get读取'),
       'initial_accounts':array(INITIAL_ACCOUNT,100),
       'allow_duplicate_name':{'type':'boolean'},'relink_preserved':{'type':'boolean'}}),
 tool('environment_delete','remove','移除环境定义，自动备份，保留网站数据和账号；必须用户确认。不能移除当前活动环境。',
      {'env_id':ID,'revision':text()},['env_id','revision']),
 tool('environment_accounts','accounts','一个环境的多个平台账号：list/save/delete/open/fill/check/confirm/credential/totp。save支持新建或编辑多条账号；编辑带revision。fill只在本机向对应网站填写，不自动提交。网页操作限本会话所选环境。删除、覆盖已有密码/2FA、修改账号身份、向会话读取密钥需用户确认。',
      {'action':enum('list','save','delete','open','fill','check','confirm','credential','totp'),
       'env_id':ID,'account_id':text(),'revision':{'type':'integer','minimum':1},
       'accounts':array(ACCOUNT,100),'field':enum('password','otp_secret'),'code':text('本次6–8位验证码，不保存')},['env_id']),
 tool('environment_proxy_test','proxy-test','真实测试代理出口、HTTPS往返延迟、城市、时区及IP。可传env_id读取该环境代理，或传proxy测试尚未保存的节点；失败不能说成功，地理信息不可用需如实说明。',
      {'env_id':ID,'proxy':CONFIG['proxy'],'expected_exit_ip':text(),'ip_mode':CONFIG['ip_mode']}),
 tool('environment_proxy_job','proxy-job','检测两个及以上环境的代理优先使用本工具。start传env_ids，status/cancel传job_id。自动短暂等待，小批次可直接返回最终结果；只检测不修改代理。仍运行时按next_action查询。',
      {'action':enum('start','status','cancel'),'env_ids':IDS,'job_id':text(),'request_id':text(),'wait_ms':WAIT}),
 tool('environment_import','import','批量新建环境、导入代理、多账号。生成示例：{action:preview,kind:environments,generate:{prefix:运营,start:1,count:4,group:海外}}；直连不用提供proxies。CSV预览传kind和text。预览校验后用{action:apply,job_id:预览返回编号}；小批次通常直接完成，仍运行按next_action查询。每批最多200行，保留已成功条目。',
      {'action':enum('preview','apply','status','history','cancel','retry','discard','source'),
       'kind':enum('environments','proxies','accounts'),'text':text('CSV/TSV文本，表头和必填项见environment_catalog'),
       'generate':obj({'prefix':text(),'start':{'type':'integer','minimum':0,'maximum':999999},
          'count':{'type':'integer','minimum':1,'maximum':200},'group':text(),'locale':text(),'start_url':text(),
          'proxies':text('每行一个代理，数量与环境数相等；留空直连')},['prefix','count']),
       'job_id':text(),'request_id':text('通常省略，由宿主自动生成；手动提供须32位十六进制'),'wait_ms':WAIT}),
 tool('environment_bulk_update','bulk','批量设置分组或标签。只修改明确列出的环境，返回逐行结果，不打开浏览器。',
      {'env_ids':IDS,'fields':obj({'group':text(),'tags':text()})},['env_ids','fields']),
 tool('environment_history','history','读取环境配置历史；restore恢复指定版本须用户确认，并携带当前revision。',
      {'env_id':ID,'action':enum('list','restore'),'version_id':text(),'revision':text()},['env_id']),
 tool('environment_backup','backup','在本机创建配置备份，不包含账号密码或网站登录数据。',{'reason':text()}),
 tool('environment_restore','restore','预览或恢复环境配置备份。传backup_id或document；apply还需preview_token与env_ids。恢复覆盖须用户确认，网站数据保留。',
      {'action':enum('preview','apply'),'backup_id':text(),'document':{'type':'object'},'preview_token':text(),'env_ids':IDS}),
 tool('environment_maintenance','maintenance','只读检查配置问题、备份目录及操作历史；不清理Cookie，不删除网站数据。'),
 tool('environment_open','open','打开/切换本会话已经选中的环境，沿用其选择的账号会话。不能扩大浏览器范围。',{'env_id':ID},['env_id']),
 tool('environment_draft','draft','读取、保存或清除管理页本机草稿。草稿可能含密码；所有操作须用户确认，避免泄露或覆盖用户未完成的表单。',
      {'action':enum('read','save','clear'),'draft':{'type':'object'}}),
 tool('environment_account_transfer','portable','本机账号加密迁移：export/preview/import。导出或导入须用户确认，使用至少10字符迁移密码；包不包含网站登录数据。',
      {'action':enum('export','preview','import'),'password':text('迁移密码，至少10字符；不要在回答中重复'),'document':{'type':'object'}},['action','password']),
 tool('environment_proxy_attach','proxy-attach','把尚未保存节点的检测receipt关联到对应环境。代理配置必须完全一致。',
      {'env_id':ID,'receipt':text()},['env_id','receipt']),
 tool('environment_legacy_account','account','兼容旧版单账号资料的保存/清除，仅用于维护旧数据；新增多账号请使用environment_accounts。覆盖或清除需用户确认。',
      {'env_id':ID,'action':enum('save','clear'),'account':obj({k:text() for k in ('platform','label','username','password','otp_secret')})},['env_id']),
 tool('environment_operation_status','operation','查询环境管理工具请求的最终结果。网络超时或outcome_unknown时必须先用此工具核实，不要重复写入。请求只属于当前DSH会话。',
      {'request_id':text('工具返回的64位请求编号')},['request_id']),
]
BY_NAME = {t['name']:t for t in TOOLS}

def catalogue():
    from environment_accounts import catalog
    from environment_import import HEADERS
    return {'ok':True,'api_version':2,'tools':[{'name':t['name'],'action':t['action']} for t in TOOLS],
            'platforms':catalog(),'import_headers':{k:[{'label':n,'key':key} for n,key in v] for k,v in HEADERS.items()},
            'import_examples':{'environments':'display_name,group,proxy\n运营环境001,运营,direct',
                'proxies':'environment,proxy\nfb-020,http://127.0.0.1:8080',
                'accounts':'environment,platform_id,login,password,otp_secret\nfb-020,facebook,user@example.com,,'},
            'proxy_auth':False,'cloud_sync':False,'batch_import_limit':200,
            'recipes':{'create_with_accounts':'environment_save(display_name,initial_accounts:[{platform_id,login,...}])；直接使用返回的env_id/revision与账号摘要',
                'edit':'准确env_id+最新revision；只提交要修改的字段。已有返回结果足够时无需重复get',
                'batch':'environment_import preview→检查invalid=0→apply；按all_succeeded报告结果，未完成时使用next_action',
                'proxies':'多环境用environment_proxy_job start(env_ids)，默认短暂等待；单节点用environment_proxy_test',
                'ambiguous':'同名或模糊匹配多个环境时请用户选择编号，不猜测，不写入',
                'recovery':'遵循error_code/next_action；超时只查询结果，不重复执行写入'},
            'management_scope':'本机环境配置可管理；不自动打开浏览器',
            'browser_scope':'打开环境、账号、网页检查及填写，必须在当前会话已选环境中',
            'risk_policy':'删除、敏感读取、覆盖账号密钥或身份、指纹修改及恢复覆盖使用DSH原生用户确认。模型传confirm=true无效。'}

def openapi():
    return {'openapi':'3.1.0','info':{'title':'蓝豆我的环境与DSH Agent API','version':'1.2.0'},
            'servers':[{'url':'http://127.0.0.1:8799'}],
            'security':[{'localBearer':[]}],
            'components':{'securitySchemes':{'localBearer':{'type':'http','scheme':'bearer'}},
                'schemas':{t['name']:t['parameters'] for t in TOOLS}},
            'paths':{'/v1/session/manage':{'post':{'summary':'会话绑定的管理网关：prepare→必要时DSH确认→execute；status查询结果',
              'requestBody':{'required':True,'content':{'application/json':{'schema':obj({
                  'phase':enum('prepare','execute','status','cancel'), 'session_id':text(),
                  'tool':enum(*BY_NAME),'arguments':{'type':'object'},'request_id':text(),
                  'approval_grant':text('由可信工具宿主在用户批准后签名；不能用布尔确认代替')},['phase','session_id','request_id'])}}},
              'responses':{'200':{'description':'结构化结果、预览或风险提示'},'401':{'description':'需要本机Bearer令牌'},'409':{'description':'配置冲突，未覆盖'}}}},
              '/v1/lan/manage/{action}':{'post':{'summary':'已有管理页REST接口（本机可信客户端）',
                 'parameters':[{'name':'action','in':'path','required':True,'schema':enum(*sorted({t['action'] for t in TOOLS}-{ 'catalog','get','operation'}))}],
                 'requestBody':{'content':{'application/json':{'schema':{'type':'object'}}}},
                 'responses':{'200':{'description':'各操作返回结构化结果'}}}}},
            'x-agent-tools':TOOLS,'x-risk-policy':catalogue()['risk_policy']}

if __name__ == '__main__':
    target=Path(__file__).parent/'lan_plugin/environment-tools.json'
    target.write_text(json.dumps(TOOLS,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    target.with_name('environment-openapi.json').write_text(json.dumps(openapi(),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Generated',len(TOOLS),'typed DSH environment tools.')
