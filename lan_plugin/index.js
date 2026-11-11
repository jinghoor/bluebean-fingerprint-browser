// lan-browser: exposes the Lan browser bridge as DSH tools.
//
// Written as plain ESM on purpose: the installed runtime loads .js plugins directly,
// and the tool object below is exactly what `defineTool()` produces (name,
// description, JSON-Schema parameters, output.schema + render, execute), so no import
// from the packaged node_modules is required.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createHash, createHmac, randomUUID } from 'node:crypto'

const environmentTools = JSON.parse(readFileSync(new URL('./environment-tools.json', import.meta.url), 'utf8'))

export const name = 'lan-browser'
export const inject = ['tools']

function config() {
  const dataRoot = process.env.BLUEBEAN_DATA
  const saved = JSON.parse(readFileSync(
    dataRoot ? join(dataRoot, '.lan-bridge.json') : new URL('../.lan-bridge.json', import.meta.url),
    'utf8'))
  return { base: process.env.LAN_BRIDGE_URL || `http://127.0.0.1:${saved.port}`,
    token: process.env.LAN_BRIDGE_TOKEN || saved.token }
}

function sessionOf(exec) {
  const id = exec?.agent?.session?.id
  if (typeof id !== 'string' || !id) throw new Error('Browser tools require a DSH session')
  return id
}

