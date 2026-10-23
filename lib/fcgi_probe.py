#!/usr/bin/env python
"""Run a PHP script through a real FPM listener and check its response.

An application-independent health probe for managed deployments: it speaks
FastCGI directly, so it works when no web server is running or routing is
unrelated to the PHP runtime. Exit zero only when the response status is 200
and every --expect marker is present in the body.
"""
from __future__ import print_function

import argparse
import socket
import struct
import sys

BEGIN, END, PARAMS, STDIN, STDOUT, STDERR = 1, 3, 4, 5, 6, 7
REQUEST_ID = 1


def record(kind, content=b""):
    return struct.pack(str("!BBHHBB"), 1, kind, REQUEST_ID, len(content), 0, 0) + content


def pair(name, value):
    def size(text):
        return struct.pack(str("!B"), len(text)) if len(text) < 128 else struct.pack(str("!I"), len(text) | 0x80000000)
    name, value = name.encode("utf-8"), value.encode("utf-8")
    return size(name) + size(value) + name + value


def read_exact(connection, count):
    data = b""
    while len(data) < count:
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise IOError("FastCGI connection closed early")
        data += chunk
    return data


def request(address, port, script, timeout):
    parameters = {
        "SCRIPT_FILENAME": script, "SCRIPT_NAME": "/" + script.rsplit("/", 1)[-1], "REQUEST_METHOD": "GET",
        "REQUEST_URI": "/" + script.rsplit("/", 1)[-1], "QUERY_STRING": "", "SERVER_PROTOCOL": "HTTP/1.1",
        "GATEWAY_INTERFACE": "CGI/1.1", "SERVER_NAME": "localhost", "SERVER_PORT": "80",
        "REMOTE_ADDR": "127.0.0.1", "REDIRECT_STATUS": "200",
    }
    body = b"".join(pair(key, value) for key, value in sorted(parameters.items()))
    connection = socket.create_connection((address, port), timeout)
    try:
        connection.sendall(record(BEGIN, struct.pack(str("!HB5x"), 1, 0)) + record(PARAMS, body)
                           + record(PARAMS) + record(STDIN))
        output, errors = b"", b""
        while True:
            version, kind, identifier, length, padding, _ = struct.unpack(str("!BBHHBB"), read_exact(connection, 8))
            content = read_exact(connection, length)
            read_exact(connection, padding)
            if kind == STDOUT:
                output += content
            elif kind == STDERR:
                errors += content
            elif kind == END:
                return output, errors
    finally:
        connection.close()


def parse(output):
    head, _, body = output.partition(b"\r\n\r\n")
    status = 200
    for line in head.decode("latin-1").split("\r\n"):
        if line.lower().startswith("status:"):
            status = int(line.split(":", 1)[1].split()[0])
    return status, body.decode("utf-8", "replace")


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--script", required=True, help="absolute path readable by the pool user")
    parser.add_argument("--expect", action="append", default=[], help="text that must appear in the body")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    if not args.script.startswith("/"):
        parser.error("--script must be absolute")
    try:
        output, errors = request(args.address, args.port, args.script, args.timeout)
        status, body = parse(output)
    except (IOError, OSError, ValueError, struct.error) as error:
        print("probe failed: " + str(error), file=sys.stderr)
        return 2
    missing = [item for item in args.expect if item not in body]
    if status != 200 or missing:
        print("probe failed: status={} missing={} stderr={}".format(status, missing, errors[:200]), file=sys.stderr)
        return 3
    print("probe ok: {}:{} {}".format(args.address, args.port, args.script))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
