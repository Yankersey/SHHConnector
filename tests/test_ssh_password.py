"""Real system OpenSSH against a disposable password-only loopback SSH server."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
try:
    from ssh_fixture import CODE, PASSWORD, SSHFixture
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
