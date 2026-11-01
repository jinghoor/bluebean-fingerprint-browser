# 我的环境：DSH Agent 与本机 API

所有资料保留在本机。管理接口复用现有环境、账号、代理、历史与导入服务；不引入云同步。DSH 注册 21 个管理工具，并保留 browser_ping、browser_task。

## 工具清单

| 工具 | 功能 |
| --- | --- |
| environment_catalog | 能力、平台目录、CSV/TSV表头、示例与限制 |
| environment_list / environment_get | 分页筛选、详情、revision、账号摘要；不返回密钥 |
| environment_status | 运行、当前、网络、Agent状态 |
| environment_save / environment_delete | 新建、编辑、移除环境定义 |
| environment_accounts | 多账号列表、批量新增/编辑、删除、打开、填写、检查、确认登录、按需读取密码/2FA验证码 |
| environment_proxy_test | 单节点真实出口、HTTPS延迟、IP、城市、时区检测 |
| environment_proxy_job | 多环境异步代理检测、停止、进度与逐项结果 |
| environment_import | 环境批量生成、环境/代理/账号CSV或TSV导入、预览、执行、停止、重试、历史、返回修改、清除任务记录 |
| environment_bulk_update | 批量分组、标签 |
| environment_history | 配置历史与版本恢复 |
| environment_backup / environment_restore | 本机配置备份、恢复预览、选择恢复 |
| environment_maintenance | 检查配置、备份和操作记录 |
| environment_open | 切换当前会话已授权环境 |
| environment_draft | 本机表单草稿读取、保存、清除 |
| environment_account_transfer | 账号加密迁移包导出、预览、导入 |
| environment_proxy_attach | 将未保存节点的检测结果关联到已保存环境 |
| environment_legacy_account | 旧单账号凭据兼容维护 |
| environment_operation_status | 同会话管理请求结果查询与超时核对 |

## 用户可以直接提出的任务

- “新建一个叫海外运营的环境，先不要打开。”自动分配环境编号和独立随机指纹种子。
- “批量新建20个环境，名称从运营001开始。”先生成预览，再执行，轮询完成并报告失败条目。
- “给fb-020添加Facebook和Instagram账号。”同一父环境可保存多个平台、多个账号，各自使用隔离网站会话。
- “把这些代理按环境编号导入，然后检测全部节点。”代理导入预览后整批确认一次，随后异步检测并报告真实结果。
- “修改这个账号的密码”或“删除这个环境”。工具弹出DSH原生确认，展示环境、账号和变更字段，凭据值不进入确认说明。

## 风险与授权

| 类别 | 处理 |
| --- | --- |
| 查询、新建环境、新增账号、分组/标签 | 直接执行，写操作仍检查停止开关 |
| 单环境代理变化 | 返回重启生效提醒；建议先检测出口 |
| 指纹/语言身份、已有账号身份/网站变化、覆盖或清除密码/2FA | 原生用户确认后才提交 |
| 删除环境/账号、恢复覆盖、账号迁移、批量代理覆盖 | 原生用户确认；恢复/批量操作保留逐项结果 |
| 读取密码/2FA、原始导入输入、表单草稿 | 原生用户确认；敏感结果只交付一次，请求状态不能再次取密钥 |
| 浏览器打开、账号填写/检查 | 准备和执行时均验证当前DSH会话所选父环境，不自动增加授权 |

新建环境不自动打开，不自动加入会话的浏览器授权。填写只向保存的网站域名填入可识别字段，不自动提交；不把填入成功或无登录框宣称为登录成功。

DSH确认策略为never时，风险操作会被自动拒绝，工具提示用户开启询问确认；不擅自改用户的会话策略。确认通道缺失、用户拒绝或取消均不执行。模型不能传confirm=true代替批准。

这些检查保护注册的管理工具。持有本机Bearer令牌的管理员仍可访问原管理页REST接口；本机拥有完整文件/进程权限的程序不是该机制的隔离对象。不要把令牌提供给模型或外部服务。

## 接口约定

认证使用现有.lan-bridge.json中的本机Bearer令牌。服务继续仅绑定本机回环地址，不提供免认证公网接口。

- GET /v1/lan/manage/catalog：能力与格式。
- GET /v1/lan/manage/openapi：OpenAPI 3.1，包含21个工具参数Schema。
- POST /v1/session/manage：DSH可信工具宿主使用的会话网关，仅接受Bearer认证，不使用网页Origin替代认证。
- POST /v1/lan/manage/{action}：保留原管理页面接口，action包括list、maintenance、save、account、remove、backup、restore、proxy-test、proxy-job、accounts、draft、bulk、history、portable、open、proxy-attach、status、import。

