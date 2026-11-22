#!/usr/bin/env python3
"""minidb TCP server + client (mkdb's network layer, in Python).

The video's last chapter serves the engine over TCP with a simple
protocol inspired by Redis; mkdb itself uses a length-prefixed wire
format, which is what this server speaks:

    request:  4-byte big-endian length N, then N bytes of UTF-8 SQL
    response: 4-byte big-endian length M, then M bytes of UTF-8 text
              (a result table, a message, or "Error: ...")

One writer at a time, like mkdb: a global lock guards the Database,
and a connection that opened a transaction (BEGIN) holds the lock
until COMMIT / ROLLBACK / disconnect — exactly how mkdb holds its
MutexGuard for the whole transaction. Other clients block on the
lock until then.

Usage:
    python3 server.py [dbpath] [port]     # serve (default demo.db, 8000)
    python3 server.py --client [host] [port]
                                          # interactive client shell
    # any length-prefixed client works; e.g. from Python:
    #   import socket, struct
    #   s = socket.create_connection(("127.0.0.1", 8000))
    #   s.sendall(struct.pack(">I", len(sql)) + sql.encode())
    #   n = struct.unpack(">I", s.recv(4))[0]; print(s.recv(n).decode())
"""
import os
import socket
import struct
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from minidb import Database, execute  # noqa: E402

DEFAULT_PORT = 8000


def _recv_exact(conn, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("client closed the connection")
        buf += chunk
    return buf


def _send_msg(conn, text: str) -> None:
    raw = text.encode("utf-8")
    conn.sendall(struct.pack(">I", len(raw)) + raw)


def _format_result(kind, payload, msg: str) -> str:
    if kind == "rows":
        if not payload:
            lines = ["(0 rows)"]
        else:
            cols = list(payload[0].keys())
            widths = {c: max(len(c), max(len(str(r[c])) for r in payload))
                      for c in cols}
            header = " | ".join(c.ljust(widths[c]) for c in cols)
            lines = [header, "-+-".join("-" * widths[c] for c in cols)]
            lines += [" | ".join(str(r[c]).ljust(widths[c]) for c in cols)
                      for r in payload]
        if msg:
            lines.append(msg)
        return "\n".join(lines)
    return msg or "OK"


class MiniDBServer:
    """Threaded TCP server over one Database + one writer lock."""

    def __init__(self, db_path: str, port: int,
                 cache_size: int = 128):
        self.db = Database(db_path, cache_size=cache_size)
        self.port = port
        self.lock = threading.RLock()
        self._sock = None
        self._running = False

    def handle_client(self, conn, addr):
        # This connection's transaction state: while it holds a
        # transaction open, it also holds the writer lock.
        holding_lock = False
        try:
            while True:
                head = _recv_exact(conn, 4)
                (n,) = struct.unpack(">I", head)
                if n > 10_000_000:
                    raise ValueError("request too large")
                sql = _recv_exact(conn, n).decode("utf-8")
                low = sql.strip().rstrip(";").lower()
                is_begin = low in ("begin", "begin transaction")
                is_end = low in ("commit", "rollback")
                acquired_here = False
                try:
                    if not holding_lock:
                        self.lock.acquire()
                        acquired_here = True
                    kind, payload, msg = execute(self.db, sql)
                    if is_begin:
                        holding_lock = True
                        acquired_here = False  # keep holding the lock
                    elif is_end:
                        # COMMIT/ROLLBACK succeeded: txn over, release.
                        self.lock.release()
                        holding_lock = False
                    response = _format_result(kind, payload, msg)
                except Exception as exc:
                    # A failed statement leaves the transaction (and
                    # the lock) exactly as it was: a failed COMMIT on
                    # an aborted transaction still needs a ROLLBACK.
                    response = f"Error: {exc}"
                finally:
                    if acquired_here:
                        self.lock.release()
                _send_msg(conn, response)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            if holding_lock:
                # Disconnect mid-transaction: roll back, like mkdb.
                try:
                    self.db.rollback()
                except Exception:
                    pass
                self.lock.release()
            try:
                conn.close()
            except OSError:
                pass

    def serve_forever(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(16)
        self._running = True
        print(f"minidb server — database at {self.db.path}, "
              f"listening on 127.0.0.1:{self.port}")
        print("Connect with: python3 server.py --client "
              f"[127.0.0.1] [{self.port}]")
        try:
            while self._running:
                conn, addr = self._sock.accept()
                t = threading.Thread(target=self.handle_client,
                                     args=(conn, addr), daemon=True)
                t.start()
        except KeyboardInterrupt:
            print("\nBye.")
        except OSError:
            pass  # socket closed by stop()
        finally:
            self.stop()

    def stop(self):
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self.db.close()


class MiniDBClient:
    """Minimal interactive client for the length-prefixed protocol."""

    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port))

    def send(self, sql: str) -> str:
        raw = sql.encode("utf-8")
        self.sock.sendall(struct.pack(">I", len(raw)) + raw)
        (n,) = struct.unpack(">I", _recv_exact(self.sock, 4))
        return _recv_exact(self.sock, n).decode("utf-8")

    def repl(self):
        print("minidb client — type SQL, .exit to quit.")
        try:
            while True:
                try:
                    line = input("minidb> ")
                except EOFError:
                    print()
                    break
                if line.strip().lower() in (".exit", ".quit", "exit", "quit"):
                    break
                if not line.strip():
                    continue
                print(self.send(line))
        except KeyboardInterrupt:
            print("\nBye.")
        finally:
            self.sock.close()


def main(argv):
    if argv and argv[0] == "--client":
        host = argv[1] if len(argv) > 1 else "127.0.0.1"
        port = int(argv[2]) if len(argv) > 2 else DEFAULT_PORT
        MiniDBClient(host, port).repl()
        return 0
    db_path = argv[0] if argv else "demo.db"
    port = int(argv[1]) if len(argv) > 1 else DEFAULT_PORT
    MiniDBServer(db_path, port).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
