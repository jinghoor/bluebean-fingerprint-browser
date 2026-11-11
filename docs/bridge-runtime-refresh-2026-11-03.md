# 运行中旧桥接服务导致浏览器控制失联（2026-11-03）

## 根因与现场验证

开发浏览器正常运行，9333 调试端点可以直连；启动器复用了 11 月 2 日创建的
旧桥接进程。旧进程未加载新版端口记录发现与回环直连修复，遇到缺失的
DevToolsActivePort 后退到命令行发现，最终将控制失联描述成浏览器未运行。

启动时原有健康检查只检查实例归属和进程存活，未比较它实际加载的源码版本。
因此更新磁盘源码后再打开浏览器，也可能继续使用旧 Python 模块。

本轮恢复后，通过原有会话授权、原有环境和 `/v1/session/task` 的 observe 步骤
读取页面成功。没有重新授权、切换到其他环境或发送页面输入。

## 修复

- `bridge_runtime.py` 计算桥接入口及本地 Python 导入闭包的内容摘要，包含延迟导入；
  仅读取程序源码，不读取用户环境、账号、配置、令牌或聊天记录。
- 桥接模块导入时固定记录已加载版本；健康接口返回该固定版本，不随磁盘变化
  假装已加载新代码。能力列表包含 wait_for。
- 启动器比较磁盘和已加载版本。健康且当前的服务复用；旧服务只在已认证、归属
  相同安装/数据/浏览器配置、记录 PID 一致、进程映像和创建时间一致、且空闲时
  才替换。缺少忙碌状态也按不能安全替换处理。
- 替换只操作桥接进程，保留控制端口和令牌；不关闭浏览器、清除环境或更换授权。
  源码变化但服务忙碌时返回 reload_pending，不重复启动；产品启动服务明确记录
  bridge-reload-pending，避免将旧版本报告为最新就绪。
- 端口发现失败使用 BROWSER_CONTROL_UNAVAILABLE，并明确 action_sent:false。
  该结果只表示控制连接不可用，不证明浏览器已关闭；停止依赖步骤，不提示输入
  可能执行过，也不引导反复开环境、转用 shell 或仅据此要求重启电脑。
- DSH 指导同步这一错误处理规则。后续加载插件时生效；当前连接恢复不依赖模型
  是否遵守提示，实际浏览器操作由桥接端执行。

## 验证

- 107 项 Python 回归及 19 项插件检查通过。
- 实进程隔离测试验证旧版本替换、保留端口和凭据、并发启动只有一个新实例、
  当前版本复用、忙碌版本保留，以及不同数据目录和认证隔离。
- 固定端口记录存在时，不需要 DevToolsActivePort 文件，也不需要启动 PowerShell。
- 当前开发版现场验证：新服务返回已加载版本与 wait_for 能力；同一授权环境
  observe 成功，保留原浏览器进程及标签页。

```powershell
python -m unittest verify.test_bridge_runtime verify.test_agent_execution_optimization verify.test_page_readiness verify.test_browser_reliability verify.test_session_browser_scope installer.test_browser_startup installer.test_clean_startup
python verify/test_bridge_reload_native.py
python installer/test_service_isolation.py <测试专用输出目录>
node verify/test_environment_agent_plugin.cjs
```

实进程报告：`.work/bridge-reload/63d5ea4d8c/report.json`；隔离报告：
`.work/bridge-runtime-20261103/results.json`。

本轮未复验 DSH 内部 pwsh 初始化故障，也未执行抖音 5 分钟观看任务或视频兼容性
验收；不能将这些项目计为通过。未打包或上传 GitHub。
