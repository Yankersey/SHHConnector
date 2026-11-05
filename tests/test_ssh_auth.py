"""Authentication tests use disposable local helpers and fixture credentials."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_runtime import background_options
from ssh_auth import AuthCancelled, AuthManager
import app


def wait_for(predicate, timeout=10):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        result = predicate()
        if result:
            return result
        time.sleep(.02)
    raise AssertionError("Authentication fixture timed out")


class AskpassTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="sd 认证 ")
        self.manager = AuthManager(self.folder.name)
        self.profile = dict(id="fixture", name="隔离认证测试", host="fixture.invalid")
        self.operation = self.manager.begin(self.profile, "测试认证")

    def tearDown(self):
        self.manager.close()
        self.folder.cleanup()

    def helper(self, prompt="Password: "):
        return subprocess.Popen([self.manager.helper, prompt], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self.operation.env(), **background_options())

    def test_native_helper_unicode_response_is_not_in_files_or_prompt_api(self):
        process = self.helper()
        try:
            prompt = wait_for(self.manager.snapshot)[0]
            secret = "fixture-密码-123"
            self.manager.answer(prompt["id"], secret)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout, secret.encode("utf-8")+b"\n")
            self.assertEqual(stderr, b"")
            self.assertNotIn(secret, str(process.args))
            self.assertNotIn(self.operation.token, json.dumps(prompt))
            wait_for(lambda: not self.manager.snapshot())
            for path in Path(self.folder.name).iterdir():
                self.assertNotIn(secret.encode("utf-8"), path.read_bytes())
                self.assertNotIn(self.operation.token.encode(), path.read_bytes())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)

    def test_sequential_prompts_do_not_reuse_answers_and_wait_excludes_timeout(self):
        for message, answer in (("Password:", "fixture-pass"), ("Verification code:", "123456")):
            process = self.helper(message)
            try:
                prompt = wait_for(self.manager.snapshot)[0]
                before = self.operation.clock()
                time.sleep(.15)
                self.assertLess(self.operation.clock()-before, .05)
                self.assertEqual(prompt["kind"], "secret")
                self.manager.answer(prompt["id"], answer)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout, answer.encode()+b"\n")
                wait_for(lambda: not self.manager.snapshot())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)

    def test_cancel_unblocks_helper_and_cancels_the_operation(self):
        process = self.helper()
        try:
            prompt = wait_for(self.manager.snapshot)[0]
            self.manager.answer(prompt["id"], cancel=True)
            stdout, stderr = process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(stdout, b"")
            with self.assertRaises(AuthCancelled):
                self.operation.check()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)

    def test_private_broker_rejects_unknown_token_without_showing_a_prompt(self):
        with socket.create_connection(self.manager.server.server_address, timeout=3) as connection:
            connection.sendall(json.dumps(dict(token="wrong-token", prompt="Password:")).encode()+b"\n")
            with connection.makefile("rb") as reader:
                self.assertEqual(json.loads(reader.readline()), dict(ok=False))
        self.assertFalse(self.manager.snapshot())

    def test_confirmation_requires_an_explicit_yes(self):
        process = self.helper("Are you sure you want to continue connecting (yes/no/[fingerprint])? ")
        try:
            prompt = wait_for(self.manager.snapshot)[0]
            self.assertEqual(prompt["kind"], "confirm")
            self.assertIsNone(process.poll())
            self.manager.answer(prompt["id"], "yes")
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(stdout, b"yes\n")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)

    def test_browser_prompt_api_requires_token_and_origin_and_never_returns_answer(self):
        workspace = SimpleNamespace(auth=self.manager, token="a"*64)
        server = app.Server(0, workspace)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        process = self.helper()
        try:
            prompt = wait_for(self.manager.snapshot)[0]
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(server.origin+"/api/auth/prompts")
            self.assertEqual(denied.exception.code, 403)
            headers = {"X-ScreenDesk-Token": workspace.token, "Content-Type": "application/json"}
            request = urllib.request.Request(server.origin+"/api/auth/prompts", headers=headers)
            with urllib.request.urlopen(request) as response:
                public = json.load(response)
            self.assertEqual(public["prompts"][0]["id"], prompt["id"])
            self.assertNotIn(self.operation.token, json.dumps(public))
            endpoint = server.origin+"/api/auth/"+prompt["id"]+"/answer"
            body = json.dumps(dict(answer="fixture-secret")).encode()
            wrong = urllib.request.Request(endpoint, data=body, headers=dict(headers, Origin="https://invalid.example"))
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(wrong)
            self.assertEqual(denied.exception.code, 403)
            valid = urllib.request.Request(endpoint, data=body, headers=dict(headers, Origin=server.origin))
            with urllib.request.urlopen(valid) as response:
                self.assertEqual(json.load(response), dict(ok=True))
            stdout, unused = process.communicate(timeout=5)
            self.assertEqual(stdout, b"fixture-secret\n")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
            server.shutdown()
            server.server_close()

    def test_unanswered_prompt_expires_and_stops_waiting(self):
        self.manager.prompt_timeout = .15
        process = self.helper()
        try:
            stdout, stderr = process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(stdout, b"")
            self.assertFalse(self.manager.snapshot())
            with self.assertRaises(AuthCancelled):
                self.operation.check()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