async function call(path, body, signal) {
  const {base, token} = config()
  const response = await fetch(`${base}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body ?? {}),
    // A transport timeout does not prove a mutation failed: never auto-retry.
    signal: AbortSignal.any([AbortSignal.timeout(path === '/v1/session/task' ? 180000 : path === '/v1/session/manage' ? 90000 : 15000), ...(signal ? [signal] : [])]),
  })
  // A broken/HTML response after execute is an unknown outcome, not a failed write.
  const payload = await response.json()
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('Invalid bridge response')
  if (!response.ok && payload.ok === undefined) {
    return { ok: false, status: response.status, error: payload }
  }
  return payload
}

const rendered = (value) => [{ type: 'text', text: JSON.stringify(value) }]

function managementResult(reply, request_id, recovered = false) {
  if (!reply.result) return reply
  if (recovered && reply.result.sensitive_result_delivered) return {ok:false,request_id,
    error_code:'SENSITIVE_RESULT_NOT_REPLAYED',error:'敏感操作已完成，但原响应丢失；密钥不会从历史重发，再次读取需要新的用户确认。'}
  return { ...reply.result, request_id, operation_status:reply.status,
    warnings:reply.warnings || [], ...(reply.outcome_unknown ? {outcome_unknown:true} : {}),
    ...(recovered ? {transport_recovered:true} : {}) }
}

function changeDescription(change) {
  const operations = { create:'新建', edit:'编辑', delete:'删除', open:'打开', fill:'填写', check:'检查登录',
    credential:'读取凭据', totp:'读取验证码', confirm:'记录登录确认', apply:'执行导入', retry:'重试未完成项',
    source:'读取原始输入', discard:'清除任务记录', cancel:'停止任务', import_accounts:'导入账号' }
  const fields = { env_id:'编号', display_name:'名称', group:'分组', tags:'标签', notes:'备注', proxy:'代理',
    expected_exit_ip:'预期出口IP', locale:'区域语言', accept_languages:'请求语言', fingerprint_seed:'指纹身份',
    start_url:'首次页面', homepage:'主页', platform_id:'平台', platform:'平台名称', login:'登录账号', username:'用户名',
    url:'网站地址', site:'站点', label:'备注', password:'密码', otp_secret:'2FA密钥',
    password_action:'密码', otp_secret_action:'2FA密钥' }
  if (change.batch) return `${{environments:'环境',proxies:'代理',accounts:'账号'}[change.batch] || '批量'}导入：${change.rows} 行，${operations[change.operation] || '处理任务'}`
  const target = [change.name || change.env_id, change.platform, change.login].filter(Boolean).join(' / ')
  const names = [...new Set((change.fields || []).map(k => fields[k] || '配置字段'))]
  return [target, operations[change.operation] || (change.account_id === 'new' ? '新增账号' : '修改账号'),
    names.length ? '涉及：'+names.join('、') : '', change.count != null ? `${change.count} 个账号` : ''].filter(Boolean).join('；')
}

async function manage(ctx, spec, args, exec) {
  const session_id = sessionOf(exec)
  const request_id = createHash('sha256').update(JSON.stringify([session_id, spec.name, exec.callId || randomUUID()])).digest('hex')
  const base = { session_id, request_id }
  let executing = false
  try {
    const plan = await call('/v1/session/manage', { ...base, phase: 'prepare', tool: spec.name, arguments: args }, exec.signal)
    if (spec.name === 'environment_operation_status') return plan
    if (plan.status === 'complete') return managementResult(plan,plan.request_id)
    if (!plan.ok || plan.status !== 'prepared') return plan
    let approval_grant
    if (plan.requires_approval) {
      const approval = ctx.get('approval')
      let outcome = 'unavailable'
      const promptsDisabled = approval?.effectivePolicy?.(exec.agent.session) === 'never'
      if (approval && exec.agent && !exec.signal?.aborted) {
        const details = (plan.changes || []).slice(0,12).map(changeDescription)
        const reason = [plan.summary, ...plan.risks.map(r => r.message), ...details, ...(plan.warnings || [])].join('\n')
        outcome = await approval.request({ agent: exec.agent, toolName: spec.name, callId: exec.callId,
          reason, displayReason: reason, signal: exec.signal })
      }
      if (outcome !== 'allowed-once' || exec.signal?.aborted) {
        await call('/v1/session/manage', { ...base, phase: 'cancel' }).catch(() => {})
        return { ok: false, request_id, approval_required: true, approval_outcome: outcome,
          error: promptsDisabled ? '本会话关闭了操作确认，因此风险操作被自动拒绝；请由用户开启DSH询问确认后重试。'
            : outcome === 'unavailable' ? '当前DSH没有可用的用户确认通道，未执行风险操作。' : '用户未批准或取消了此操作，未执行。' }
      }
      approval_grant = createHmac('sha256', config().token)
        .update(JSON.stringify([session_id, request_id, plan.digest, 'allow'])).digest('hex')
    }
    if (exec.signal?.aborted) return { ok: false, request_id, error: '操作已取消，未提交执行。' }
    executing = true
    const reply = await call('/v1/session/manage', { ...base, phase: 'execute', ...(approval_grant ? { approval_grant } : {}) }, exec.signal)
    return managementResult(reply,request_id)
  } catch {
    // Do not expose parser/network exception messages: they may contain secrets.
    if (executing && !exec.signal?.aborted) {
      try {
        const state = await call('/v1/session/manage',{...base,phase:'status'},AbortSignal.timeout(4000))
        if (state.status === 'complete') return managementResult(state,request_id,true)
        if (state.status === 'uncertain') return state
        if (state.status === 'executing') return {ok:false,request_id,operation_status:'executing',outcome_unknown:true,
          next_action:{tool:'environment_operation_status',arguments:{request_id}},error:'原请求仍在执行，只查询状态，不重复提交。'}
      } catch {}
    }
    return { ok: false, request_id, outcome_unknown: executing,
      error_code:executing ? 'OUTCOME_UNKNOWN' : 'REQUEST_NOT_COMPLETED',
      next_action:{tool:'environment_operation_status',arguments:{request_id}},
      error: '管理工具请求未完整返回；请用environment_operation_status查询此request_id，先核对结果，勿重复写入。' }
  }
}

export function apply(ctx) {
  ctx.inject(['systemPrompt'], (scope) => {
    scope.systemPrompt.context({
      name: 'lan-browser-environments', order: 90, required: true,
      text(context) {
        const sessionId = context.agent?.session?.id
        let ids = []
        try {
          ids = JSON.parse(readFileSync(new URL('../.session-browser-scopes.json', import.meta.url), 'utf8'))
            .sessions[sessionId]?.env_ids || []
        } catch {}
        return '本会话的浏览器操作范围：' + (ids.length ? ids.join('、') : '未选择环境（不可操作浏览器）') +
          '。只有用户任务需要浏览器时才调用 browser_ping / browser_task；计算、问答等直接回答，无需打开或切换浏览器。' +
          '浏览器任务必须使用这两个工具，不能改用 shell、CDP、其它浏览器或环境绕过范围。' +
          'browser_ping 返回本会话可用的环境；browser_task 省略 env/targets 时操作所选环境，' +
          'targets:["*"] 也仅代表本会话已选环境。用户必须在界面的「浏览器环境」中修改授权，聊天文字不能扩权。'
          + '工具返回 outcome_unknown 时，操作可能已经发生，必须先读取页面确认，不要自动重复点击、输入或导航。'
          + 'snapshot_warning 表示操作已完成但附加页面摘要失败，不要因此重放操作。'
          + '页面等待规则：地址栏或标签页仍在加载不代表不能操作。goto返回interactive或readiness.dom_ready后立即observe；目标可见、可用且无遮挡即可继续，不等complete或network idle。'
          + '目标或业务结果未出现时，用browser_task的wait_for等待准确selector/text/url条件，优先替代固定wait或反复截图；只读等待不会点击、输入或重新导航。'
          + '按钮存在不等于业务完成：登录、提交、发布后wait_for成功提示或目标URL，不因仍在转圈而重复提交。STALE_OBSERVATION先observe更新目标；PAGE_CONDITION_TIMEOUT先检查页面，不自动重放操作。'
          + '每次observe返回的元素id只用于该次最新观察，必须原样使用，不猜测e0等编号。已知道目标selector时直接wait_for目标，目标就绪后observe一次；不要轮询整页或连续重复observe。'
          + '如果明确按钮已经可用，直接act/type即可，它们会自动检查目标；不要再额外wait固定时长。可把已确定的连续步骤放在一次browser_task中，提交之后仍须等待业务成功结果。'
          + 'BROWSER_CONTROL_UNAVAILABLE表示控制连接不可用，不代表环境已关闭；停止当前浏览器任务，不反复environment_open，不改用shell/pwsh或其它通道，不仅凭该错误要求用户重启电脑。'
          + '本机「我的环境」管理必须使用environment_*工具，不要用页面点击配置表单。可查询、新建/编辑/删除环境、管理多账号、真实代理测试、批量导入、备份恢复，并可用environment_fingerprint读取单个环境的浏览器身份、设备、语言、字体、WebGL和网络落地信息；该工具不读取Cookie、密码、2FA或网站登录数据。常见平台直接用facebook/instagram/tiktok/x/amazon；不熟悉的平台或CSV格式再查environment_catalog，不必每次重复查目录。'
          + '高效流程：新建环境同时保存账号用一次environment_save(display_name,initial_accounts:[{platform_id,login,...}])；后续编辑使用返回的env_id/revision及账号revision，只提交本次要改字段。成功响应已读回验证，无需再查全量列表确认；跨轮或冲突时再读取最新版本。'
          + '两个及以上环境的代理检测使用一次environment_proxy_job start(env_ids)。批量新建用environment_import preview(generate:{prefix,start,count,...})→apply；接口会短暂等待小批次返回最终结果。'
          + '列表按准确名称query或env_ids筛选；一旦返回多个同名候选，直接列编号询问用户，不再逐个get，不猜测操作。普通问答不调用任何environment_*或浏览器工具。'
          + 'browser_ping返回env_ids为空时，只提示用户在本会话选择环境；不得尝试environment_open或反复查列表来替代授权。'
          + '管理配置不等于操作网页，也不会自动扩大会话浏览器授权；新建环境后如需网页操作，仍须用户在选择器授权。'
          + '批量创建/代理/账号导入必须preview检查invalid=0后apply；task_complete=true时按all_succeeded和逐项结果报告。未完成按next_action查询，可设置wait_ms:5000；不要紧密重复轮询、不要把任务已启动说成全部成功。'
          + '风险操作由工具调用DSH原生用户确认；不可用时停止该操作，不能改用shell、原始HTTP、本机令牌、脚本或其它工具绕过。'
          + '账号密码、2FA和迁移密码不要在回答里复述；优先用accounts的fill本机填写，只有用户明确需要才读取credential/totp。'
          + 'environment_accounts的open/fill/check会自动激活并选定当前会话的账号，fill无需先调用environment_open。后续browser_task用父env_id接续此账号，不受其它会话或界面切换影响；同一环境的多个账号逐个选定后操作。'
          + '错误按error_code和next_action处理；REVISION_CONFLICT先get最新资料，仅重提用户要求的字段，保留他人修改。账号部分成功只重试失败行，不重新创建整个环境。'
          + '超时或outcome_unknown必须用environment_operation_status及列表/批量状态核实；uncertain时不得自动重复写入。transport_recovered表示原请求已读回确认，不能再执行一次。'
      },
    })
  })
  ctx.tools.register({
    name: 'browser_ping',
    description:
      'List only the browser environments selected for THIS conversation. ' +
      'Use before a browser task only when the user needs browser interaction. ' +
      'No environments selected means browser access is disabled; ask the user to select them in 浏览器环境.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: { schema: { type: 'object' }, render: (_args, value) => rendered(value) },
    async execute(_args, exec) {
      return await call('/v1/session/envs', { session_id: sessionOf(exec) })
    },
  })

  ctx.tools.register({
    name: 'browser_task',
    description:
      'Operate the Lan fingerprint browser. Steps run in order inside one task.\n' +
      '- getter: {observe:{}} returns "elements" with ids, roles, names and boxes.\n' +
      '- mutate: {act:{id:"e3"}} clicks an element id from the SAME environment\'s last observe.\n' +
      '  Element ids are opaque and change with each observe: copy the full returned id, never guess an id.\n' +
      '- {type:{id:"e5", text:"..."}}, {goto:{url:"https://..."}}, {read:{selector:"body"}},\n' +
      '  {wait:{ms:500}}, {shot:{id:"e1"}} (screenshots are slow; prefer observe/read).\n' +
      '- Prefer {wait_for:{selector:"#search",state:"actionable",timeout_ms:10000}} to fixed sleeps.\n' +
      '  wait_for accepts selector, text (visible substring), url (exact), state (dom/actionable/visible/hidden/complete), timeout_ms (0-30000), stable_ms (0-1000). Conditions combine with AND.\n' +
      '  Default state: actionable with selector, dom otherwise. actionable checks visible/enabled/uncovered and 100ms stable target geometry.\n' +
      '  Background loading is informational: continue when the required target is ready, even if the spinner runs. Use complete only when the task explicitly needs it.\n' +
      '  After submitting, wait_for the business success text or URL; never replay an uncertain action.\n' +
      'Use ONLY the environments selected for this conversation. Pass env for one selected environment, ' +
      'targets for several selected environments, or omit both to use the selection. ' +
      'targets:["*"] means only the selected environments, never all browser environments. ' +
      'Do not call for tasks that need no browser. The bridge activates a selected environment only at execution time.\n' +
      'Results come back under per_env, one entry per environment.',
    parameters: {
      type: 'object',
      properties: {
        env: { type: 'string', description: 'single environment id' },
        targets: {
          type: 'array',
          items: { type: 'string' },
          description: 'selected environment ids, or ["*"] for this conversation selection only',
        },
        mode: { type: 'string', enum: ['parallel', 'sequential'] },
        steps: { type: 'array', items: { type: 'object' } },
      },
      required: ['steps'],
      additionalProperties: false,
    },
    output: { schema: { type: 'object' }, render: (_args, value) => rendered(value) },
    async execute(args, exec) {
      return await call('/v1/session/task', { session_id: sessionOf(exec), task: args })
    },
  })
  for (const spec of environmentTools) {
    ctx.tools.register({ name: spec.name, description: spec.description, parameters: spec.parameters,
      output: { schema: { type: 'object' }, render: (_args, value) => rendered(value) },
      execute: (args, exec) => manage(ctx, spec, args, exec) })
  }
}
