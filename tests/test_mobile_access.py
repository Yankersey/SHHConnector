import http.client
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import app
import mobile_access


def wait_for(predicate):
    end = time.monotonic()+3
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Mobile access did not reach the expected state")


class FakeOutput:
    def __init__(self):
        self.lines = queue.Queue()
    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line.encode()
    def close(self):
        pass


class FakeTunnel:
    def __init__(self):
        self.stdout = FakeOutput()
        self.finished = threading.Event()
    def poll(self):
        return 0 if self.finished.is_set() else None
    def terminate(self):
        self.finished.set()
        self.stdout.lines.put(None)
    kill = terminate
    def wait(self, timeout=None):
        if not self.finished.wait(timeout):
            raise mobile_access.subprocess.TimeoutExpired("fake tunnel", timeout)
        return 0


class TunnelTests(unittest.TestCase):
    def test_tunnel_origin_token_rotation_and_immediate_revocation(self):
        revoked = mock.Mock()
        manager = mobile_access.MobileAccess("http://127.0.0.1:1234", revoked)
        child = FakeTunnel()
        with mock.patch.object(mobile_access, "cloudflared_path", return_value="fake-cloudflared"), mock.patch.object(mobile_access.subprocess, "Popen", return_value=child) as popen:
            manager.start()
            token = manager.token
            self.assertFalse(manager.accepts(token))
            child.stdout.lines.put("https://untrusted.example.org\n")
            child.stdout.lines.put("https://isolated-fixture.trycloudflare.com\n")
            child.stdout.lines.put("Registered tunnel connection connIndex=0\n")
            wait_for(lambda: manager.state == "running")
            self.assertTrue(manager.accepts(token, manager.origin))
            self.assertFalse(manager.accepts(token, "https://evil.example"))
            self.assertFalse(manager.accepts("中文"))
            self.assertNotEqual(token, "")
            args = popen.call_args[0][0]
            self.assertIn("--http-host-header", args)
            self.assertIn("127.0.0.1:1234", args)
            self.assertIn(mobile_access.os.devnull, args)
            manager.stop()
            self.assertFalse(manager.accepts(token))
            revoked.assert_called_once()
            child2 = FakeTunnel()
            with mock.patch.object(mobile_access.subprocess, "Popen", return_value=child2):
                manager.start()
            self.assertNotEqual(manager.token, token)
            manager.stop()

    def test_startup_timeout_revokes_and_stops_only_owned_process(self):
        manager = mobile_access.MobileAccess("http://127.0.0.1:1234", mock.Mock(), startup_timeout=.08)
        child = FakeTunnel()
        with mock.patch.object(mobile_access, "cloudflared_path", return_value="fake-cloudflared"), mock.patch.object(mobile_access.subprocess, "Popen", return_value=child):
            manager.start()
            wait_for(lambda: manager.state == "failed")
            self.assertIsNone(manager.token)
            wait_for(lambda: child.poll() == 0)
        manager.stop()

    def test_missing_dependency_does_not_enable_access(self):
        manager = mobile_access.MobileAccess("http://127.0.0.1:1", mock.Mock())
        with mock.patch.object(mobile_access, "cloudflared_path", return_value=None):
            with self.assertRaises(mobile_access.MobileAccessError):
                manager.start()
        self.assertEqual(manager.state, "off")
        self.assertIsNone(manager.token)


class MobileHTTPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ws = app.Workspace(Path(self.folder.name)/"data", ssh_config=False)
        self.server = app.Server(0, self.ws)
        self.server.mobile.origin = "https://isolated-fixture.trycloudflare.com"
        self.server.mobile.token = "b"*64
        self.server.mobile.state = "running"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.ws.shutdown()
        self.folder.cleanup()

    def request(self, path, token=None, origin=None, body=None, headers=None):
        values = dict(headers or {})
        if token is not None:
            values["X-ScreenDesk-Token"] = token
        if origin:
            values["Origin"] = origin
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            client.request("POST" if body is not None else "GET", path, body=json.dumps(body) if body is not None else None, headers=values)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def test_mobile_auth_origin_and_admin_boundaries(self):
        mobile = self.server.mobile
        self.assertEqual(self.request("/api/workspace")[0], 403)
        status, headers, body = self.request("/api/workspace", mobile.token, mobile.origin)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["client_mode"], "mobile")
        self.assertNotIn(mobile.token.encode(), body)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertEqual(self.request("/api/workspace", mobile.token, "https://evil.example")[0], 403)
        self.assertEqual(self.request("/api/mobile", mobile.token)[0], 403)
        self.assertEqual(self.request("/api/mobile/stop", mobile.token, body={})[0], 403)
        self.assertEqual(self.request("/api/shutdown", mobile.token, body={})[0], 403)
        self.assertEqual(self.request("/api/mobile", self.ws.token)[0], 200)
        old = mobile.token
        self.assertEqual(self.request("/api/mobile/stop", self.ws.token, body={})[0], 200)
        self.assertEqual(self.request("/api/workspace", old)[0], 403)
        self.assertEqual(self.request("/api/workspace", self.ws.token)[0], 200)

    def test_browser_file_download_range_cookie_and_revocation(self):
        path = self.ws.transfers.folder/"fixture.bin"
        path.write_bytes(b"0123456789"*1000)
        identifier = "c"*32
        self.ws.transfers.jobs.append(dict(id=identifier, state="completed", local_path=str(path), name="中文.bin"))
        url = "/api/downloads/"+identifier+"/file"
        self.assertEqual(self.request(url)[0], 403)
        cookie = {"Cookie":"sd-download="+self.server.mobile.token}
        status, headers, data = self.request(url, headers={**cookie, "Range":"bytes=123-234"})
        self.assertEqual(status, 206)
        self.assertEqual(data, path.read_bytes()[123:235])
        self.assertEqual(headers["Content-Range"], "bytes 123-234/10000")
        self.assertIn("filename*=UTF-8''", headers["Content-Disposition"])
        self.assertEqual(self.request(url, headers={**cookie,"Range":"bytes=-12"})[2], path.read_bytes()[-12:])
        self.assertEqual(self.request(url, headers={**cookie,"Range":"bytes=20000-"})[0],416)
        self.assertEqual(self.request(url, headers={**cookie,"Range":"bytes=0-1,4-5"})[0],416)
        self.assertEqual(self.request(url, headers={**cookie,"Range":"bytes=3-","If-Range":"old"})[0],200)
        # The download cookie cannot authorize arbitrary API or terminal access.
        self.assertEqual(self.request("/api/workspace", headers=cookie)[0],403)
        self.server.mobile.stop()
        self.assertEqual(self.request(url, headers=cookie)[0],403)

    def test_websocket_origin_and_revocation_detach_only_mobile_terminals(self):
        handler = object.__new__(app.Handler)
        handler.server = self.server
        handler.headers = {"Host":"127.0.0.1:%d" % self.server.server_port,
                           "Origin":self.server.mobile.origin,
                           "Sec-WebSocket-Protocol":"screen-desk, "+self.server.mobile.token}
        self.assertEqual(handler.auth_kind(True),"mobile")
        handler.headers["Origin"]="https://evil.example"
        self.assertIsNone(handler.auth_kind(True))
        handler.headers["Origin"]=self.server.mobile.origin
        phone = mock.Mock(stopped=False,mobile=True)
        desktop = mock.Mock(stopped=False,mobile=False)
        self.ws.terminals.update(phone=phone,desktop=desktop)
        with mock.patch.object(self.ws,"control") as control:
            self.server.mobile.stop()
            phone.stop.assert_called_once()
            desktop.stop.assert_not_called()
            control.assert_not_called()
        self.assertIsNone(handler.auth_kind(True))
        phone.stopped=desktop.stopped=True

    def test_mobile_websocket_upgrade_and_revocation_without_remote_ssh(self):
        profile = app.clean_profile(dict(host="isolated-test"))
        self.ws.profiles.append(profile)
        record = self.ws.new_session(dict(profile_id=profile["id"],name="mobile websocket"))
        self.ws.activate(profile["id"],False)
        sock = socket.create_connection(("127.0.0.1",self.server.server_port),timeout=3)
        reader = sock.makefile("rb")
        try:
            with mock.patch.object(app.Terminal,"start") as start:
                request = ("GET /ws/"+record["id"]+"?forwarding=0&cols=80&rows=24 HTTP/1.1\r\n"
                           "Host: 127.0.0.1:%d\r\n" % self.server.server_port +
                           "Origin: "+self.server.mobile.origin+"\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\n"
                           "Sec-WebSocket-Protocol: screen-desk, "+self.server.mobile.token+"\r\n\r\n")
                sock.sendall(request.encode())
                self.assertIn(b"101",reader.readline())
                while reader.readline().strip():
                    pass
                wait_for(lambda:start.called)
                terminal = self.ws.terminals[record["id"]]
                self.assertTrue(any(p.kind == 'mobile' for p in terminal.ws.viewers.values()))
                self.server.mobile.stop()
                self.assertTrue(terminal.stopped)
        finally:
            try:sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            reader.close()
            sock.close()

    def test_observe_only_race_never_starts_a_new_ssh_connection(self):
        profile = app.clean_profile(dict(host="isolated-test"))
        self.ws.profiles.append(profile)
        record = self.ws.new_session(dict(profile_id=profile["id"], name="observe race"))
        self.ws.activate(profile["id"], False)
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        reader = sock.makefile("rb")
        try:
            with mock.patch.object(app.Terminal, "start") as start:
                request = ("GET /ws/" + record["id"] + "?forwarding=0&shared=1&observe=1 HTTP/1.1\r\n"
                           "Host: 127.0.0.1:%d\r\n" % self.server.server_port +
                           "Origin: " + self.server.mobile.origin + "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\n"
                           "Sec-WebSocket-Protocol: screen-desk, " + self.server.mobile.token + "\r\n\r\n")
                sock.sendall(request.encode())
                self.assertIn(b"409", reader.readline())
                start.assert_not_called()
                self.assertNotIn(record["id"], self.ws.terminals)
        finally:
            reader.close()
            sock.close()

    def test_desktop_and_mobile_snapshots_advertise_the_same_live_transport(self):
        profile = app.clean_profile(dict(host="isolated-test"))
        self.ws.profiles.append(profile)
        record = self.ws.new_session(dict(profile_id=profile["id"], name="shared metadata"))
        hub = app.SharedTerminal(80, 24)
        terminal = app.Terminal(self.ws, record, hub, 80, 24)
        hub.terminal = terminal
        self.ws.terminals[record["id"]] = terminal
        desktop = json.loads(self.request("/api/workspace", self.ws.token)[2])
        phone = json.loads(self.request("/api/workspace", self.server.mobile.token)[2])
        self.assertEqual(desktop["shared"], phone["shared"])
        self.assertEqual(phone["shared"][record["id"]],
                         dict(connection_id=terminal.connection_id, forwarding=False))
        terminal.stopped = True
        self.assertFalse(self.ws.snapshot()["shared"])

    def test_explicit_takeover_requires_current_connection_and_never_quits_screen(self):
        profile = app.clean_profile(dict(host="isolated-test"))
        self.ws.profiles.append(profile)
        record = self.ws.new_session(dict(profile_id=profile["id"], name="mobile fixture"))
        terminal = mock.Mock(stopped=False, connection_id="owner-one", mobile=False)
        self.ws.terminals[record["id"]] = terminal
        url = "/api/sessions/"+record["id"]+"/connection"
        self.assertTrue(json.loads(self.request(url,self.ws.token,body={})[2])["occupied"])
        terminal.stop.assert_not_called()
        with mock.patch.object(self.ws,"control") as control:
            self.assertEqual(self.request(url,self.ws.token,body={"takeover":True,"owner":"stale"})[0],400)
            terminal.stop.assert_not_called()
            self.assertEqual(self.request(url,self.ws.token,body={"takeover":True,"owner":"owner-one"})[0],200)
            terminal.stop.assert_called_once()
            control.assert_not_called()
        terminal.stopped=True
        self.assertFalse(json.loads(self.request(url,self.ws.token,body={})[2])["occupied"])

    def test_revoked_terminal_cannot_start_a_new_ssh_transport(self):
        profile = app.clean_profile(dict(host="isolated-test"))
        self.ws.profiles.append(profile)
        record = self.ws.new_session(dict(profile_id=profile["id"], name="revoked fixture"))
        terminal = app.Terminal(self.ws,record,mock.Mock(),80,24)
        terminal.stop()
        with mock.patch.object(app,"open_terminal") as transport:
            terminal.start()
            terminal.open_transport()
            transport.assert_not_called()
        self.assertEqual(record["state"],"new")
