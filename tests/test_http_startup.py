"""Browser asset bursts against an isolated local backend, without SSH."""
import http.client
from pathlib import Path
import socket
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class HTTPStartupTests(unittest.TestCase):
    def test_asset_connections_survive_accept_loop_startup_delay(self):
        server = app.Server(0, None)
        clients = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            # Hold off accepting until both pages' connection burst is queued.
            # The old backlog of five rejects / times out later connections.
            for _ in range(16):
                clients.append(socket.create_connection(server.server_address, timeout=1))
            thread.start()
            for client in clients:
                client.settimeout(5)
                client.sendall(("GET /vendor/addon-serialize.js HTTP/1.1\r\n"
                                "Host: 127.0.0.1:%s\r\nConnection: close\r\n\r\n" %
                                server.server_port).encode())
                response = http.client.HTTPResponse(client)
                response.begin()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "application/javascript; charset=utf-8")
                self.assertIn(b"SerializeAddon", response.read())
                response.close()
        finally:
            for client in clients:
                client.close()
            if thread.is_alive():
                server.shutdown()
                thread.join(5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
