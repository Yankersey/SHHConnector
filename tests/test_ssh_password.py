"""Real system OpenSSH against a disposable password-only loopback SSH server."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import jump_transfer
try:
    from ssh_fixture import CODE, PASSWORD, Files, SSHFixture
except ImportError:
    SSHFixture = None


@unittest.skipUnless(SSHFixture and shutil.which(app.SSH), "Install requirements-test.txt and system OpenSSH")
class PasswordSSHTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="sd password ")
        self.root = Path(self.folder.name)
        self.remote = self.root/"remote"
        self.remote.mkdir()
        self.fixture = SSHFixture(self.remote, mfa=True)
        self.payload = bytes(range(256))*8193
        (self.remote/"payload.bin").write_bytes(self.payload)
        config = self.root/"ssh_config"
        config.write_text('Host *\n UserKnownHostsFile "'+(self.root/"known_hosts").as_posix()+'"\n'
                          ' GlobalKnownHostsFile none\n StrictHostKeyChecking ask\n PubkeyAuthentication no\n'
                          ' PreferredAuthentications keyboard-interactive,password\n PasswordAuthentication yes\n'
                          ' KbdInteractiveAuthentication yes\n NumberOfPasswordPrompts 2\n', encoding="utf-8")
        self.ws = app.Workspace(self.root/"client"/"data", ssh_config=config)
        self.profile = app.clean_profile(dict(host="127.0.0.1", user="fixture", port=str(self.fixture.port), name="隔离密码服务器"))
        self.ws.profiles.append(self.profile)
        self.ws.activate(self.profile["id"], False)

    def tearDown(self):
        self.ws.shutdown()
        self.fixture.close()
        self.folder.cleanup()

    def resolve(self, done, wrong=False, cancel=False, timeout=25):
        seen, requests = set(), []
        deadline = time.monotonic()+timeout
        try:
            while not done():
                if time.monotonic() >= deadline:
                    self.fail("Password-only OpenSSH fixture did not finish")
                for prompt in self.ws.auth.snapshot():
                    if prompt["id"] in seen:
                        continue
                    if prompt["kind"] == "confirm":
                        # Host authenticity must be visible before accepting.
                        if "SHA256:" not in prompt["details"]+prompt["prompt"]:
                            continue
                        answer = "yes"
                    elif cancel:
                        self.ws.auth.answer(prompt["id"], cancel=True)
                        seen.add(prompt["id"])
                        requests.append(prompt)
                        continue
                    elif "code" in prompt["prompt"].lower():
                        answer = CODE
                    else:
                        answer = "wrong-fixture-password" if wrong else PASSWORD
                    self.ws.auth.answer(prompt["id"], answer)
                    seen.add(prompt["id"])
                    requests.append(prompt)
                time.sleep(.02)
            return requests
        except BaseException:
            self.ws.auth.close()
            raise

    def test_screen_scan_uses_real_password_and_mfa_without_stdin(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.ws.scan_screens, self.profile["id"], False)
            prompts = self.resolve(future.done)
            result = future.result(timeout=3)
        self.assertEqual(result["entries"][0]["name"], "模型训练")
        self.assertTrue(any(p["kind"] == "confirm" for p in prompts))
        self.assertTrue(any("password" in p["prompt"].lower() for p in prompts))
        self.assertTrue(any("code" in p["prompt"].lower() for p in prompts))
        self.assertFalse(self.ws.records)

    def test_file_listing_and_binary_download_authenticate_independently(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.ws.transfers.list_directory, self.profile, "/", False)
            self.resolve(future.done)
            result = future.result(timeout=3)
        self.assertIn("payload.bin", [entry["name"] for entry in result["entries"]])
        created = self.ws.transfers.create(self.profile, "/payload.bin", False)
        job = next(job for job in self.ws.transfers.jobs if job["id"] == created["id"])
        prompts = self.resolve(lambda: job["state"] in ("completed", "paused", "changed"))
        self.assertEqual(job["state"], "completed", job["message"])
        self.assertTrue(any("password" in p["prompt"].lower() for p in prompts))
        self.assertEqual(Path(job["local_path"]).read_bytes(), self.payload)
        self.ws.save()
        for name in ("workspace.json", "downloads.json"):
            saved = (self.ws.data_dir/name).read_text(encoding="utf-8")
            self.assertNotIn(PASSWORD, saved)
            self.assertNotIn(CODE, saved)

    def test_cancel_download_authentication_pauses_without_retrying(self):
        created = self.ws.transfers.create(self.profile, "/payload.bin", False)
        job = next(job for job in self.ws.transfers.jobs if job["id"] == created["id"])
        self.resolve(lambda: job["state"] == "paused", cancel=True)
        time.sleep(.3)
        self.assertIn("取消", job["message"])
        self.assertEqual(job["retries"], 0)
        self.assertFalse(self.ws.auth.snapshot())
        self.assertEqual(self.fixture.password_attempts, 0)

    def test_binary_upload_authenticates_and_uses_sftp(self):
        name = "上传 # 文件.bin"
        created = self.ws.transfers.create_upload(self.profile, ".", name, len(self.payload), BytesIO(self.payload))
        job = next(job for job in self.ws.transfers.jobs if job["id"] == created["id"])
        prompts = self.resolve(lambda: job["state"] in ("completed", "paused", "changed"))
        self.assertEqual(job["state"], "completed", job["message"])
        self.assertTrue(any("password" in p["prompt"].lower() for p in prompts))
        self.assertTrue(any("code" in p["prompt"].lower() for p in prompts))
        self.assertEqual((self.remote/name).read_bytes(), self.payload)

    @unittest.skipIf(os.name == "nt", "loopback lab-jump fixture requires a POSIX shell")
    def test_server_copy_runs_on_jump_with_passwords_and_resumes(self):
        destination = self.root/"target"
        destination.mkdir()
        state = self.root/"jump-state"
        tools = self.root/"jump-bin"
        tools.mkdir()
        remote_config = self.root/"jump-ssh-config"
        remote_config.write_text('Host *\n UserKnownHostsFile "'+str(self.root/"jump-known-hosts")+'"\n'
                                 ' GlobalKnownHostsFile none\n')
        ssh = shutil.which(app.SSH)
        wrapper = tools/"ssh"
        wrapper.write_text("#!/bin/sh\nexec "+shlex.quote(ssh)+" -F "+shlex.quote(str(remote_config))+' "$@"\n')
        wrapper.chmod(0o700)
        written = []
        dropped = threading.Event()

        class InterruptedFiles(Files):
            def open(self, path, flags, attr):
                handle = super().open(path, flags, attr)
                if not isinstance(handle, int) and handle.writefile:
                    original = handle.write

                    def write(offset, data):
                        written.append(offset)
                        result = original(offset, data)
                        if offset == 1024*1024 and not dropped.is_set():
                            dropped.set()
                            target.transports[-1].close()
                        return result

                    handle.write = write
                return handle

        def execute(channel, command):
            env = dict(os.environ, XDG_CACHE_HOME=str(state),
                       PATH=str(tools)+os.pathsep+os.environ.get("PATH", os.defpath))
            process = subprocess.Popen(["/bin/sh", "-c", command.decode()], env=env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       bufsize=0)

            def input_loop():
                try:
                    for chunk in iter(lambda: channel.recv(32768), b""):
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(process.stdin.fileno(), view):]
                except OSError:
                    pass
                finally:
                    process.stdin.close()

            threading.Thread(target=input_loop, daemon=True).start()
            try:
                for chunk in iter(lambda: process.stdout.read(32768), b""):
                    channel.sendall(chunk)
                channel.send_exit_status(process.wait(timeout=4))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
                channel.close()

        target = SSHFixture(destination, mfa=True, files=InterruptedFiles)
        jump = SSHFixture(self.root, execute=execute)
        self.addCleanup(target.close)
        self.addCleanup(jump.close)
        local_config = self.ws.ssh_config
        for alias, fixture in (("source-host", self.fixture), ("target-host", target), ("lab-jump", jump)):
            local_config.write_text(local_config.read_text() +
                                    "\nHost "+alias+"\n HostName 127.0.0.1\n User fixture\n Port "+str(fixture.port)+"\n"+
                                    (" ProxyJump lab-jump\n" if alias!="lab-jump" else ""))
        source, dest, jump_profile = [app.clean_profile(dict(host=name)) for name in
                                      ("source-host", "target-host", "lab-jump")]
        self.ws.profiles.extend([source, dest, jump_profile])
        for profile in (source, dest):
            self.ws.activate(profile["id"], False)
        control_bytes = 0

        class CountedPipes(jump_transfer.TimedPipes):
            def read(self, size, deadline, stop=None):
                nonlocal control_bytes
                data = super().read(size, deadline, stop)
                control_bytes += len(data)
                return data

        with mock.patch.object(jump_transfer, "TimedPipes", CountedPipes):
            created = self.ws.transfers.create_copy(dest, ".", source, "/payload.bin")
            job = next(job for job in self.ws.transfers.jobs if job["id"] == created["id"])
            prompts = self.resolve(lambda: job["state"] in ("completed", "paused", "changed"), timeout=40)
            self.assertEqual(job["state"], "completed", job["message"])
            self.assertTrue(dropped.is_set())
            self.assertEqual(written.count(0), 1)
            self.assertGreaterEqual(written.count(1024*1024), 2)
            self.assertEqual((destination/"payload.bin").read_bytes(), self.payload)
            self.assertLess(control_bytes, 65536)
            self.assertFalse(list(self.ws.transfers.folder.iterdir()))
            self.assertTrue({"source-host", "target-host", "lab-jump"}.issubset(
                {p["server"] for p in prompts if p["kind"] == "secret"}))
            for saved in state.rglob("*.json"):
                self.assertNotIn(PASSWORD, saved.read_text())
                self.assertNotIn(CODE, saved.read_text())

    def test_wrong_password_stops_at_paused_instead_of_reprompting_forever(self):
        created = self.ws.transfers.create(self.profile, "/payload.bin", False)
        job = next(job for job in self.ws.transfers.jobs if job["id"] == created["id"])
        prompts = self.resolve(lambda: job["state"] == "paused", wrong=True)
        self.assertGreaterEqual(sum(p["kind"] == "secret" for p in prompts), 1)
        self.assertEqual(job["retries"], 0)
        self.assertIn("重新认证", job["message"])
        time.sleep(.3)
        self.assertFalse(self.ws.auth.snapshot())

    def test_changed_host_key_is_rejected_without_asking_for_password(self):
        wrong_key = type(self.fixture.key).generate(2048)
        (self.root/"known_hosts").write_text("[127.0.0.1]:%s %s %s\n" %
                                           (self.fixture.port, wrong_key.get_name(), wrong_key.get_base64()), encoding="ascii")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.ws.scan_screens, self.profile["id"], False)
            prompts = self.resolve(future.done)
            with self.assertRaises(app.UserError):
                future.result(timeout=3)
        self.assertFalse(prompts)
        self.assertEqual(self.fixture.password_attempts, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
