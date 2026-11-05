"""Browser-mediated OpenSSH askpass, scoped to individual local SSH processes."""
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socketserver
import subprocess
import sys
import threading
import time
import zipfile

from local_runtime import WINDOWS, background_options


class AuthError(RuntimeError):
    pass


class AuthCancelled(AuthError):
    pass


def build_helper(folder):
    folder = Path(folder)
    helper_source = Path(__file__).with_name("ssh_askpass.py").resolve()
    if WINDOWS:
        try:
            from pip._vendor.distlib.scripts import ScriptMaker
            # Use pip's standard native entry-point launcher; never put an
            # answer or authentication token in an executable or script file.
            launcher = ScriptMaker(None, str(folder))._get_launcher("w")
        except (ImportError, ValueError) as exc:
            raise AuthError("Windows 认证助手需要 pip；请重新运行 setup-windows.cmd") from exc
        code = "import sys\nsys.path.insert(0, %r)\nfrom ssh_askpass import main\nsys.exit(main())\n" % str(helper_source.parent)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("__main__.py", code)
        path = folder / "ssh-askpass.exe"
        path.write_bytes(launcher + ('#!"%s"\n' % sys.executable).encode("utf-8") + stream.getvalue())
    else:
        path = folder / "ssh-askpass"
        path.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(str(helper_source))), encoding="utf-8")
        path.chmod(0o700)
    return str(path)


class AuthOperation:
    def __init__(self, manager, profile, purpose, stop=None):
        self.manager, self.profile, self.purpose = manager, dict(profile), purpose
        self.stop = stop
        self.token = secrets.token_hex(32)
        self.cancelled = threading.Event()
        self.reason = "SSH 认证已取消"
        self.waiting = 0
        self.wait_started = None
        self.wait_total = 0
        self.prompt_count = 0
        self.diagnostic = b""
        self.process = None

    def attach(self, process):
        with self.manager.lock:
            self.process = process
            if self.cancelled.is_set() and process.poll() is None:
                process.terminate()

    def check(self):
        if self.stop and self.stop.is_set():
            self.cancel("操作已暂停")
        if self.cancelled.is_set():
            raise AuthCancelled(self.reason)

    def clock(self):
        self.check()
        with self.manager.lock:
            now = time.monotonic()
            return now - self.wait_total - (now-self.wait_started if self.wait_started is not None else 0)

    def note(self, chunk):
        with self.manager.lock:
            self.diagnostic = (self.diagnostic + chunk)[-4096:]

    def env(self):
        env = dict(os.environ)
        env.update(SSH_ASKPASS=self.manager.helper, SSH_ASKPASS_REQUIRE="force",
                   SCREENDESK_AUTH_PORT=str(self.manager.server.server_address[1]), SCREENDESK_AUTH_TOKEN=self.token)
        # DISPLAY supports older OpenSSH versions; no X server is accessed by
        # our askpass helper. The variables belong only to this SSH child.
        env["DISPLAY"] = env.get("DISPLAY") or "screendesk:0"
        return env

    def cancel(self, reason="SSH 认证已取消"):
        with self.manager.lock:
            self.reason = reason
            # Stop the SSH client before releasing its askpass helper. Some
            # clients otherwise treat askpass cancellation as an empty password
            # and submit another authentication attempt.
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                except OSError:
                    pass
            self.cancelled.set()
            for prompt in self.manager.prompts.values():
                if prompt["operation"] is self:
                    prompt["event"].set()

    def close(self):
        self.cancel("SSH 连接已结束")
        with self.manager.lock:
            self.manager.operations.pop(self.token, None)


