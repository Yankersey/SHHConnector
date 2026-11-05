#!/usr/bin/env python3
"""Check the tracked Git revision before importing and starting the application."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


class UpdateError(Exception):
    pass


def git(root, *args, check=True):
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_MERGE_AUTOEDIT="no")
    try:
        result = subprocess.run(["git", "-C", str(root)] + list(args), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=35, env=environment)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("Git 操作未完成：" + str(exc))
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1800:]
        raise UpdateError("Git 更新失败：" + detail)
    return result


def update_checkout(root=ROOT):
    root = Path(root).resolve()
    if not (root / ".git").exists():
        return "ZIP 版本，没有 Git 元数据；未检查远程更新"
    if not shutil.which("git"):
        raise UpdateError("找不到 Git。请安装 Git，或显式使用 --offline 启动本地版本")
    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    if upstream.returncode:
        if not git(root, "remote").stdout.strip():
            return "尚未设置远程仓库；按本地开发版本启动"
        raise UpdateError("当前分支没有跟踪远程分支。请先设置 upstream，或显式使用 --offline")
    status = git(root, "status", "--porcelain", "--untracked-files=normal").stdout
    if status.strip():
        raise UpdateError("项目中有未提交的代码修改，未自动覆盖。请提交 / 推送后重试；开发时可用 --offline")
    branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    remote = git(root, "config", "--get", "branch." + branch + ".remote").stdout.strip()
    if not remote or remote == ".":
        raise UpdateError("当前分支没有可检查的远程服务器")
    before = git(root, "rev-parse", "HEAD").stdout.strip()
    git(root, "fetch", "--no-tags", remote)
    target = git(root, "rev-parse", "@{upstream}").stdout.strip()
    local_only = int(git(root, "rev-list", "--count", "@{upstream}..HEAD").stdout.strip())
    if local_only:
        raise UpdateError("本地分支含未推送的提交，未自动合并。请先 git push / 处理分支差异，或用 --offline")
    git(root, "merge", "--ff-only", "@{upstream}")
    after = git(root, "rev-parse", "HEAD").stdout.strip()
    if after != target:
        raise UpdateError("更新后版本与远程分支不一致，请检查 Git 状态")
    return ("已更新 " + before[:8] + " → " + after[:8]) if before != after else "已是远程最新版 " + after[:8]


def current_build(root=ROOT):
    root = Path(root)
    try:
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = "development"
    if (root / ".git").exists() and shutil.which("git"):
        result = git(root, "rev-parse", "--short=8", "HEAD", check=False)
        if not result.returncode:
            version += " · " + result.stdout.strip()
            if git(root, "status", "--porcelain", check=False).stdout.strip():
                version += " · 本地修改"
    return version


def already_running(data_dir, no_browser=False):
    import fcntl
    lock = Path(data_dir) / "running.lock"
    if not lock.exists():
        return False
    with lock.open("r+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("ScreenDesk 正在运行；先退出旧程序，再启动才能更新。", flush=True)
            if not no_browser:
                try:
                    from platform_support import open_workspace
                    info = json.loads((Path(data_dir)/"running.json").read_text(encoding="utf-8"))
                    open_workspace(info["url"])
                except (OSError, KeyError, ValueError):
                    pass
            return True
    return False


def main(argv=None):
    if os.name != "posix":
        print("Windows 请运行 start-windows.cmd，或在 WSL / Ubuntu 中运行 sh start.sh。", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--update-only", action="store_true")
    parser.add_argument("--data-dir", default=str(ROOT/"data"))
    parser.add_argument("--no-browser", action="store_true")
    original = list(sys.argv[1:] if argv is None else argv)
    args, _ = parser.parse_known_args(original)
    if already_running(args.data_dir, args.no_browser):
        return 0
    try:
        if args.offline:
            print("[版本] 离线 / 开发模式，未检查远程最新版", flush=True)
        elif not any(flag in original for flag in ("--help", "-h")):
            print("[更新] 正在检查 Git 远程版本…", flush=True)
            print("[更新] " + update_checkout(), flush=True)
        if args.update_only:
            return 0
        os.environ["SCREENDESK_BUILD"] = current_build()
        # Import only after checkout update, so updated modules are loaded.
        import app
        sys.argv = [str(ROOT/"app.py")] + [value for value in original if value not in ("--offline", "--update-only")]
        return app.main()
    except UpdateError as exc:
        print(str(exc), file=sys.stderr)
        print("未启动旧代码。若现在要使用本地版本，请运行 sh start.sh --offline。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
