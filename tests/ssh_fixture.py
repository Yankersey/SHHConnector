"""Disposable loopback SSH/SFTP server; never reads user SSH configuration."""
from pathlib import Path
import logging
import re
import socket
import threading

import paramiko

PASSWORD = "fixture-password-123"
CODE = "123456"


class FileHandle(paramiko.SFTPHandle):
    def stat(self):
        import os
        return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))


class Files(paramiko.SFTPServerInterface):
    def __init__(self, server, root):
        super().__init__(server)
        self.root = Path(root).resolve()

    def local(self, path):
        candidate = (self.root/path.lstrip("/")).resolve()
        candidate.relative_to(self.root)
        return candidate

    def canonicalize(self, path):
        return "/" if path in ("", ".", "/") else "/"+str(self.local(path).relative_to(self.root)).replace("\\", "/")

    def list_folder(self, path):
        result = []
        for child in self.local(path).iterdir():
            attrs = paramiko.SFTPAttributes.from_stat(child.stat())
            attrs.filename = child.name
            result.append(attrs)
        return result

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.local(path).stat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            handle = FileHandle(flags)
            handle.readfile = self.local(path).open("rb")
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class SSHFixture:
    def __init__(self, root, mfa=False):
        self.root, self.mfa = Path(root), mfa
        self.key = paramiko.RSAKey.generate(2048)
        self.stop = threading.Event()
        self.transports, self.threads = [], []
        self.password_attempts = 0
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(.1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                connection, unused = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self.client, args=(connection,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def client(self, connection):
        fixture = self
        class Server(paramiko.ServerInterface):
            verified = False
            def get_allowed_auths(self, username):
                return "keyboard-interactive" if self.verified and fixture.mfa else "password"
            def check_auth_password(self, username, password):
                fixture.password_attempts += 1
                if username == "fixture" and password == PASSWORD:
                    self.verified = True
                    return paramiko.AUTH_PARTIALLY_SUCCESSFUL if fixture.mfa else paramiko.AUTH_SUCCESSFUL
                return paramiko.AUTH_FAILED
            def check_auth_interactive(self, username, submethods):
                if self.verified:
                    return paramiko.InteractiveQuery("Fixture MFA", "Local test only", ("Verification code: ", False))
                return paramiko.AUTH_FAILED
            def check_auth_interactive_response(self, responses):
                return paramiko.AUTH_SUCCESSFUL if responses == [CODE] else paramiko.AUTH_FAILED
            def check_channel_request(self, kind, channelid):
                return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            def check_channel_exec_request(self, channel, command):
                def reply():
                    try:
                        match = re.search(rb"__SD_LIST_([0-9a-f]{32})_BEGIN__", command)
                        if match:
                            marker = match[1].decode()
                            data = "__SD_LIST_%s_BEGIN__\nThere is a screen on:\n\t123.sd2_模型训练_aaaaaaaaaaaa\t(Detached)\n__SD_LIST_%s_END__\n" % (marker, marker)
                            channel.sendall(data.encode("utf-8"))
                        else:
                            channel.sendall(b"fixture-command-ok\n")
                        channel.send_exit_status(0)
                    finally:
                        channel.close()
                threading.Thread(target=reply, daemon=True).start()
                return True
        transport = paramiko.Transport(connection)
        transport.set_log_channel("screendesk.fixture.ssh")
        logging.getLogger("screendesk.fixture.ssh").setLevel(logging.CRITICAL)
        self.transports.append(transport)
        transport.add_server_key(self.key)
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, Files, root=self.root)
        try:
            transport.start_server(server=Server())
            while transport.is_active() and not self.stop.wait(.05):
                pass
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def close(self):
        self.stop.set()
        self.listener.close()
        for transport in self.transports:
            transport.close()
        self.thread.join(timeout=2)
        for thread in self.threads:
            thread.join(timeout=2)
