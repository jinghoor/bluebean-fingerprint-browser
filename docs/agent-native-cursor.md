# Agent 原生指针可视化

## 目标

让 Agent 执行浏览器动作时，在浏览器原生合成层显示一个短暂的蓝色指针标记。网页仍由原始渲染器绘制，指针是浏览器外层的独立视图，不向页面注入 DOM、CSS 或脚本。

## 动作链路

1. `browser_backend.py` 在点击、悬停、聚焦、填写和滚动前发送一个轻量的 `cursor` 事件。
2. `AutomationServer` 在同一个本地鉴权通道接收事件，并按环境 ID 转交给当前浏览器窗口。
3. `EnvOverlayHost` 为每个浏览器窗口维护环境独立的最后位置。非当前环境的事件只缓存位置，切换环境时恢复对应位置。
4. `EnvAutomationCursorView` 在 UI 线程以 16ms 定时器完成 70–90ms 的平滑移动，并绘制点击扩散环、滚动方向线和阴影。
5. 指针视图禁止事件命中和焦点获取，因此不会拦截网页输入，也不会改变网页坐标或版式。

## 稳定性约束

- 指针事件是 best-effort。原生通道不可用时，浏览器动作继续执行，不因可视化失败而重放或中断动作。
- 通道只接受 loopback 地址和启动时生成的令牌，事件不包含账号密码或页面内容。
- 环境和浏览器窗口由 profile 绑定。多环境、多窗口和切换标签页不会共享指针位置。
- 滚动动作使用原生 `mouseWheel` 输入，默认滚动 640 DIP，可由 Agent 指定坐标和横纵向增量。

## 验证

- `verify/test_cursor_events.py` 检查桥接层在通道可用与不可用时的行为。
- `verify/test_cursor_native.py` 在一次性 profile 和独立 Windows desktop 中启动实际 Chromium，验证移动、点击、填写、滚动请求均被原生 socket 接收，并产生 compositor 侧 `ENVCURSOR` trace。
