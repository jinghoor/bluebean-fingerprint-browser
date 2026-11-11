# Agent 页面等待

`goto` 原先已经等待正确文档的 DOM 就绪，而非等待全部资源下载。
本次补齐目标等待、动作前检查、页面状态反馈与 DSH 调用指导。
加载指示器和后台资源继续正常工作。

## 使用流程

1. `goto` 返回 `readiness.dom_ready` 后 `observe`。
2. 目标已可操作时直接 `act` / `type`，否则 `wait_for` 对应目标。
3. 提交后等待明确的业务结果；动作派发成功不代表业务成功。

元素 id 是不透明标识，每次 observe 都会更新。只使用对应环境最近一次 observe
返回的完整 id，不自行拼接或继续使用上次观察的 id。过期 id 返回
STALE_OBSERVATION，明确标记本次没有发送输入。

```json
{"steps":[{"wait_for":{"selector":"#submit","state":"actionable","timeout_ms":10000}}]}
```

```json
{"steps":[{"wait_for":{"selector":"#status","text":"提交成功","state":"visible","timeout_ms":10000}}]}
```

`wait_for` 可组合 selector、text（渲染文本子串）、url（精确匹配），条件为 AND。
state 支持 dom、complete、visible、hidden、actionable。指定 selector 时默认
actionable，否则默认 dom。visible/hidden 指当前视口中的可见状态；hidden 不等于
DOM 节点已删除。actionable 额外检查禁用、遮挡和指针命中。
只要指定了 selector，dom/complete 也必须等到匹配节点存在。未指定 selector/text
的 DOM、URL 和完整加载等待不读取页面全文或执行不必要的布局检测。

timeout_ms 默认 10000，范围 0–30000；stable_ms 范围 0–1000，actionable 默认
100，其余默认 0。只比较目标自身几何及命中点，不因页面其他动画而延长等待。
只读探测不向页面安装观察器、全局属性或 DOM 标记。

## 正确性边界

现代 observe 结果记录文档标识、目标定位和描述。act/type 在发送输入前重新检查
文档、目标描述、可见性、可用性、遮挡及短暂位置稳定性；输入还检查可编辑性。
只露出一部分的目标使用经过命中测试的可见区域坐标。

- STALE_OBSERVATION：文档或目标描述变化，本次未发送输入，重新 observe。
- PAGE_CONDITION_TIMEOUT：条件超时，本次未发送输入，先检查页面。
- outcome_unknown：操作可能发生，不自动重放，先读取结果。
- snapshot_warning：动作返回成功但附加摘要失败，不自动重放。

这些检查不能证明任意网站的异步事件监听器或业务数据都初始化完成，也无法消除
检查与输入之间任意脚本修改页面的竞态。登录、发布、支付等必须等待明确的业务
结果。没有添加动作自动重试，没有修改既有内核/CDP 输入派发方式。旧观察结果
不具备新文档标识时保留兼容行为；批量派发路径仍需调用者先等待所需目标。

## 验证

- `python -m unittest verify.test_page_readiness verify.test_browser_reliability verify.test_session_browser_scope installer.test_browser_startup installer.test_clean_startup`
- `node verify/test_environment_agent_plugin.cjs`
- `python verify/test_page_readiness_native.py`（Windows，本机开发内核）
- `python -m unittest verify.test_agent_execution_optimization`

原生测试启动私有桌面和一次性配置，仅访问本地测试网站，不切换用户桌面。
覆盖慢资源期间的单次可信点击、业务结果、延迟按钮、遮挡、移动、局部出屏、输入、
旧文档、只读字段、重定向、SPA。报告写入 `.work/page-readiness/<运行编号>/report.json`。
这是生产桥接/浏览器后端的集成验证，不是在线 DSH 模型的端到端耗时基准。

2026-11-03 的补充优化及前后对比见 `agent-execution-optimization-2026-11-03.md`。
