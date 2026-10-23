import os
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import fcgi_probe  # noqa: E402


def serve(listener, payload):
    connection, _ = listener.accept()
    try:
        seen = b""
        while True:
            header = fcgi_probe.read_exact(connection, 8)
            _, kind, _, length, padding, _ = struct.unpack("!BBHHBB", header)
            fcgi_probe.read_exact(connection, length + padding)
            seen += header[1:2]
            if kind == fcgi_probe.STDIN and length == 0:
                break
        connection.sendall(fcgi_probe.record(fcgi_probe.STDOUT, payload)
                           + fcgi_probe.record(fcgi_probe.STDOUT)
                           + fcgi_probe.record(fcgi_probe.END, struct.pack("!IB3x", 0, 0)))
    finally:
        connection.close()


class ProbeTests(unittest.TestCase):
    def run_probe(self, payload, *extra):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        thread = threading.Thread(target=serve, args=(listener, payload))
        thread.start()
        try:
            return fcgi_probe.main(["--port", str(listener.getsockname()[1]), "--script", "/srv/health.php",
                                    "--timeout", "5"] + list(extra))
        finally:
            thread.join()
            listener.close()

    def test_success_requires_status_and_markers(self):
        body = b"Content-Type: text/plain\r\n\r\nshift8-ok\next=mysqli\n"
        self.assertEqual(self.run_probe(body, "--expect", "shift8-ok", "--expect", "ext=mysqli"), 0)

    def test_missing_marker_fails(self):
        body = b"Content-Type: text/plain\r\n\r\nshift8-ok\n"
        self.assertEqual(self.run_probe(body, "--expect", "ext=mysqli"), 3)

    def test_fpm_error_status_fails(self):
        body = b"Status: 404 Not Found\r\nContent-Type: text/plain\r\n\r\nFile not found.\n"
        self.assertEqual(self.run_probe(body), 3)

    def test_connection_failure_fails(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        self.assertEqual(fcgi_probe.main(["--port", str(port), "--script", "/x.php", "--timeout", "1"]), 2)

    def test_relative_script_is_rejected(self):
        with self.assertRaises(SystemExit):
            fcgi_probe.main(["--port", "1", "--script", "health.php"])


if __name__ == "__main__":
    unittest.main()