class AuthManager:
    def __init__(self, folder, prompt_timeout=180):
        self.folder, self.prompt_timeout = Path(folder), prompt_timeout
        self.lock = threading.RLock()
        self.operations, self.prompts = {}, {}
        self.server = None
        self.helper = None
        self.closed = False

    def begin(self, profile, purpose, stop=None):
        with self.lock:
            if self.closed:
                raise AuthCancelled("程序正在退出")
            if len(self.operations) >= 64:
                raise AuthError("SSH 操作过多，请稍后重试")
            if self.server is None:
                manager = self
                class Handler(socketserver.StreamRequestHandler):
                    def handle(self):
                        try:
                            self.connection.settimeout(3)
                            raw = self.rfile.readline(16385)
                            if len(raw) > 16384:
                                return
                            request = json.loads(raw)
                            response = manager.request(request)
                            self.wfile.write(json.dumps(response).encode("utf-8") + b"\n")
                        except (OSError, ValueError, TypeError, AuthError):
                            pass
                class Broker(socketserver.ThreadingTCPServer):
                    daemon_threads = True
                    allow_reuse_address = True
                self.helper = build_helper(self.folder)
                self.server = Broker(("127.0.0.1", 0), Handler)
                threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.1), daemon=True).start()
            operation = AuthOperation(self, profile, purpose, stop)
            self.operations[operation.token] = operation
            return operation

    def request(self, request):
        if not isinstance(request, dict):
            return dict(ok=False)
        token, message = request.get("token"), request.get("prompt")
        if not isinstance(token, str) or not isinstance(message, str) or not message or len(message) > 4096:
            return dict(ok=False)
        with self.lock:
            operation = self.operations.get(token)
            if not operation or not hmac.compare_digest(token, operation.token):
                return dict(ok=False)
            operation.check()
            if len(self.prompts) >= 64 or operation.prompt_count >= 12:
                operation.cancel("SSH 认证次数过多，请检查认证方式后重试")
                return dict(ok=False)
            identifier = secrets.token_hex(16)
            confirmation = ("yes/no" in message.lower() or
                            request.get("hint") == "confirm" and not re.search(r"password|passphrase|code|token", message, re.I))
            prompt = dict(id=identifier, operation=operation, message=message,
                          kind="confirm" if confirmation else "secret", event=threading.Event(), answer=None, answered=False)
            self.prompts[identifier] = prompt
            operation.prompt_count += 1
            if operation.waiting == 0:
                operation.wait_started = time.monotonic()
            operation.waiting += 1
        try:
            deadline = time.monotonic() + self.prompt_timeout
            while not prompt["event"].wait(.1):
                operation.check()
                if time.monotonic() >= deadline:
                    operation.cancel("SSH 认证等待超时，请重新操作")
            operation.check()
            return dict(ok=True, answer=prompt["answer"]) if prompt["answered"] else dict(ok=False)
        except AuthError:
            return dict(ok=False)
        finally:
            with self.lock:
                prompt["answer"] = None
                self.prompts.pop(identifier, None)
                operation.waiting -= 1
                if operation.waiting == 0:
                    operation.wait_total += time.monotonic()-operation.wait_started
                    operation.wait_started = None

    def snapshot(self):
        with self.lock:
            return [dict(id=p["id"], profile_id=p["operation"].profile.get("id"),
                         server=p["operation"].profile.get("name", ""), host=p["operation"].profile.get("host", ""),
                         purpose=p["operation"].purpose, prompt=p["message"], kind=p["kind"],
                         details=p["operation"].diagnostic.decode("utf-8", "replace"))
                    for p in self.prompts.values() if not p["answered"] and not p["operation"].cancelled.is_set()]

    def answer(self, identifier, answer=None, cancel=False):
        with self.lock:
            prompt = self.prompts.get(identifier)
            if not prompt or prompt["answered"] or prompt["operation"].cancelled.is_set():
                raise AuthError("认证请求已结束，请重新操作")
            if cancel:
                prompt["operation"].cancel()
                return
            try:
                valid = isinstance(answer, str) and len(answer.encode("utf-8")) <= 1000 and not any(c in answer for c in "\r\n\0")
            except UnicodeError:
                valid = False
            if not valid:
                raise AuthError("认证输入无效")
            if prompt["kind"] == "confirm" and answer != "yes":
                raise AuthError("请明确确认或取消 SSH 连接")
            prompt.update(answer=answer, answered=True)
            prompt["event"].set()

    def cancel_profile(self, identifier):
        with self.lock:
            for operation in self.operations.values():
                if operation.profile.get("id") == identifier:
                    operation.cancel("服务器连接已关闭")

    def run(self, args, profile, purpose, timeout):
        operation = self.begin(profile, purpose)
        process, reader = None, None
        output = bytearray()
        def read_output():
            try:
                while True:
                    chunk = os.read(process.stdout.fileno(), 32768)
                    if not chunk:
                        return
                    operation.note(chunk)
                    if len(output) + len(chunk) > 2*1024*1024:
                        operation.cancel("SSH 操作输出过多，已停止等待")
                        return
                    output.extend(chunk)
            except (OSError, ValueError):
                return
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       env=operation.env(), **background_options(), **({"start_new_session": True} if not WINDOWS else {}))
            operation.attach(process)
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            started = operation.clock()
            while process.poll() is None:
                operation.check()
                if operation.clock()-started >= timeout:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    process.wait(timeout=.1)
                except subprocess.TimeoutExpired:
                    pass
            reader.join(timeout=2)
            operation.check()
            return subprocess.CompletedProcess(args, process.returncode, bytes(output))
        finally:
            try:
                if process:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=3)
                    if reader:
                        reader.join(timeout=1)
                    process.stdout.close()
            finally:
                operation.close()

    def close(self):
        with self.lock:
            self.closed = True
            for operation in self.operations.values():
                operation.cancel("程序正在退出")
            server = self.server
        if server:
            server.shutdown()
            server.server_close()
