"""Talk to a running Odon's control bridge (127.0.0.1:17870, one JSON line
per request) -- block A9 §39 benchmark helper.
Usage: a9_odon_ctl.py METHOD [PARAMS_JSON]"""
import json
import socket
import sys


def call(method, params=None, timeout=10.0):
    with socket.create_connection(("127.0.0.1", 17870), timeout=timeout) as s:
        s.sendall((json.dumps({"method": method, "params": params or {}}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


if __name__ == "__main__":
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(call(sys.argv[1], params))[:2000])
