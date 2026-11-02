# 开发版稳定性与速度优化（2026-11-02）

## 改动

- `fp_proxy.py`：SOCKS 认证按协议读取完整回复，兼容 TCP 分片；拒绝未提供的认证方式及非法 CONNECT 回复；回复尾部增加读取超时。
- TCP 转发保留单向 EOF，等待反向响应结束，避免请求结束后提前关闭连接导致响应截断。TLS 上游仍保持证书验证，采用 TLS 支持的完整关闭方式。
- 转发失败或取消时，清理双向传输任务及上游连接；停止中继时关闭监听、取消并等待客户端任务，再关闭事件循环。删除每次连接同步输出的调试日志。
- `lan_seats/lib/client.js`：完整环境目录由约 3 秒一次改为正常空闲时 15 秒一次；后台隐藏时暂停，恢复显示立即刷新；连接中 2 秒重试，失败退避到 30 秒。保存、编辑、导入等操作仍立即更新；共享的轻量状态通道保持原有逻辑。

## 实测

所有测试使用本机回环代理、临时数据和无头浏览器，不修改用户环境、账号或代理配置。

| 检查 | 优化前 | 优化后 |
| --- | --- | --- |
| 分片 SOCKS 认证回复 | 连接失败 | 通过 |
| HTTP 请求半关闭后的延迟响应 | 1 MiB 响应收到 0 字节 | 完整且逐字节一致 |
| 上游返回未提供的认证方式 | 错误接受 | 拒绝 |
| 16 秒空闲窗口中的完整目录请求 | 6 次 | 1 次 |
| 16 秒隐藏窗口中的完整目录请求 | 1 次 | 0 次 |
| 页面恢复显示 | 等待下一轮轮询 | 立即刷新 |

- 中继 7 项回归通过，包括 16 路并发、每路超过 512 KiB 的内容校验，20 次启停与监听释放，以及活动隧道和未完成握手的退出清理；未发现遗留任务或资源警告。
- 环境状态及依赖范围测试 66 项通过，保留后台运行、代理异常与 Agent 并发状态规则。
- 真实 DSH 外壳中的目录同步 6 项通过，包括迟到响应不能覆盖新环境、改名、账号同步、相同内容保留 DOM 和焦点、删除同步；未调用代理检测。
- H.264/AAC、720p、MSE 音视频分轨、VP9/Opus 的默认与软件解码共 10 项播放测试通过。

这是请求次数和已复现故障的改善，不代表互联网带宽或所有网站加载速度的倍数提升。通过 Agent 或其他页面进行的外部目录修改，前台目录正常情况下最迟下一轮 15 秒刷新时显示。

## 生效方式

仅更新源码与开发版持久插件，无需重编译 Chromium。已打开的 DSH 页面需正常重新加载插件；已运行的 Python 中继需在下次正常重启对应服务时加载新代码。没有强制刷新用户页面或停止用户浏览器及中继。未更新已安装产品、未打包、未上传 GitHub。

## 验证与记录

- `python -B -m unittest verify.test_proxy_relay_stability -v`
- `python -B -m unittest verify.test_environment_status -q`
- `node verify/test_environment_catalog_polling.cjs`
- `node verify/test_environment_sidebar_catalog_ui.cjs`
- `node verify/test_media_playback.cjs --report decode-stability-speed.json`
- 基线与刷新计数：`.work/relay-stability-before.txt`、`.work/stability-speed/polling-before.json`、`.work/stability-speed/polling-after.json`。
- 修改前源码备份：`.work/stability-speed/fp_proxy.before.py`、`.work/stability-speed/client.before.js`。
