# 视频播放兼容性（2026-11-02）

## 根因和修复范围

开发构建曾沿用 Chromium 默认编解码配置：`USE_PROPRIETARY_CODECS=0`、
FFmpeg Chromium codec set。实际普通窗口和独立无头浏览器都报告：

- H.264 / AAC 的 `canPlayType()` 返回空字符串。
- H.264 / AAC 的 `MediaSource.isTypeSupported()` 返回 false。
- 本地 MP4 播放报 `DEMUXER_ERROR_NO_SUPPORTED_STREAMS`。
- MSE 创建 H.264 缓冲失败，VP9/Opus 同一环境正常播放。

这些能力缺失足以导致 B 站“不支持 HTML5”、抖音“浏览器版本过低”或
00:00 无法前进；这些文案本身不证明需要伪装版本号。

开发输出 `out/Release-pgo/args.gn`、`dev-args.gn` 和
`setup-chromium-build.ps1` 的生成流程统一设置：

```gn
proprietary_codecs = true
ffmpeg_branding = "Chrome"
```

`ffmpeg_branding` 在此选择 FFmpeg 编解码集合，不改变浏览器品牌、图标或 UA。
源树中对应配置包含 H.264 和 AAC 软件解码器。现有 GN 默认同时启用 HLS
解析和平台 HEVC 支持；HEVC 实际可用性仍取决于系统、设备与解码路径。
未伪造媒体支持检测结果，未移除代理或指纹隔离。

## 验证和构建

`verify/test_media_playback.cjs` 使用 Chromium 自带媒体样本和本地 HTTP 服务，
测试 H.264/AAC MP4、720p、MSE 合流、MSE 音视频独立轨、VP9/Opus。
成功标准包括时间轴超过 0.55 秒、有解码帧及可 seek，分别运行默认解码和
关闭硬件解码的场景。

`verify/test_media_native.py` 在独立 Windows desktop、一次性环境 profile 中
运行相同测试，不显示窗口、不发送全局输入。`--sites` 另测匿名公共视频网页；
网站或 CDN 网络失败单独记载，不与解码测试混淆。

```powershell
node verify/test_media_playback.cjs --baseline
node verify/test_media_playback.cjs --chrome .work/media-playback/browser/chrome.exe
python verify/test_media_native.py .work/media-playback/browser/chrome.exe --sites
```

原用户浏览器正在使用 `Release-pgo/chrome.dll`，因此构建先用独立 ninja
phony 目标编译其依赖，再由 `verify/build_media_runtime.py` 将新对象链接到
`.work/media-playback/browser`。不终止原用户浏览器。

验收记录、构建日志和网站截图位于 `.work/media-playback/`。本次不生成
安装包、不更新安装版、不上传 GitHub。当前已经运行的旧内核不能通过网页
热更新获得新的解码器，使用新构建必须重新启动浏览器。

测试脚本已先通过本机原版 Chrome 的 10 项对照测试（默认路径和强制软件解码
各 5 项），结果位于 `decode-reference.json`。这只验证测试方法，不代替新内核验收。

## 本机验收结果

- 新构建：默认解码和强制软件解码各 5 项全部通过，包含真实出帧、时间推进和 seek。
- 原生隔离环境：相同的 5 项通过，运行在私有 Windows desktop，未操作用户窗口。
- B 站截图中的同一视频：播放从 2.13 秒到 5.14 秒，57 帧到 132 帧，媒体 CDN 返回 206。
- 抖音截图中的具体视频：1011.234 秒的视频从 metadata 阶段推进至 2.734 秒，实际解码
  86 帧；预加载的另一条视频也取得 1280×720 画面。未再显示“浏览器版本过低”。
  新临时 profile 会出现抖音官方登录提示，未登录、未提交账号资料。
- 部署到原开发输出后，再次通过默认/软件解码的 10 项测试。

构建 DLL SHA256：`94e6e3e62cb6f4451848e9b765e3400459845df47925aa1f6c59f173fa50febe`。
旧 DLL 保留为 `chrome.pre-media-69584d02974c.dll`；原用户进程未终止，需正常退出重开才
整体使用新内核。

构建期间运行中的浏览器占用 `v8_context_snapshot.bin`。临时输出的新快照与旧文件
SHA256 完全一致，因此仅在生成的 Ninja 规则中使用“生成临时文件后比较、相同则保留”
的辅助步骤，避免覆写映射文件。编译完成后已用 `gn gen` 恢复标准生成规则，没有修改
上游快照生成器源码。
