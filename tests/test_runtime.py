"""Local runtime tests: no real hosts, credentials, or user SSH configuration."""
import contextlib
import http.client
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import launcher
import local_runtime
import transfers
from pipe_io import TimedPipes
from ssh_config import read_profiles
from windows_tunnels import NativeTunnels

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("Condition timed out")


class RuntimeTests(unittest.TestCase):
    def test_loopback_http_binding_does_not_depend_on_dns(self):
        with mock.patch("socket.getfqdn", side_effect=AssertionError("Unexpected DNS lookup")):
            server = app.Server(0, None)
        try:
            self.assertEqual(server.server_name, "127.0.0.1")
            self.assertGreater(server.server_port, 0)
        finally:
            server.server_close()

    def test_process_lock_excludes_another_process_and_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"running.lock"
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                self.assertTrue(local_runtime.try_lock(fd))
                code = "import os,sys; from local_runtime import try_lock; fd=os.open(sys.argv[1],os.O_RDWR); print(try_lock(fd)); os.close(fd)"
                result = subprocess.run([sys.executable, "-c", code, str(path)], cwd=ROOT,
                                        capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), "False")
                self.assertTrue(launcher.already_running(folder, no_browser=True))
            finally:
                os.close(fd)
            self.assertFalse(launcher.already_running(folder, no_browser=True))

    def test_printable_ready_marker_at_every_chunk_boundary(self):
        raw = b"Password: __SD_READY_token:123.sd_test__SD_END__\x1b[Hscreen"
        for split in range(len(raw)+1):
            refs = []
            f = app.ReadyFilter("token", refs.append, printable=True)
            output = f.feed(raw[:split]) + f.feed(raw[split:]) + f.feed(b"", final=True)
            self.assertEqual(output, b"Password: \x1b[Hscreen")
            self.assertEqual(refs, ["123.sd_test"])

    def test_windows_arguments_keep_config_and_disable_unsupported_mux(self):
        with tempfile.TemporaryDirectory() as folder:
            ws = app.Workspace(Path(folder)/"data", ssh_config=False)
            try:
                profile = app.clean_profile({"host": "test-alias"})
                with mock.patch.object(app, "WINDOWS", True):
                    enabled = ws.ssh_args(profile, forwarding=True)
                    disabled = ws.ssh_args(profile, forwarding=False)
                    control = ws.ssh_args(profile, control=True, forwarding=True)
                self.assertIn("ControlMaster=no", enabled)
                self.assertIn("ControlPath=none", enabled)
                self.assertIn("ClearAllForwardings=no", enabled)
                self.assertIn("ClearAllForwardings=yes", disabled)
                self.assertIn("ClearAllForwardings=yes", control)
                self.assertNotIn("ProxyCommand=none", enabled)
                self.assertEqual(enabled[-1], "test-alias")
            finally:
                ws.shutdown()

    @unittest.skipUnless(os.name == "nt", "Windows Include paths")
    def test_config_include_preserves_windows_backslashes_and_spaces(self):
        with tempfile.TemporaryDirectory(prefix="sd config ") as folder:
            root = Path(folder)
            included = root/"hosts.conf"
            included.write_text("Host native-server\n", encoding="utf-8")
            config = root/"config"
            original = 'Include "' + str(included) + '"\nHost default-server\n'
            config.write_text(original, encoding="utf-8")
            profiles, notes = read_profiles(config)
            self.assertEqual([p["host"] for p in profiles], ["native-server", "default-server"])
            self.assertFalse(notes)
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_terminal_unicode_resize_input_and_local_exit(self):
        code = "import time; print('PROMPT',flush=True); s=input(); print('RESULT:'+s,flush=True); time.sleep(20)"
        transport = local_runtime.open_terminal([sys.executable, "-u", "-c", code], 100, 30,
                                               dict(os.environ, PYTHONIOENCODING="utf-8"))
        data = bytearray()
        def read_until(text):
            end = time.monotonic()+10
            while time.monotonic() < end:
                chunk = transport.read(.2)
                if chunk:
                    data.extend(chunk)
                    if b"\x1b[6n" in chunk:
                        transport.write("\x1b[1;1R")
                if text.encode("utf-8") in data:
                    return
            self.fail(repr(data))
        try:
            read_until("PROMPT")
            transport.resize(120, 40)
            transport.write("测试 Unicode\r")
            read_until("RESULT:测试 Unicode")
        finally:
            transport.close()
        wait_for(lambda: transport.process.poll() is not None)

    def test_native_http_startup_auth_single_instance_and_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder)/"data"
            process = subprocess.Popen([sys.executable, "-u", "app.py", "--no-browser", "--data-dir", str(data_dir),
                                        "--ssh-config", str(Path(folder)/"empty-config")],
                                       cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                runtime = data_dir/"running.json"
                try:
                    wait_for(lambda: runtime.exists() or process.poll() is not None)
                except AssertionError:
                    process.terminate()
                    output, unused = process.communicate(timeout=5)
                    self.fail("App did not finish starting: " + output.decode("utf-8", "replace"))
                if process.poll() is not None:
                    self.fail("App exited before startup: " + process.stdout.read().decode("utf-8", "replace"))
                info = json.loads(runtime.read_text(encoding="utf-8"))
                match = re.fullmatch(r"(http://127\.0\.0\.1:(\d+))/#token=([a-f0-9]{64})", info["url"])
                self.assertIsNotNone(match)
                origin, port, token = match.groups()
                client = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
                try:
                    client.request("GET", "/api/workspace")
                    response = client.getresponse()
                    self.assertEqual(response.status, 403)
                    response.read()
                    client.request("GET", "/api/workspace", headers={"X-ScreenDesk-Token": token})
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
                    second = subprocess.run([sys.executable, "launcher.py", "--offline", "--no-browser", "--data-dir", str(data_dir)],
                                            cwd=ROOT, capture_output=True, timeout=10)
                    self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                    self.assertIn("正在运行".encode("utf-8"), second.stdout)
                    client.request("POST", "/api/shutdown", body="{}", headers={"X-ScreenDesk-Token": token,
                                   "Origin": origin, "Content-Type": "application/json"})
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
                finally:
                    client.close()
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertFalse(runtime.exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                process.stdout.close()


class PipeTests(unittest.TestCase):
    def test_sftp_handshake_pipelined_binary_download_and_resume_offset(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"source.bin"
            payload = bytes(range(256))*8193
            source.write_bytes(payload)
            workspace = SimpleNamespace(ssh_args=lambda *a: [sys.executable, str(ROOT/"tests/sftp_stub.py"), str(source), "isolated"])
            with mock.patch.object(transfers, "WINDOWS", True):
                client = transfers.SFTP(workspace, {}, False)
            try:
                handle = client.open("/source.bin")
                self.assertEqual(client.fstat(handle)["size"], len(payload))
                first = client.read_block(handle, 0, 1024*1024)
                self.assertEqual(first, payload[:1024*1024])
            finally:
                client.close()
            with mock.patch.object(transfers, "WINDOWS", True):
                resumed = transfers.SFTP(workspace, {}, False)
            try:
                handle = resumed.open("/source.bin")
                self.assertEqual(resumed.read_block(handle, len(first), 1024*1024), payload[len(first):2*1024*1024])
                resumed.close_handle(handle)
            finally:
                resumed.close()

    @contextlib.contextmanager
    def child(self, code):
        process = subprocess.Popen([sys.executable, "-u", "-c", code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        pipes = TimedPipes(process)
        try:
            yield pipes
        finally:
            pipes.close()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            pipes.join()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_binary_roundtrip_with_fragmented_output(self):
        code = "import sys\nwhile True:\n b=sys.stdin.buffer.read(1)\n if not b: break\n sys.stdout.buffer.write(b); sys.stdout.buffer.flush()"
        with self.child(code) as pipes:
            payload = bytes(range(256))*128
            pipes.write(payload, time.monotonic()+10)
            self.assertEqual(pipes.read(len(payload), time.monotonic()+10), payload)

    def test_read_timeout_cancellation_and_blocked_write_timeout(self):
        with self.child("import time; time.sleep(20)") as pipes:
            with self.assertRaises(TimeoutError):
                pipes.read(1, time.monotonic()+.15)
            stop = threading.Event()
            stop.set()
            with self.assertRaisesRegex(OSError, "暂停"):
                pipes.read(1, time.monotonic()+2, stop)
            with self.assertRaises(TimeoutError):
                pipes.write(b"x"*(2*1024*1024), time.monotonic()+.15)


class TunnelTests(unittest.TestCase):
    def test_concurrent_forwarded_windows_share_config_tunnel_until_server_close(self):
        transports = []
        class FakeTransport:
            def __init__(self, args, *unused):
                self.marker = re.search(r"__SD_TUNNEL_[a-f0-9]+__", args[-1])[0].encode()
                self.incoming = queue.Queue()
                self.incoming.put(b"Password: ")
                self.closed = False
                self.process = self
                transports.append(self)
            def read(self):
                try:
                    return self.incoming.get(timeout=.05)
                except queue.Empty:
                    return None
            def write(self, value):
                self.incoming.put(self.marker[:10])
                self.incoming.put(self.marker[10:])
            def poll(self):
                return 0 if self.closed else None
            def close(self):
                self.closed = True
                self.incoming.put(b"")
        profile = {"host": "isolated"}
        calls = []
        def args(p, **kwargs):
            calls.append(kwargs)
            return ["ssh", p["host"]]
        workspace = SimpleNamespace(connection_key=lambda p: p["host"], ssh_args=args)
        owner = SimpleNamespace(cols=80, rows=24, lock=threading.RLock(), stopped=False,
                                auth_transport=None, output=lambda data: None, event=lambda **kw: None)
        other = SimpleNamespace(cols=80, rows=24, lock=threading.RLock(), stopped=False,
                                auth_transport=None, output=lambda data: None, event=lambda **kw: None)
        manager = NativeTunnels(workspace)
        errors = []
        def ensure(terminal):
            try:
                manager.ensure(profile, terminal)
            except Exception as exc:
                errors.append(exc)
        with mock.patch("windows_tunnels.open_terminal", side_effect=FakeTransport):
            try:
                first = threading.Thread(target=ensure, args=(owner,))
                first.start()
                wait_for(lambda: owner.auth_transport is not None)
                second = threading.Thread(target=ensure, args=(other,))
                second.start()
                owner.auth_transport.write("test-auth")
                first.join(timeout=5)
                second.join(timeout=5)
                self.assertFalse(first.is_alive() or second.is_alive())
                self.assertFalse(errors)
                self.assertEqual(len(transports), 1)
                self.assertEqual(calls, [{"forwarding": True}])
                owner.stopped = True
                manager.ensure(profile, other)
                self.assertFalse(transports[0].closed)
                manager.close_profile(profile)
                self.assertTrue(transports[0].closed)
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
