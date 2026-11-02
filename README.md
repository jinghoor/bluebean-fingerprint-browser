# 蓝豆指纹浏览器

蓝豆指纹浏览器是面向 Windows x64 的本地多环境浏览器。每个环境拥有独立的浏览器数据、账号、代理和指纹设置；环境与账号资料保存在本机。

此仓库提供蓝豆原创应用代码、DSH 插件、Chromium 修改补丁、Windows 启动器和打包脚本。Chromium 内核源码及完整构建树不在本仓库中。安装包见 [GitHub Releases](https://github.com/jinghoor/bluebean-fingerprint-browser/releases)。

## 许可

蓝豆原创部分的许可详情见 [LICENSE](LICENSE)；第三方组件的许可见[第三方声明](THIRD_PARTY_NOTICES.md)。

商业用途需事先取得书面授权，联系邮箱：**277030704@qq.com**。

## 安装

1. 从 Releases 下载 `BlueBean-Setup-1.0.4-Windows-x64.exe`。
2. 运行安装程序并按向导完成安装。
3. 从桌面快捷方式、开始菜单或安装目录中的 `BlueBean.exe` 启动。

安装包为 Windows 10 2004（19041）及以上的 x64 版本。浏览器、DSH、Python 和 Node 运行时均随安装包附带。模型服务、代理和网站账号仍需使用者自行配置。

当前版本为 **1.0.4**。安装、沙箱渲染、运行时、DSH 初始化、插件和视频回归的检查结果见 [1.0.4 发布测试说明](docs/RELEASE-1.0.4-TESTS.md)。卸载会保留用户数据。

## 从源码构建

仓库不含 Chromium 内核源码。构建内核需要单独取得 Chromium 158.0.8122.0 源码和 Windows 构建工具，并按 Chromium 要求保留第三方声明。复制 `chromium-src.example.json` 为未跟踪的 `chromium-src.json`，填写本机源码路径，并把源码目录连接到仓库的 `chromium` 路径。运行 `patches/manage.ps1 -Action restore` 应用补丁，再按 Chromium 官方文档生成 Release 构建。

应用安装包需要 Windows x64、Python 3.12、`requirements-build.txt` 所列 Python 依赖、一个本机 DSH 安装和 Inno Setup 7.1.1。设置 `BLUEBEAN_ISCC` 指向 `ISCC.exe`，然后运行：

```powershell
python build-installer.py
```

产品版本以 `bluebean_version.py` 为准，打包时写入安装器和原生启动器；DSH 菜单显示的版本也会在插件同步时更新。内核编译须保留 `proprietary_codecs=true` 和 `ffmpeg_branding="Chrome"`，构建示例见 `dev-args.gn` 与 `setup-chromium-build.ps1`；组件自己的许可和声明仍须保留。

构建结果写入 `build/installers`。独立安装验证脚本见 `installer/verify_install.py`。自动验收需要使用一次性测试目录和独立数据目录。

本地交付前须纠正安装 EXE 可能继承的 Low Integrity 标签，构建脚本已内置此检查。
如果又将文件复制到另一个交付目录，最后运行
`python installer/windows_security.py "安装包的完整路径"` 再验证最终文件。
详细说明见 [安装器说明](installer/README.md)。1.0.1 同时移除了安装收尾对隐藏控制台命令的依赖。

安装器首先选择随机命名、仅当前用户/管理员/SYSTEM 可访问的可写临时目录，验证内嵌安装包的 SHA-256 后启动安装向导。它只为自身进程设置 TEMP/TMP，不改变系统设置或系统目录权限。系统临时目录不可用时，会尝试用户本地数据目录和安装包所在目录；至少一个候选目录必须允许当前用户写入。

## 数据和安全

登录资料、Cookie、代理密码、API Key 和 DSH 会话属于本机用户数据，不应提交到版本库。应用默认把用户数据放在 `%LOCALAPPDATA%\BlueBean Fingerprint Browser\Data`。公开提交前请检查配置文件、日志、测试目录和构建产物。

发现安全问题请勿在公开 issue 中附上令牌、密码、Cookie 或可复现的真实账号数据。
