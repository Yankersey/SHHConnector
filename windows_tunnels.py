"""One native OpenSSH forwarding connection per server, without ControlMaster.

The user's SSH config defines the forwards. Authentication goes through the
requesting terminal; neither credentials nor SSH config are saved or rewritten.
"""
import os
import secrets
import threading
import time

from local_runtime import open_terminal


class Tunnel:
    def __init__(self, workspace, profile, owner):
        self.workspace, self.profile, self.owner = workspace, dict(profile), owner
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.transport = None
        self.error = None
        self.lock = threading.Lock()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        marker = ("__SD_TUNNEL_" + secrets.token_hex(16) + "__").encode()
        pending = b""
        try:
            args = self.workspace.ssh_args(self.profile, forwarding=True)
            # A foreground remote process holds the SSH transport open. No
            # screen is created and no command from the SSH config is executed.
            command = "printf '%s'; exec cat" % marker.decode("ascii")
            env = dict(os.environ, TERM="xterm-256color")
            env.pop("SSH_ASKPASS", None)
            env.pop("SSH_ASKPASS_REQUIRE", None)
            transport = open_terminal(args + [command], self.owner.cols, self.owner.rows, env)
            with self.lock:
                self.transport = transport
                if self.closed.is_set():
                    return
            with self.owner.lock:
                if not self.owner.stopped:
                    self.owner.auth_transport = transport
            while not self.closed.is_set():
                chunk = transport.read()
                if chunk == b"":
                    raise OSError("共享 SSH 端口转发连接已断开，请重新连接启用转发的窗口")
                if chunk is None:
                    if transport.process.poll() is not None:
                        raise OSError("SSH 端口转发连接未成功，请检查终端中的登录或端口占用提示")
                    continue
                if not self.ready.is_set():
                    pending += chunk
                    position = pending.find(marker)
                    if position >= 0:
                        output = pending[:position] + pending[position + len(marker):]
                        pending = b""
                        with self.owner.lock:
                            self.owner.auth_transport = None
                        self.ready.set()
                    else:
                        # Keep only a possible partial marker, so password and
                        # MFA prompts remain visible immediately.
                        keep = 0
                        for n in range(min(len(marker)-1, len(pending)), 0, -1):
                            if marker.startswith(pending[-n:]):
                                keep = n
                                break
                        output = pending[:-keep] if keep else pending
                        pending = pending[-keep:] if keep else b""
                    if output and not self.owner.stopped:
                        self.owner.output(output)
        except Exception as exc:
            self.error = str(exc)
            if self.ready.is_set() and not self.closed.is_set() and not self.owner.stopped:
                self.owner.event(type="error", message=self.error)
        finally:
            self.close()

    def close(self):
        self.closed.set()
        with self.lock:
            transport, self.transport = self.transport, None
        with self.owner.lock:
            if self.owner.auth_transport is transport:
                self.owner.auth_transport = None
        if transport:
            try:
                transport.close()
            except OSError:
                pass


class NativeTunnels:
    def __init__(self, workspace):
        self.workspace = workspace
        self.lock = threading.Lock()
        self.tunnels = {}
        self.closing = False

    def ensure(self, profile, terminal):
        key = self.workspace.connection_key(profile)
        with self.lock:
            if self.closing:
                raise OSError("程序正在退出")
            tunnel = self.tunnels.get(key)
            created = tunnel is None or tunnel.closed.is_set()
            if created:
                tunnel = Tunnel(self.workspace, profile, terminal)
                self.tunnels[key] = tunnel
                tunnel.start()
        deadline = time.monotonic() + 180
        try:
            while not tunnel.ready.wait(.1):
                if tunnel.closed.is_set():
                    raise OSError(tunnel.error or "SSH 端口转发连接已关闭")
                if terminal.stopped:
                    raise OSError("终端已分离")
                if time.monotonic() >= deadline:
                    raise TimeoutError("SSH 端口转发登录超时，请重新连接")
            if tunnel.closed.is_set():
                raise OSError(tunnel.error or "SSH 端口转发连接已关闭")
        except Exception:
            if created:
                tunnel.close()
            raise

    def close_profile(self, profile):
        key = self.workspace.connection_key(profile)
        with self.lock:
            tunnel = self.tunnels.pop(key, None)
        if tunnel:
            tunnel.close()

    def close(self):
        with self.lock:
            self.closing = True
            tunnels = list(self.tunnels.values())
            self.tunnels.clear()
        for tunnel in tunnels:
            tunnel.close()
