"""Optional, explicitly enabled Cloudflare Quick Tunnel. Never opens a LAN socket."""
import hmac
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading

from local_runtime import background_options


class MobileAccessError(Exception):
    pass


def cloudflared_path():
    found = shutil.which("cloudflared")
    if found:
        return found
    for path in ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"):
        if Path(path).is_file():
            return path
    return None


class MobileAccess:
    def __init__(self, local_origin, revoked, startup_timeout=60):
        self.local_origin, self.revoked = local_origin, revoked
        self.startup_timeout = startup_timeout
        self.lock = threading.RLock()
        self.generation = 0
        self.process = None
        self.origin = self.token = None
        self.state, self.message = "off", "手机访问尚未开启"

    def snapshot(self):
        with self.lock:
            return dict(state=self.state, message=self.message, available=bool(cloudflared_path()),
                        url=self.origin + "/#token=" + self.token if self.origin and self.token else None)

    def accepts(self, token, origin=None):
        with self.lock:
            return bool(isinstance(token, str) and token.isascii() and self.token and self.origin and self.state in ("running", "reconnecting") and
                        origin in (None, self.origin) and hmac.compare_digest(token, self.token))

    def start(self):
        with self.lock:
            if self.state in ("starting", "running", "reconnecting"):
                return self.snapshot()
            binary = cloudflared_path()
            if not binary:
                raise MobileAccessError("电脑尚未安装 cloudflared。macOS 可用 brew install cloudflared；Windows 可用 winget install --id Cloudflare.cloudflared。手机无需安装。")
            self.generation += 1
            generation = self.generation
            self.token, self.origin = secrets.token_hex(32), None
            self.state, self.message = "starting", "正在建立手机访问入口…"
            # An empty, explicit config avoids touching any existing named tunnel.
            args = [binary, "tunnel", "--config", os.devnull, "--no-autoupdate",
                    "--url", self.local_origin, "--http-host-header", self.local_origin.split("//", 1)[1],
                    "--protocol", "http2", "--metrics", "127.0.0.1:0", "--loglevel", "info"]
            env = {k: v for k, v in os.environ.items() if not k.startswith(("TUNNEL_", "CLOUDFLARED_"))}
            try:
                self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, env=env, **background_options())
            except OSError as exc:
                self.state, self.token = "failed", None
                self.message = "手机访问启动失败：" + str(exc)
                raise MobileAccessError(self.message) from exc
            process = self.process
            timer = threading.Timer(self.startup_timeout, self._timeout, args=(generation,))
            timer.daemon = True
            timer.start()
            threading.Thread(target=self._watch, args=(process, generation, timer), daemon=True).start()
            return self.snapshot()

    def _timeout(self, generation):
        with self.lock:
            if self.generation != generation or self.state != "starting":
                return
        self.stop("手机访问连接超时。电脑上的 SSH 连接和远程任务均保留，可重新开启。", generation)

    def _watch(self, process, generation, timer):
        try:
            for raw in process.stdout:
                line = raw.decode("utf-8", "replace")
                with self.lock:
                    if self.generation != generation:
                        return
                    match = re.search(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com\b", line)
                    if match:
                        self.origin = match.group(0)
                    if "Registered tunnel connection" in line and self.origin:
                        self.state, self.message = "running", "手机可通过完整链接访问；电脑需保持程序运行且不睡眠。"
                        timer.cancel()
                    elif self.state == "running" and ("Unregistered tunnel connection" in line or "Connection terminated" in line):
                        self.state, self.message = "reconnecting", "外网入口正在重连；远程 screen 任务保留。"
            process.wait()
        finally:
            timer.cancel()
            process.stdout.close()
            with self.lock:
                current = self.generation == generation
            if current:
                self.stop("外网入口已断开。远程任务保留，请重新开启手机访问并使用新链接。", generation)

    def stop(self, message=None, generation=None):
        with self.lock:
            if generation is not None and self.generation != generation:
                return self.snapshot()
            self.generation += 1
            process, self.process = self.process, None
            self.token = self.origin = None  # Revoke before waiting for the subprocess.
            self.state = "failed" if message else "off"
            self.message = message or "手机访问已关闭；旧链接失效，远程任务继续运行。"
        self.revoked()
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        return self.snapshot()
