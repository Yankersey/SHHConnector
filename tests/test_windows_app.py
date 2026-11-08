"""Desktop host tests use disposable local workspaces, never user SSH profiles."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app import Backend, allow_navigation, desktop_lock, endpoint, local_request
from local_runtime import background_options

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "a" * 64


class DesktopTests(unittest.TestCase):
    def test_runtime_url_must_be_exact_loopback_with_token(self):
        url = "http://127.0.0.1:1234/#token=" + TOKEN
        self.assertEqual(endpoint(url), ("http://127.0.0.1:1234", TOKEN))
        for invalid in [None, "https://example.com/#token=" + TOKEN,
                        "http://localhost:1234/#token=" + TOKEN,
                        url + "&next=evil", url.replace(":1234", ":65536"),
                        url.replace("127.0.0.1", "127.0.0.1@evil.example"),
                        url.replace("/#", "/redirect#")]:
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                endpoint(invalid)
        for invalid in ["https://example.com", "file:///C:/secrets",
                        "http://127.0.0.1:1234.evil.example/", "http://[broken"]:
            self.assertFalse(allow_navigation(invalid, "http://127.0.0.1:1234"))
        self.assertTrue(allow_navigation(url, "http://127.0.0.1:1234"))

    def test_redirect_does_not_receive_workspace_token(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/stolen-token")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(OSError):
                local_request("http://127.0.0.1:%s/#token=%s" %
                              (server.server_port, TOKEN), "/api/workspace")
            self.assertEqual(requests, ["/api/workspace"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_start_and_reuse_hidden_backend_preserves_local_data(self):
        with tempfile.TemporaryDirectory(prefix="sd desktop ") as folder:
            root = Path(folder)
            config = root / "empty-config"
            config.write_text("", encoding="utf-8")
            backend = Backend(ROOT, root / "data", ["--offline", "--ssh-config", str(config)])
            url = None
            try:
                url = backend.connect(timeout=20)
                self.assertIn("profiles", json.loads(local_request(url, "/api/workspace")))
                second = Backend(ROOT, root / "data", ["--offline"])
                self.assertEqual(second.connect(), url)
                self.assertIsNone(second.process)
                self.assertIsNone(backend.process.poll())
                marker = root / "data" / "keep-local.txt"
                marker.write_text("keep", encoding="utf-8")
                local_request(url, "/api/shutdown", data=b"{}")
                self.assertEqual(backend.process.wait(timeout=10), 0)
                self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
                self.assertIsNone(second.running_url())
            finally:
                if backend.process and backend.process.poll() is None:
                    if url:
                        local_request(url, "/api/shutdown", data=b"{}")
                    else:
                        backend.process.terminate()
                    backend.process.wait(timeout=10)

    def test_second_desktop_requests_activation_then_lock_is_released(self):
        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder)
            lock = desktop_lock(data_dir)
            self.assertIsNotNone(lock)
            try:
                code = ("from pathlib import Path; from windows_app import desktop_lock; "
                        "assert desktop_lock(Path(%r)) is None") % folder
                subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True,
                               timeout=10, **background_options())
                self.assertTrue((data_dir / "desktop.activate").exists())
            finally:
                lock.close()
            reopened = desktop_lock(data_dir)
            self.assertIsNotNone(reopened)
            reopened.close()

    @unittest.skipUnless(os.name == "nt", "Windows GUI executable")
    def test_backend_survives_the_windowed_executable_exiting(self):
        from install_windows_app import build_launcher
        with tempfile.TemporaryDirectory(prefix="sd desktop lifetime ") as folder:
            root = Path(folder)
            (root / "empty-config").write_text("", encoding="utf-8")
            (root / "windows_app.py").write_text(
                "from pathlib import Path\nimport runpy,sys\nsys.path.insert(0,%r)\ndef main():\n"
                "    implementation=runpy.run_path(%r)\n"
                "    root=Path(__file__).parent\n"
                "    backend=implementation['Backend'](%r,root/'data',"
                "['--offline','--ssh-config',str(root/'empty-config')])\n"
                "    backend.connect(timeout=15)\n    return 0\n" %
                (str(ROOT), str(ROOT / "windows_app.py"), str(ROOT)), encoding="utf-8")
            executable = build_launcher(root)
            backend = Backend(ROOT, root / "data", [])
            try:
                subprocess.run([str(executable)], cwd=root, check=True, timeout=25)
                time.sleep(.3)
                self.assertIsNotNone(backend.running_url(), "Closing the desktop executable killed the backend")
            finally:
                url = backend.running_url()
                if url:
                    local_request(url, "/api/shutdown", data=b"{}")
                    deadline = time.monotonic() + 10
                    while (root / "data" / "running.json").exists() and time.monotonic() < deadline:
                        time.sleep(.05)

    @unittest.skipUnless(os.name == "nt", "Windows GUI executable")
    def test_windowed_executable_with_spaces_unicode_and_idempotent_install(self):
        from install_windows_app import build_launcher
        with tempfile.TemporaryDirectory(prefix="sd 桌面 app ") as folder:
            root = Path(folder)
            (root / "windows_app.py").write_text(
                "from pathlib import Path\nimport sys\ndef main():\n"
                "    Path(__file__).with_name('result.txt').write_text("
                "str(sys.stdout is None and sys.stderr is None))\n    return 0\n",
                encoding="utf-8")
            exe = build_launcher(root)
            modified = exe.stat().st_mtime_ns
            self.assertEqual(build_launcher(root), exe)
            self.assertEqual(exe.stat().st_mtime_ns, modified)
            subprocess.run([str(exe)], cwd=root, check=True, timeout=15)
            self.assertEqual((root / "result.txt").read_text(), "True")


if __name__ == "__main__":
    unittest.main()
