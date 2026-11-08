"""Exercise the actual WebView2 renderer against a disposable ScreenDesk backend."""
from pathlib import Path
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webview
from windows_app import Backend, endpoint, local_request, secure_webview

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="sd webview ") as folder:
        config = Path(folder) / "empty-config"
        config.write_text("", encoding="utf-8")
        backend = Backend(ROOT, Path(folder) / "data", ["--offline", "--ssh-config", str(config)])
        url = backend.connect(timeout=20)
        window = webview.create_window("ScreenDesk renderer test", url=url, width=900, height=650)
        secure_webview(window, endpoint(url)[0])
        result = []
        outside_requests = []
        checked = threading.Event()

        class Outside(BaseHTTPRequestHandler):
            def do_GET(self):
                outside_requests.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        outside = ThreadingHTTPServer(("127.0.0.1", 0), Outside)
        threading.Thread(target=outside.serve_forever, daemon=True).start()

        def loaded():
            if checked.is_set():
                return
            checked.set()
            try:
                result.append(window.evaluate_js("document.title.startsWith('ScreenDesk')"))
                result.append(window.evaluate_js("!!document.querySelector('#workspace-sidebar')"))
                foreign = "http://127.0.0.1:%s/" % outside.server_port
                window.evaluate_js("window.location.href = %r; true" % foreign)
                time.sleep(.5)
                result.append(window.evaluate_js("!!document.querySelector('#workspace-sidebar')"))
                window.evaluate_js("window.open(%r); true" % foreign)
                time.sleep(.5)
                result.append(not outside_requests)
            finally:
                window.destroy()

        window.events.loaded += loaded
        watchdog = threading.Timer(35, window.destroy)
        watchdog.daemon = True
        watchdog.start()
        try:
            webview.start(gui="edgechromium", debug=False, private_mode=True,
                          icon=str(ROOT / "assets" / "ScreenDesk.ico"))
            assert result == [True] * 4, (result, outside_requests)
            assert backend.process.poll() is None, "Closing the window stopped the backend"
            local_request(url, "/api/workspace")
            print("PASS: WebView2 rendered ScreenDesk; closing the window preserved the backend")
        finally:
            watchdog.cancel()
            outside.shutdown()
            outside.server_close()
            local_request(url, "/api/shutdown", data=b"{}")
            backend.process.wait(timeout=10)


if __name__ == "__main__":
    main()
