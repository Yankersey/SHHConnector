# Windows 使用说明

当前版本通过 WSL 运行 Python / SSH / PTY，界面使用 Windows 浏览器。不能直接用 Windows Python 运行 app.py。

## 1. 安装 WSL / Ubuntu

以管理员身份打开 PowerShell：

```powershell
wsl --install
```

按提示重启 Windows，第一次打开 Ubuntu 时设置 Linux 用户名和密码。已安装 WSL 的电脑可直接进入下一步。安装要求与选项见 [Microsoft WSL 安装说明](https://learn.microsoft.com/en-us/windows/wsl/install)。

## 2. 在 Ubuntu 里准备环境并克隆

下面的命令在 **Ubuntu / WSL 终端**运行：

```sh
sudo apt update
sudo apt install -y python3 openssh-client git
mkdir -p ~/code
cd ~/code
git clone https://github.com/Yankersey/SHHConnector.git
cd SHHConnector
```

程序没有 pip / npm 运行依赖。项目放在 WSL 的 Linux 文件系统中，符合 [Microsoft 的文件存放建议](https://learn.microsoft.com/en-us/windows/wsl/filesystems)。

## 3. 准备 WSL 的 SSH 配置

WSL 的 `~/.ssh` 与 Windows 的 `C:\Users\你的用户名\.ssh` 是两个目录。程序读取 **WSL 的 ~/.ssh/config**。

```sh
mkdir -p ~/.ssh
chmod 700 ~/.ssh
nano ~/.ssh/config
```

示例：

```sshconfig
Host my-server
    HostName 你的服务器地址
    User 你的服务器用户名
    Port 22
    IdentityFile ~/.ssh/id_ed25519
```

如果已经在 Windows 配好 SSH，可以自行把需要的 config / 私钥复制进 WSL 的 ~/.ssh，并设置私钥权限 `chmod 600 ~/.ssh/私钥文件名`。不需要把私钥放进项目。已有 `IdentityFile C:\...`、绝对 Include 路径、Windows 专用 ProxyCommand 时，需要改成 WSL 可用的路径 / 命令。

先用 `ssh my-server` 确认 WSL 自己能正常登录；如果目标服务器需要 GNU screen，先在服务器上安装它。

## 4. 启动

在项目目录：

```sh
sh start.sh
```

程序启动前会检查 Git 更新，再打印完整本机地址，并尝试打开 Windows 默认浏览器。没有自动打开时，把打印出的 **完整 http://127.0.0.1:端口/#token=... 地址**粘贴到 Windows 浏览器，不要省略 token。

Windows 浏览器可以访问 WSL 中的本地 Web 服务，见 [Microsoft WSL 网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)。程序仅绑定 127.0.0.1，无需把它开放到局域网。

也可在 Windows 资源管理器中进入项目目录，双击 **start-windows.cmd**；它会使用默认 WSL 发行版。项目位于 WSL 时，可在资源管理器地址栏输入 `\\wsl$`，进入 Ubuntu 的 home / 你的用户名 / code / SHHConnector。

## 5. 两台电脑保持同一代码版本

Mac 与 Windows / WSL 各自克隆同一仓库，保留默认 main 跟踪 origin/main。每次启动器会检查并只执行快进更新；界面左下角显示版本和提交号。遇到离线、未提交修改、未推送提交或分支分叉，会停止启动并说明原因，不偷偷覆盖代码或使用旧代码冒充最新版。

已运行的程序不会热更新。先从界面退出，再重新启动。

明确要用已有本地版本时：

```sh
sh start.sh --offline
```

或在 Windows 命令提示符运行 `start-windows.cmd --offline`。

只同步代码，不启动：`python3 launcher.py --update-only`。

SSH config、密钥、窗口布局、下载进度和下载文件各自留在电脑本机，不通过 Git 同步。远程 screen 在服务器上，可以在第二台电脑通过“连接已有 screen”接回；本机布局不会自动跨电脑复制。

下载文件在 WSL 项目目录的 downloads/，点“在本机显示”会尝试打开 Windows 资源管理器。

本仓库已提供 Ubuntu 的自动测试流程。Windows 的 WSL 启动入口需要在你的 Windows 电脑完成一次实际运行验证。
