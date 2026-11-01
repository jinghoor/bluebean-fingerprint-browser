# Windows 安装包

交付物为 `dist/BlueBean-Setup-Windows-x64.exe`，是带安装向导的普通 Windows 安装程序。

用户双击安装包，选择安装目录，完成安装后从桌面、开始菜单或安装目录的
`BlueBean.exe` 启动。用户无需安装 Python、Node.js、DSH、编译工具或 VC 开发环境。
适用 Windows 10 2004（19041）及以上的 x64 系统，包括 Windows 11。

默认安装到当前用户的 `%LOCALAPPDATA%\Programs\BlueBean Fingerprint Browser`，
无需管理员权限。安装向导默认创建桌面快捷方式，并添加开始菜单入口和 Windows
卸载项。正常安装会显示向导；自动验收使用静默参数，不改变正常安装方式。

## 包含的运行时

- 当前构建的 Chromium 及 GN 列出的运行时文件。
- DSH 应用及其 Electron/Node 运行时、蓝豆插件。
- 独立 Python 3.12、Tk、所需 VC 运行库、WebSocket、HTTP/SOCKS、加密、Zstandard、
  CA 证书和 IANA 时区数据。具体版本见安装目录 `runtime/bundled-packages.json`。
- 蓝豆透明 Logo、ICO 和启动提示组件。

启动入口为原生 Windows GUI 程序，直接使用同目录内的 `runtime/pythonw.exe`，
启用 Python 隔离模式，不搜索系统 Python。子进程启动时也会清除外部 Python 路径变量。

账户、环境、代理、浏览器配置、DSH 状态和日志位于当前 Windows 用户的
`%LOCALAPPDATA%\BlueBean Fingerprint Browser\Data`。安装包不包含开发机上的账户、
API Key、Cookie 或个人环境。卸载程序不删除此数据目录。需要联网的模型、网站和代理
仍需用户正常配置，运行时打包不代替这些服务。

## 构建

在开发机准备当前 Chromium 构建、Python 运行时和上文所列依赖以及 Inno Setup。
运行 `python build-installer.py`。通过 `BLUEBEAN_ISCC` 可以指定 `ISCC.exe`，默认位置为
`build/tools/InnoSetup/ISCC.exe`。构建结果保存在带时间戳的 `build/installers` 目录。

当前有效的安装器实现为 `BlueBean.iss`；不使用旧的 IExpress/PowerShell 安装草案。

## 无界面验收

在独立目录安装并设置独立测试数据后运行：

```powershell
python installer/verify_install.py --app "X:/test/App" --payload "X:/release/App" --data "X:/test/Data" --report "X:/test/acceptance.json"
```

脚本核对所有安装文件的 SHA-256，验证 EXE 内嵌图标、自带 Python 及延迟加载的
依赖、开启沙箱的 Chromium 页面渲染、DSH 服务，以及带空格路径下的原生 EXE
到 pythonw 启动链路。它使用无界面浏览器，不打开产品窗口，不访问真实用户数据。
原生启动链路在独立夹具中使用无害入口脚本，不把这一项当作完整图形界面验收。

本机自动测试使用 `/ACCEPTANCETEST=1`，避免改动使用者的桌面和系统应用登记。
此参数仅跳过快捷方式、卸载注册表项创建，安装文件和沙箱权限流程照常执行。
因此自动测试不等同于在全新电脑上完成完整安装向导、系统登记和 UI 验收。

最终交付目录中的测试报告记录具体检查结果和验证边界。
