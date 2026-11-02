// Only installed in disposable SDK test profiles; never in the product profile.
export const inject = ['tools']
export function apply(ctx) {
  ctx.tools.guard(exec => {
    if (exec.name === 'browser_task' && (exec.arguments?.steps || []).some(s => s.goto && !s.goto.url.startsWith(process.env.LAN_TEST_BROWSER_ORIGIN+'/')))
      return '隔离验收仅允许本机测试网页，外部导航未执行。'
    return exec.name.startsWith('environment_') || ['browser_ping','browser_task'].includes(exec.name)
      ? undefined : '隔离验收只允许环境管理与测试浏览器工具；其它工具未执行。'
  })
  ctx.on('approval/request', req => (req.agent.session.id.includes('rejected_delete') ? 'rejected'
    : process.env.LAN_TEST_APPROVAL === 'allow' ? 'allowed-once' : 'rejected'))
}
