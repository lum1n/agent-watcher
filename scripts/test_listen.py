#!/usr/bin/env python3
"""Socket fan-out: client gets hello; socket stop does not kill the daemon."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHER = os.path.join(ROOT, "src", "agent_watcher.py")


def recv_line(conn, timeout=3.0):
    conn.settimeout(timeout)
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            raise RuntimeError("socket closed before a line")
        buf += chunk
    line, _ = buf.split(b"\n", 1)
    return json.loads(line.decode())


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="agent-watcher-")
    sock_path = os.path.join(tmp, "w.sock")
    proc = subprocess.Popen(
        [sys.executable, "-u", WATCHER, "--listen", sock_path],
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.time() + 4
        while time.time() < deadline and not os.path.exists(sock_path):
            time.sleep(0.05)
        if not os.path.exists(sock_path):
            print("FAIL  listen socket never appeared")
            return 1
        mode = os.stat(sock_path).st_mode & 0o777
        if mode != 0o700:
            print("FAIL  socket mode %o want 0700" % mode)
            return 1
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(3)
        conn.connect(sock_path)
        ev = recv_line(conn)
        if ev.get("type") != "hello":
            print("FAIL  first event %s" % ev)
            return 1
        conn.sendall(b'{"cmd":"stop"}\n')
        time.sleep(0.3)
        if proc.poll() is not None:
            print("FAIL  socket stop killed the watcher")
            return 1
        print("ok  listen hello + socket stop is disconnect")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
