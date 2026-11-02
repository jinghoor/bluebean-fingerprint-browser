"""Real DSH chats sharing one parent, using independent headless account targets."""
import json
import tempfile
from pathlib import Path
from verify.test_environment_agent_rounds import Round, CASE
from verify.environment_agent_browser_fixture import BrowserSandbox

def main():
    reports=[]
    for number in range(1,4):
        with tempfile.TemporaryDirectory(prefix='bluebean-account-round-') as folder:
            home=Path(folder);(home/'management').mkdir();test=Round(home,number)
            try:
                b=test.bridge;m=b.env_management;scopes=b.browser_scopes
                test.sandbox=BrowserSandbox(b,home,test.server.server_port)
                accounts=[]
                for index in (1,2):
                    account=m._workspace().save('fb-008',{'platform_id':'custom','platform':'路由验收',
                        'label':'验收账号'+str(index),'login':'account'+str(index)+'@example.test',
                        'password':'isolated-fixture-'+str(index),
                        'url':'http://127.0.0.1:'+str(test.server.server_port)+'/__login'})
                    accounts.append(account)
                for index,account in enumerate(accounts,1):
                    sid='routing-chat-'+str(index);scopes.set(sid,['fb-008'],{'fb-008'})
                    child=account['session_env_id']
                    def filled(c,r,a,child=child,index=index,sid=sid):
                        values=test.sandbox.values(child)
                        return values['email']=='account'+str(index)+'@example.test' and values['submitted']==0 and scopes.selected_account(sid,'fb-008')==child
                    test.ask('select_account_'+str(index),'请接管 fb-008 里的“验收账号'+str(index)+'”，用保存资料填入它的登录页，停在提交前，不要显示密码。',filled,sid)
                # The second chat/UI now selects account 2. First chat must still read account 1.
                for index in (1,2,1):
                    child=accounts[index-1]['session_env_id'];sid='routing-chat-'+str(index)
                    other=accounts[2-index]['session_env_id']
                    meta=m._meta();meta['envs'].setdefault('fb-008',{})['selected_account_session']=other;m._write_meta(meta)
                    start=len(test.sandbox.activations)
                    def routed(c,r,a,child=child,start=start):
                        touched=test.sandbox.activations[start:]
                        return bool(touched) and set(touched)=={child} and any(x['name']=='browser_task' for x in c) and test.sandbox.values(child)['submitted']==0
                    test.ask('followup_'+str(index)+'_'+str(start),'请打开并检查刚才我在 fb-008 选择的那个账号页面，只读取页面标题或正文，确认仍在登录页；不切换账号，不填表不提交，不读取密码。',routed,sid)
                # In a single chat, switching accounts must replace only that chat's target.
                child=accounts[1]['session_env_id']
                test.ask('switch_same_parent','现在将 fb-008 切换到“验收账号2”，用保存资料填入，停在提交前，不显示密码。',
                    lambda c,r,a:scopes.selected_account('routing-chat-1','fb-008')==child and test.sandbox.values(child)['submitted']==0,'routing-chat-1')
                reports.extend(test.records)
            finally:test.close()
        (CASE/'account-routing.json').write_text(json.dumps({'cases':reports},ensure_ascii=False,indent=2),encoding='utf-8')
    summary={'rounds':3,'passed':sum(r['ok'] for r in reports),'total':len(reports)}
    print(json.dumps(summary),flush=True)
    return 0 if summary['passed']==summary['total'] else 1

if __name__=='__main__':raise SystemExit(main())
