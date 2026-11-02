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

首次使用的“我的环境”必须为空，只有用户主动新建或导入才增加环境。所有启动入口均不再
自动生成开发演示环境，也不根据 `fb-008` 等名字清理现有数据；升级时保留用户已有的环境、
账号、代理和网站数据。

构建在调用安装器编译器之前执行 `installer/payload_policy.py`：拒绝打入环境定义、账号库、
Cookie、浏览器配置、DSH 用户 profile、开发凭据，以及仍会自动创建演示环境的旧启动脚本。
检查只读，不会为了使检查通过而删除数据。安装验收也执行相同检查，首次使用的真实产品
桌面验收另行断言环境列表为空，再显式新建验收环境并确认其能跨重启保留。

启动器会先建立空的 `BrowserProfile/Default` 目录，再启动检测与桥接服务。该目录准备不创建
任何环境，作用是让所有组件从第一次启动起就使用同一条配置与命令通道路径，避免服务启动
早于 Chromium 建立目录时，把命令或账号库写到上一级。

可用 `python installer/payload_policy.py "X:/release/App"` 单独检查待发布应用目录。
已经生成的旧 EXE 不会因修改开发源码而自动更新，需要重新生成安装包后才包含此修复。

## 构建

在开发机准备当前 Chromium 构建、Python 运行时和上文所列依赖以及 Inno Setup。
运行 `python build-installer.py`。通过 `BLUEBEAN_ISCC` 可以指定 `ISCC.exe`，默认位置为
`build/tools/InnoSetup/ISCC.exe`。构建结果保存在带时间戳的 `build/installers` 目录。

当前有效的安装器实现为 `BlueBean.iss`；不使用旧的 IExpress/PowerShell 安装草案。

最终 Setup EXE 使用 Inno Setup 安装器前的原生临时目录引导程序，并将最终交付 EXE 的完整性级别设为 Medium。构建目录可能由受限构建环境创建为 Low Integrity；若不在最终产物上纠正此安全标签，Windows Explorer 启动时也会把安装器进程降到 Low Integrity，导致它无法写入正常的 per-user 安装位置。发布前应从 Explorer 的同级完整性运行安装验收，至少确认进程不是 Low Integrity。

复制 EXE 到另一个本地交付文件夹后，该文件可能再次继承目标目录的 Low 标签。
因此最后一次复制完成后必须执行，并按 SID 验证，不能只检查构建目录中的原件：

```powershell
python installer/windows_security.py "X:/delivery/BlueBean-Setup-Windows-x64.exe"
```

该操作只纠正指定安装 EXE 的完整性标签，不修改系统目录权限、不要求产品安装提权，
也不改变 EXE 内容或 SHA-256。NTFS 安全标签不包含在上传的 EXE 字节中。
启动层若发现自己仍在 Low Integrity 下运行，会在解压之前退出并给出明确提示
（静默模式退出码 13），避免继续弹出误导性的 TEMP 或安装目录错误。

安装收尾通过无控制台的原生 `BlueBean-Permissions.exe` 为当前安装目录补充浏览器
沙箱所需的读取/执行权限。它保留原有 DACL，只合并 ALL APPLICATION PACKAGES 和
ALL RESTRICTED APPLICATION PACKAGES 的继承项，不启动 `icacls` 或控制台宿主。
配置失败时安装器返回 20；验收另行检查实际文件权限，不仅依赖安装器退出码。

## 无界面验收

在独立目录安装并设置独立测试数据后运行：

```powershell
python installer/verify_install.py --app "X:/test/App" --payload "X:/release/App" --data "X:/test/Data" --report "X:/test/acceptance.json"
```

脚本核对所有安装文件的 SHA-256，验证 EXE 内嵌图标、自带 Python 及延迟加载的
依赖、开启沙箱的 Chromium 页面渲染、DSH 服务，以及带空格路径下的原生 EXE
到 pythonw 启动链路。它使用无界面浏览器，不打开产品窗口，不访问真实用户数据。
原生启动链路在独立夹具中使用无害入口脚本，不把这一项当作完整图形界面验收。

隔离测试可使用 `/ACCEPTANCETEST=1`，避免改动使用者的桌面和系统应用登记。
此参数仅跳过快捷方式、卸载注册表项创建，安装文件和沙箱权限流程照常执行。
因此自动测试不等同于在全新电脑上完成完整安装向导、系统登记和 UI 验收。

正常安装回归应另外在可恢复的测试条件下，不传入此参数，以默认安装目录运行，
并传入 `--require-uninstall-registration` 验证安装登记和两个快捷方式的真实目标及图标。
应先备份已有快捷方式，完成卸载后恢复。完整性检查直接查询 Windows SID，
不依赖操作系统的中英文输出。运行时检查始终使用独立测试数据目录。

最终交付目录中的测试报告记录具体检查结果和验证边界。
