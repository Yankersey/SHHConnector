# 修改与同步

仓库：https://github.com/Yankersey/SHHConnector

在其中一台电脑的项目目录修改代码。每次开始修改前先退出正在运行的程序，拉取最新代码：

```sh
git pull --ff-only
```

让编码助手在这个仓库目录中工作，然后运行测试。开发时用 `sh start.sh --offline`，避免未提交修改阻止启动。

改完后提交并推送：

```sh
git add app.py static/ tests/  # 换成这次实际修改的文件
git commit -m "Describe the change"
git push
```

另一台电脑退出旧程序后再运行启动器，会拉取 origin/main 的新提交。两台电脑不应同时修改同一分支并假定 Git 会自动解决冲突；本地有修改、未推送提交或分支分叉时，启动器会保留它们并停止自动更新。

发布前检查 GitHub Actions 的 Tests 流程。默认对每次推送 / PR 在 Ubuntu + Python 3.9 / 3.12 运行集成测试。

## 本机数据

以下内容已在 .gitignore 中排除：data/、downloads/、缓存、日志、.ssh/、.env、*.pem、*.key。

不要把自己的 SSH config、私钥、token 或服务器数据写进源码、测试或示例。需要改配置时，编辑电脑自己的 ~/.ssh/config。

## 测试

macOS 已有 screen 与系统 SFTP server。Ubuntu / WSL 可安装测试服务：

```sh
sudo apt install screen openssh-sftp-server
SCREENDESK_TEST_SCREEN=1 PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

安装 Node 的开发环境还可以运行：

```sh
node tests/test_mouse.mjs
```

测试使用本机临时目录 / socket，不登录用户的真实服务器。