网关请求体上限4MB：

```json
{
  "phase": "prepare",
  "session_id": "由DSH执行上下文提供",
  "request_id": "64位十六进制幂等编号",
  "tool": "environment_save",
  "arguments": {"display_name": "海外运营"}
}
```

只读请求直接返回complete/result；写请求先返回prepared、摘要、changes、warnings和requires_approval。宿主在需要时调用ctx.approval.request，只有allowed-once才产生绑定会话、请求编号和计划摘要的HMAC grant，再提交execute。批准有效期15分钟；执行前重新核对环境及账号revision。session_id不属于模型工具参数。

请求编号由可信session、tool、callId派生。重复提交同编号返回已记录结果，不重放。若网络中断，用environment_operation_status查询同一request_id；executing说明仍在执行，uncertain/outcome_unknown说明结果待核对，不能当作失败自动重试。崩溃后的执行中记录转为uncertain，不继续旧写入。所有操作计划和结果使用Windows DPAPI加密，保留7天、最多2000条；审计不保存参数、密码或2FA。

批量导入每批最多200行、原始文本最多1MB。必须preview→检查逐行结果→apply→status轮询。取消保留已成功行，retry只处理可恢复条目，冲突需重新预览。返回“任务已启动”不等于全部成功。

环境编辑/删除必须先get取得字符串revision；账号编辑/删除使用账号列表中的整数revision。批量分组、导入、恢复仍由后端固定版本并检查冲突。

## 当前边界

- 代理支持http、https、socks4、socks5，以及direct/system；当前原生代理不支持用户名密码认证，接口会拒绝，不伪装成功。
- 延迟是实际HTTPS出口请求耗时；城市、时区依赖出口地理服务，可用性不足时明确说明，不编造数据。直连/system不执行远端代理落地检测。
- 删除环境仅移除定义并先备份，保留账号和网站数据；删除账号清除保存资料，保留原网站会话数据。
- 配置备份不包含Cookie或账号密钥；加密账号迁移包含账号密钥，迁移密码至少10字符，不含网站登录数据。
- 网页操作仍使用原浏览器工具和会话范围；不因调用管理工具而扩大浏览器权限。

机器可读离线文档：lan_plugin/environment-openapi.json。生成命令：python environment_api.py。

## 2026-10-31 稳定性与调用效率升级（API能力版本2）

- 新建环境可以一次传 `initial_accounts` 保存多个账号；所有账号先预校验，再创建环境。返回环境读回值、revision和逐项账号结果；部分账号保存失败时保留已成功资料，仅补失败项。支持Facebook/FB/脸书、Instagram/IG等平台别名。
- 列表和详情精简重复平台目录、历史版本。需要目录时调用catalog，需要历史时调用history。新建成功可直接使用返回的编号和revision；跨轮编辑先刷新最新版。
- 批量导入与代理检测支持 `wait_ms`（0–10000，默认1500）；等待不占配置或账号锁。通过 `task_complete`、`all_succeeded` 判断终态，未完成按 `next_action` 查询，不重复启动任务。
- 返回 `error_code`、`retryable`、`next_action`。配置冲突先刷新再只修改用户要求字段；多候选同名环境先询问用户。账号目标被删除或身份更换返回 `ACCOUNT_TARGET_CHANGED`，重新选择账号，不回退到其它账号。
- 写入已完成但HTTP响应丢失或JSON损坏时，工具只自动查询一次原请求状态，不重发写入。敏感响应丢失不从历史重交付密钥，重新读取仍需确认。
- `environment_accounts` 的open/fill/check成功后，将账号网站会话绑定到当前DSH会话；fill自动打开账号网站，无需先open。后续 `browser_task(env:父环境编号)` 与 `environment_open` 沿用本会话账号，不受其它DSH会话、界面选择影响。同一父环境多个账号可以依次选定；不同父环境分别保留目标。
- 账号绑定随授权持久化，撤销父环境授权同时清除绑定。每个网页步骤前重新验证授权和账号身份；账号删除或身份更换后停止，不静默操作其它账号。界面的当前账号状态仍反映实际已激活页面。

验证使用实际DSH与真实模型、临时资料和独立无头Chromium；不会向正式环境创建验收账号。覆盖自然语言配置、多轮编辑、批量创建、代理任务协议、原生确认允许/拒绝、故障恢复、多环境账号填写、同父环境不同DSH会话账号路由。代理测试中的37ms/城市为受控探针数据，只验证协议，不作为公网测速证据；独立BrowserContext用于验证真实DOM操作链，不替代产品内核原生分区隔离验收。证据与小样本效率对比见 `.work/environment-agent-rounds/README.md`。
