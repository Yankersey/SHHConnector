"""Local OS integration; remote SSH commands remain POSIX on every platform."""
import os
from pathlib import Path
import select
import shutil
import signal
import struct
import subprocess
import sys
import time

WINDOWS = os.name == "nt"


def system_ssh():
    if WINDOWS:
        native = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/OpenSSH/ssh.exe"
        if native.is_file():
            return str(native)
    return shutil.which("ssh") or "ssh"


def background_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}


def try_lock(fd):
    """Acquire the same nonblocking process lock in the app and launcher."""
    if WINDOWS:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (13, 11, 36):
                return False
            raise
    else:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


class PosixTerminal:
    def __init__(self, args, cols, rows, env):
        import pty
        self.fd, slave = pty.openpty()
        try:
            self.resize(cols, rows)
            os.set_blocking(self.fd, False)
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("pty_child.py"))] + args,
                stdin=slave, stdout=slave, stderr=slave, env=env,
                start_new_session=True, close_fds=True)
        except Exception:
            os.close(self.fd)
            raise
        finally:
            os.close(slave)

    def read(self, timeout=.2):
        if not select.select([self.fd], [], [], timeout)[0]:
            return None
        try:
            return os.read(self.fd, 32768)
        except BlockingIOError:
            return None
        except OSError:
            return b""

    def write(self, value):
        data = memoryview(value.encode("utf-8"))
        deadline = time.monotonic() + 2
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                raise TimeoutError("终端输入暂时繁忙，请稍后重试")
            try:
                data = data[os.write(self.fd, data[:4096]):]
            except BlockingIOError:
                continue

    def resize(self, cols, rows):
        import fcntl
        import termios
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def close(self):
        os.close(self.fd)
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGHUP)
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)


class WindowsTerminal:
    def __init__(self, args, cols, rows, env):
        try:
            from winpty import Backend, PtyProcess
        except ImportError as exc:
            raise RuntimeError("Windows 原生模式需要 pywinpty，请运行 python -m pip install -r requirements-windows.txt") from exc
        # The socket exposes raw UTF-8 output, preserving partial code points.
        # PtyProcess.read decodes each chunk and can block on a split code point.
        self.pty = PtyProcess.spawn(args, dimensions=(rows, cols), env=env, backend=Backend.ConPTY)
        self.process = self
        self.pid = self.pty.pid
        self.closed = False

    def poll(self):
        return None if self.pty.pty.isalive() else self.pty.pty.get_exitstatus()

    def read(self, timeout=.2):
        if not select.select([self.pty.fileobj], [], [], timeout)[0]:
            return None
        return self.pty.fileobj.recv(32768)

    def write(self, value):
        # ConPTY accepts Unicode rather than POSIX byte-oriented input.
        self.pty.write(value)

    def resize(self, cols, rows):
        self.pty.setwinsize(rows, cols)

    def close(self):
        if self.closed:
            return
        self.closed = True
        # Terminate only our local ssh child, never its remote screen session.
        try:
            if self.poll() is None:
                os.kill(self.pid, signal.SIGTERM)
        finally:
            self.pty.fileobj.close()
            self.pty._server.close()
            self.pty.closed = True


def open_terminal(args, cols, rows, env):
    return (WindowsTerminal if WINDOWS else PosixTerminal)(args, cols, rows, env)
