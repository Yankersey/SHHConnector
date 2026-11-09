"""Windows desktop host: a WebView2 window and a hidden local backend."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from local_runtime import background_options, try_lock

ROOT = Path(__file__).resolve().parent
WORKSPACE_URL = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})/#token=([0-9a-f]{64})\Z")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint(url):
    match = WORKSPACE_URL.fullmatch(url) if isinstance(url, str) else None
    if not match or not 0 < int(match[1]) < 65536:
        raise ValueError("Invalid local ScreenDesk address")
    return "http://127.0.0.1:" + match[1], match[2]


def local_request(url, path, data=None):
    origin, token = endpoint(url)
    request = Request(origin + path, data=data,
                      headers={"X-ScreenDesk-Token": token, "Content-Type": "application/json"})
    # System proxies must never receive the local authentication token. Do not
    # forward it to redirect targets, even if the runtime file is stale.
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=1) as response:
        return response.read()


class Backend:
    def __init__(self, root, data_dir, arguments):
        self.root, self.data_dir = Path(root), Path(data_dir)
        self.arguments = list(arguments)
        self.process = None

    def running_url(self):
        try:
            url = json.loads((self.data_dir / "running.json").read_text(encoding="utf-8"))["url"]
            local_request(url, "/api/workspace")
            return url
        except (OSError, ValueError, KeyError, TypeError, URLError):
            return None

    def connect(self, timeout=90):
        existing = self.running_url()
        if existing:
            return existing
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe":
            python = python.with_name("python.exe")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # stdout includes the private workspace URL; discard rather than log it.
        with tempfile.TemporaryFile() as errors:
            self.process = subprocess.Popen(
                [str(python), str(self.root / "launcher.py"), "--no-browser",
                 "--data-dir", str(self.data_dir)] + self.arguments,
                cwd=str(self.root), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=errors,
                env=dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1"),
                **background_options())
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                url = self.running_url()
                if url:
                    return url
                if self.process.poll() is not None:
                    errors.seek(0)
                    detail = errors.read().decode("utf-8", "replace").strip()[-3000:]
                    raise RuntimeError(detail or "ScreenDesk 后台未能启动，请稍后重试。")
                time.sleep(.15)
        raise RuntimeError("启动超时。请检查 Git 网络连接，然后重新打开 ScreenDesk。")


def allow_navigation(uri, origin):
    if uri == "about:blank":
        return True
    try:
        parsed = urlsplit(uri)
        return (parsed.scheme + "://" + parsed.netloc == origin and
                not parsed.username and not parsed.password)
    except ValueError:
        return False


def secure_webview(window, origin):
    """Keep top-level navigation in the authenticated workspace; no native API."""
    import webview
    webview.settings["ALLOW_FILE_URLS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    def navigation(sender, args):
        if not allow_navigation(str(args.Uri), origin):
            args.Cancel = True

    def resource(sender, args):
        # NavigationStarting can cancel display after a request has begun.
        # Also reject foreign resources before any network request is sent.
        if not allow_navigation(str(args.Request.Uri), origin):
            args.Response = sender.Environment.CreateWebResourceResponse(
                None, 403, "Forbidden", "Content-Type: text/plain\r\n")

    def new_window(sender, args):
        args.Handled = True

    def context_menu(sender, args):
        editing = {"undo", "redo", "cut", "copy", "paste", "pasteAndMatchStyle", "selectAll"}
        for index in range(args.MenuItems.Count - 1, -1, -1):
            if str(args.MenuItems[index].Name) not in editing:
                args.MenuItems.RemoveAt(index)

    def configure(core):
        core.WebResourceRequested += resource
        core.NewWindowRequested -= window.native.browser.on_new_window_request
        core.NewWindowRequested += new_window
        core.Settings.AreDefaultContextMenusEnabled = True
        core.ContextMenuRequested += context_menu

    def initialized(sender, args):
        if args.IsSuccess:
            configure(sender.CoreWebView2)

    def before_show():
        native = window.native.webview
        native.NavigationStarting += navigation
        if native.CoreWebView2 is not None:
            configure(native.CoreWebView2)
        else:
            native.CoreWebView2InitializationCompleted += initialized

    window.events.before_show += before_show


def desktop_lock(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    stream = (data_dir / "desktop.lock").open("a+b")
    if try_lock(stream.fileno()):
        return stream
    stream.close()
    (data_dir / "desktop.activate").write_text(str(time.time_ns()), encoding="ascii")
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="ScreenDesk Windows desktop window")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    args, backend_args = parser.parse_known_args(argv)
    data_dir = Path(args.data_dir).resolve()
    lock = None
    try:
        if os.name != "nt":
            raise RuntimeError("此入口仅用于 Windows；macOS 请使用 ScreenDesk.app。")
        lock = desktop_lock(data_dir)
        if lock is None:
            return 0
        try:
            import webview
        except ImportError as exc:
            raise RuntimeError("请先运行 setup-windows.cmd，安装桌面窗口所需的依赖。") from exc
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ScreenDesk.Desktop")
        backend = Backend(ROOT, data_dir, backend_args)
        url = backend.connect()
        origin, _ = endpoint(url)
        window = webview.create_window("ScreenDesk", url=url, width=1280, height=820,
                                       min_size=(800, 550), text_select=True,
                                       background_color="#111827")
        secure_webview(window, origin)
        closed = threading.Event()
        window.events.closed += closed.set
        marker = data_dir / "desktop.activate"

        def activate_requests():
            previous = None
            while not closed.wait(.3):
                if not (data_dir / "running.json").exists():
                    window.destroy()
                    return
                try:
                    current = marker.read_text(encoding="ascii")
                except OSError:
                    continue
                if current != previous:
                    previous = current
                    window.restore()
                    window.show()

        # Force Chromium: never silently fall back to the old IE renderer.
        webview.start(activate_requests, gui="edgechromium", debug=False,
                      private_mode=False, storage_path=str(data_dir / "desktop" / "webview2"),
                      icon=str(ROOT / "assets" / "ScreenDesk.ico"))
        # Closing the window leaves remote sessions and downloads running.
        # The existing authenticated "退出程序" action stops the backend.
        return 0
    except Exception as exc:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(
                None, str(exc) + "\n\n若缺少依赖，请运行 setup-windows.cmd；桌面窗口需要 Microsoft Edge WebView2 Runtime。",
                "ScreenDesk — 启动失败", 0x10)
        return 1
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
