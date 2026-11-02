const assert = require('node:assert/strict')
const { spawn } = require('node:child_process')
const { once } = require('node:events')
const { pathToFileURL } = require('node:url')
const path = require('node:path')

;(async () => {
  const server = spawn('python', ['verify/environment_agent_server.py'], { cwd: path.resolve(__dirname, '..'), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
  let count = 0
  const realFetch = global.fetch
  try {
    const chunk = (await once(server.stdout, 'data'))[0].toString()
    const { port } = JSON.parse(chunk.trim())
    process.env.LAN_BRIDGE_URL = `http://127.0.0.1:${port}`
    process.env.LAN_BRIDGE_TOKEN = 'isolated-agent-tests'
    const { apply } = await import(pathToFileURL(path.resolve(__dirname, '../lan_plugin/index.js')))
    let approvalOutcome = 'rejected', asks = [], available = true
    const registry = new Map()
    let prompt
    const ctx = { tools: { register(t) { assert(!registry.has(t.name));registry.set(t.name,t) } },
      inject(_keys, callback) { callback({systemPrompt:{context(value){prompt=value}}}) },
      get(name) { assert.equal(name,'approval');return available ? {async request(value){asks.push(value);return approvalOutcome}} : undefined } }
    apply(ctx)
    const agent = { session: { id: 'plugin-session' } }
    const exec = (id) => ({ agent, callId: id, signal: new AbortController().signal })
    const run = (name,args,id) => registry.get(name).execute(args,exec(id))
    assert.equal(registry.size,23);count++
    assert(prompt.text({agent}).includes('不能改用shell'));count++
    const catalog = await run('environment_catalog',{},'catalog')
    assert.equal(catalog.tools.length,21);assert.equal(catalog.ok,true);count++
    const created = await run('environment_save',{display_name:'Plugin create'},'create')
    assert.equal(created.ok,true);assert.match(created.request_id,/^[a-f0-9]{64}$/);assert.equal(asks.length,0);count++
    const repeated = await run('environment_save',{display_name:'Plugin create'},'create')
    assert.equal(repeated.request_id,created.request_id)
    const list = await run('environment_list',{},'list')
    assert.equal(list.envs.length,4);count++
    let row = list.envs.find(r=>r.display_name==='Plugin create')
    const rejected = await run('environment_delete',{env_id:row.env_id,revision:row.revision},'denied')
    assert.equal(rejected.approval_outcome,'rejected');assert.equal(rejected.ok,false);assert.equal(asks.length,1)
    assert.equal(asks[0].agent,agent);assert.equal(asks[0].toolName,'environment_delete')
    assert(asks[0].displayReason.includes('Plugin create'));count++
    const status = await run('environment_operation_status',{request_id:rejected.request_id},'status')
    assert.equal(status.status,'cancelled');assert.equal(status.request_id,rejected.request_id);count++
    available=false
    const unavailable=await run('environment_delete',{env_id:row.env_id,revision:row.revision},'unavailable')
    assert.equal(unavailable.approval_outcome,'unavailable');assert.equal(asks.length,1);count++
    available=true;approvalOutcome='cancelled'
    assert.equal((await run('environment_delete',{env_id:row.env_id,revision:row.revision},'cancel')).ok,false);count++
    approvalOutcome='allowed-once'
    assert.equal((await run('environment_delete',{env_id:row.env_id,revision:row.revision},'approved')).ok,true)
    assert.equal((await run('environment_list',{},'list2')).envs.length,3);count++
    const account=await run('environment_accounts',{env_id:'fb-008',action:'save',accounts:[{platform_id:'facebook',login:'test@example.test',password:'sensitive-plugin-secret'}]},'account')
    const aid=account.results[0].account.account_id
    const credential=await run('environment_accounts',{env_id:'fb-008',action:'credential',account_id:aid,field:'password'},'credential')
    assert.equal(credential.value,'sensitive-plugin-secret')
    assert(!JSON.stringify(asks).includes('sensitive-plugin-secret'))
    const replay=await run('environment_accounts',{env_id:'fb-008',action:'credential',account_id:aid,field:'password'},'credential')
    assert(!JSON.stringify(replay).includes('sensitive-plugin-secret'));count++
    const scope=await run('environment_open',{env_id:'fb-008'},'scope')
    assert.equal(scope.ok,false);assert.equal(scope.scope_denied,true);count++
    let dropped=false
    global.fetch=async (url,options)=>{
      const response=await realFetch(url,options)
      if (!dropped && String(url).endsWith('/v1/session/manage') && JSON.parse(options.body).phase==='execute') {
        await response.json();dropped=true;throw new Error('connection lost after commit')
      }
      return response
    }
    const lost=await run('environment_save',{display_name:'Committed once'},'lost')
    assert.equal(lost.ok,true);assert.equal(lost.transport_recovered,true)
    global.fetch=realFetch
    const recovered=await run('environment_operation_status',{request_id:lost.request_id},'recover')
    assert.equal(recovered.status,'complete');assert.equal(recovered.result.ok,true)
    assert.equal((await run('environment_list',{},'list3')).envs.filter(r=>r.display_name==='Committed once').length,1);count++
    let malformed=false
    global.fetch=async (url,options)=>{
      const response=await realFetch(url,options)
      if (!malformed && String(url).endsWith('/v1/session/manage') && JSON.parse(options.body).phase==='execute') {
        await response.json();malformed=true;return {ok:true,json:async()=>{throw new Error('malformed response')}}
      }
      return response
    }
    const parsed=await run('environment_save',{display_name:'Malformed response once'},'malformed')
    assert.equal(parsed.ok,true);assert.equal(parsed.transport_recovered,true);global.fetch=realFetch;count++
    let lostSecret=false
    global.fetch=async (url,options)=>{
      const response=await realFetch(url,options)
      if (!lostSecret && String(url).endsWith('/v1/session/manage') && JSON.parse(options.body).phase==='execute') {
        await response.json();lostSecret=true;throw new Error('sensitive response lost')
      }
      return response
    }
    const noSecretReplay=await run('environment_accounts',{env_id:'fb-008',action:'credential',account_id:aid,field:'password'},'lost-credential')
    assert.equal(noSecretReplay.ok,false);assert.equal(noSecretReplay.error_code,'SENSITIVE_RESULT_NOT_REPLAYED')
    assert(!JSON.stringify(noSecretReplay).includes('sensitive-plugin-secret'));global.fetch=realFetch;count++
    await assert.rejects(registry.get('environment_list').execute({}, {callId:'no-session'}), /DSH session/);count++
    const aborted=exec('aborted');const controller=new AbortController();controller.abort();aborted.signal=controller.signal
    const cancelled=await registry.get('environment_save').execute({display_name:'Not created'},aborted)
    assert.equal(cancelled.ok,false);assert.equal((await run('environment_list',{},'list4')).envs.some(r=>r.display_name==='Not created'),false);count++
    console.log(JSON.stringify({ok:true,checks:count,registered_tools:registry.size,real_http:true,profile:'temporary'}))
  } finally {
    global.fetch=realFetch
    server.kill()
    await once(server,'exit').catch(()=>{})
  }
})().catch(()=>{console.error('DSH environment plugin integration failed; inspect assertions locally.');process.exitCode=1})
