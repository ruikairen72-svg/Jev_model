#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证代理能透传 WebSocket（App 的 realtime /live 走这条路）。不联网、不需要 key。

用一个假 WS 上游（回 101 然后 echo）+ 真代理实例（8789）+ 裸 socket 客户端。
"""
import os
import socket
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

FAKE_PORT = 9998
PROXY_PORT = 8789

os.environ["JEVPROXY_UPSTREAM"] = "http://127.0.0.1:%d" % FAKE_PORT
os.environ["JEVPROXY_PORT"] = str(PROXY_PORT)
os.environ["JEVPROXY_HOST"] = "127.0.0.1"
os.environ["JEVPROXY_LOG"] = os.path.join(HERE, "ws_test_proxy.log")
os.environ["JEVPROXY_PIDFILE"] = os.path.join(HERE, "ws_test.pid")

import jev_proxy as P  # noqa: E402


def fake_upstream():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", FAKE_PORT))
    srv.listen(5)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle_upstream, args=(conn,), daemon=True).start()


def handle_upstream(conn):
    try:
        fh = conn.makefile("rb")
        req_line = fh.readline()
        hdrs = {}
        while True:
            line = fh.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode("latin-1").partition(":")
            hdrs[k.strip().lower()] = v.strip()
        ok = (hdrs.get("upgrade", "").lower() == "websocket"
              and "upgrade" in hdrs.get("connection", "").lower())
        with open(os.path.join(HERE, "ws_upstream_seen.txt"), "a") as log:
            log.write("%s upgrade=%s path=%s\n" % (
                req_line.decode("latin-1").strip(), ok, hdrs.get("host")))
        if not ok:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                     b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: dummy\r\n\r\n")
        while True:
            data = conn.recv(4096)
            if not data:
                break
            conn.sendall(b"echo:" + data)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    threading.Thread(target=fake_upstream, daemon=True).start()
    server = P.Server((P.HOST, P.PORT), P.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    import time
    time.sleep(0.4)

    ok = True
    sock = socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=15)
    sock.sendall(
        b"GET /live HTTP/1.1\r\n"
        b"Host: 127.0.0.1:%d\r\n"
        b"Connection: Upgrade\r\n"
        b"Upgrade: websocket\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        b"Sec-WebSocket-Version: 13\r\n"
        b"Authorization: Bearer fake-token\r\n\r\n" % PROXY_PORT)
    resp = sock.recv(4096)
    status = resp.split(b"\r\n")[0].decode("latin-1")
    print("1) 握手响应:", status)
    ok &= "101" in status
    ok &= b"Upgrade: websocket" in resp or b"upgrade: websocket" in resp.lower() or b"Upgrade" in resp

    sock.sendall(b"ping-123")
    data = sock.recv(4096)
    print("2) 双向数据:", data)
    ok &= data == b"echo:ping-123"
    sock.close()

    seen = open(os.path.join(HERE, "ws_upstream_seen.txt")).read().strip().splitlines()[-1]
    print("3) 上游看到的请求:", seen)
    ok &= "upgrade=True" in seen
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
