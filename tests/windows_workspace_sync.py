"""Two real WebView2 pages share one fixture backend, with no remote SSH."""
import json
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import select
import socket
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import webview


def main():
    with tempfile.TemporaryDirectory(prefix="sd workspace sync ") as folder:
        workspace = app.Workspace(Path(folder) / "data", ssh_config=False)
        profile = app.clean_profile(dict(host="isolated-fixture"))
        workspace.profiles.append(profile)
        workspace.activate(profile["id"], False)
        record = workspace.new_session(dict(profile_id=profile["id"], name="电脑已有窗口", layout={"visible":True}))
        server = app.Server(0, workspace)
        class Proxy(BaseHTTPRequestHandler):
            """Local stand-in for cloudflared's Host rewrite and WS relay."""
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    self.close_connection = True
                    with socket.create_connection(server.server_address, timeout=10) as upstream:
                        headers = [(key, server.origin.split("//", 1)[1] if key.lower() == "host" else value)
                                   for key, value in self.headers.items()]
                        upstream.sendall((self.requestline + "\r\n" + "".join(
                            "%s: %s\r\n" % item for item in headers) + "\r\n").encode())
                        peers = (self.connection, upstream)
                        try:
                            while True:
                                ready, _, _ = select.select(peers, [], [], 10)
                                if not ready:
                                    continue
                                for source in ready:
                                    raw = source.recv(65536)
                                    if not raw:
                                        return
                                    (upstream if source is self.connection else self.connection).sendall(raw)
                        except OSError:
                            return
                else:
                    self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                client = http.client.HTTPConnection(*server.server_address, timeout=10)
                try:
                    headers = dict(self.headers)
                    headers["Host"] = server.origin.split("//", 1)[1]
                    body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    client.request(self.command, self.path, body=body, headers=headers)
                    response = client.getresponse()
                    raw = response.read()
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in ("connection", "transfer-encoding", "content-length"):
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                finally:
                    client.close()

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        proxy.daemon_threads = True
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        # Exercise real cross-origin mobile HTTP/WS through a reverse proxy;
        # no public tunnel, remote server, or real credentials are used.
        server.mobile.origin = "http://127.0.0.1:%s" % proxy.server_port
        server.mobile.token = "b" * 64
        server.mobile.state = "running"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started, writes, failures = [], [], []

        def start(terminal):
            started.append(terminal.record["id"])
            terminal.record.update(state="connected", ever_ready=True, screen_ref="123.fixture")
            terminal.event(type="state", state="connected", screen_ref="123.fixture")
            terminal.ws.send_frame(2, "同步画面 中文🙂\r\n".encode())

        def write(terminal, text):
            writes.append((terminal.record["id"], text))
            terminal.ws.send_frame(2, text.encode())

        desktop = webview.create_window("ScreenDesk desktop sync test", url=server.origin + "/#token=" + workspace.token,
                                        width=1100, height=760)
        phone = webview.create_window("ScreenDesk phone sync test", url=server.mobile.origin + "/#token=" + server.mobile.token,
                                      width=420, height=760, min_size=(360, 500))

        def evaluate(window, expression):
            return window.evaluate_js(expression)

        resources = {desktop.uid: [], phone.uid: []}
        for window in (desktop, phone):
            def response_received(response, target=window):
                resources[target.uid].append((str(response.url).split("#")[0], response.status_code, response.headers))
            window.events.response_received += response_received

        def wait(window, expression, label):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if evaluate(window, expression):
                        return
                except Exception:
                    pass
                time.sleep(.1)
            details = evaluate(window, "({title:document.title,status:document.querySelector('#app-status')?.textContent,toast:document.querySelector('#toast-container')?.textContent,loaded:typeof WorkspaceSync})")
            raise AssertionError("Timed out: %s %r; resources=%r" % (label, details, resources[window.uid]))

        def exercise():
            try:
                identifier = json.dumps(record["id"])
                wait(desktop, "windows.size===1", "desktop restored window")
                wait(phone, "windows.size===1", "phone restored same window")
                evaluate(desktop, "reconnectWindow(windows.get(%s)); true" % identifier)
                wait(desktop, "windows.get(%s).canControl" % identifier, "desktop connected and owns input")
                wait(phone, "windows.get(%s).ws?.readyState===1 && !windows.get(%s).sharing.replaying" %
                     (identifier, identifier), "phone automatically joins existing desktop PTY")
                assert len(started) == 1, started
                assert evaluate(phone, "windows.get(%s).canControl===false" % identifier)
                assert evaluate(phone, "TerminalView.history(windows.get(%s).term).includes('同步画面')" % identifier)
                # Readonly phone cannot inject input; takeover keeps both sockets alive.
                evaluate(phone, "send(windows.get(%s),{type:'input',data:'REJECT'}); true" % identifier)
                assert not writes, writes
                evaluate(phone, "windows.get(%s).control.click(); true" % identifier)
                wait(phone, "windows.get(%s).canControl" % identifier, "phone takeover")
                wait(desktop, "!windows.get(%s).canControl" % identifier, "desktop becomes readonly")
                evaluate(phone, "send(windows.get(%s),{type:'input',data:'PHONE'}); true" % identifier)
                wait(desktop, "TerminalView.history(windows.get(%s).term).includes('PHONE')" % identifier, "shared output")
                assert len(started) == 1, started
                # Create through the real phone form, including forwarding approval.
                evaluate(phone, "openNew(); document.querySelector('#new-form').elements.name.value='手机新窗口'; "
                                "document.querySelector('#new-form').elements.forwarding.value='0'; "
                                "document.querySelector('#new-form').requestSubmit(); true")
                wait(phone, "windows.size===2", "phone-created window")
                wait(desktop, "windows.size===2", "phone window appears on desktop without refresh")
                wait(desktop, "[...windows.values()].every(w=>w.ws?.readyState===1)", "desktop observes new phone PTY")
                assert len(started) == 2, started
                assert evaluate(phone, "JSON.stringify([...windows.keys()].sort())") == evaluate(desktop, "JSON.stringify([...windows.keys()].sort())")
                # Forget through the owner page, with neither screen quit nor new SSH.
                evaluate(phone, "forgetRecord(%s); true" % identifier)
                wait(phone, "windows.size===1", "phone removal")
                wait(desktop, "windows.size===1", "desktop removal sync")
                assert len(started) == 2, started
                print("PASS: real desktop/phone pages share windows, PTYs, takeover, input and deletion")
            except Exception as exc:
                failures.append(exc)
            finally:
                desktop.destroy()
                phone.destroy()

        watchdog = threading.Timer(120, lambda: (desktop.destroy(), phone.destroy()))
        watchdog.daemon = True
        watchdog.start()
        try:
            with mock.patch.object(app.Terminal, "start", start), mock.patch.object(app.Terminal, "write", write):
                webview.start(exercise, gui="edgechromium", debug=False, private_mode=True)
            if failures:
                raise failures[0]
            assert len(started) == 2, "UI test did not finish"
        finally:
            watchdog.cancel()
            server.shutdown()
            server.server_close()
            proxy.shutdown()
            proxy.server_close()
            workspace.shutdown()


if __name__ == "__main__":
    main()
