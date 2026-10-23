"""Loopback-only TCP bridge for active per-recording noVNC viewers."""
from __future__ import annotations

import select
import socket
import socketserver
import sys
import threading


class BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = None
        try:
            upstream = socket.create_connection(
                (self.server.target_host, self.server.target_port), timeout=5
            )
            self.request.settimeout(None)
            sockets = (self.request, upstream)
            while True:
                readable, _, exceptional = select.select(sockets, (), sockets, 60)
                if exceptional:
                    return
                if not readable:
                    continue
                for source in readable:
                    payload = source.recv(65536)
                    if not payload:
                        return
                    destination = upstream if source is self.request else self.request
                    destination.sendall(payload)
        except (ConnectionError, OSError):
            return
        finally:
            if upstream:
                upstream.close()


class BridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    ports = [int(value) for value in sys.argv[1:]]
    if not ports or any(port < 6081 or port > 6090 for port in ports):
        raise SystemExit("Provide one or more viewer ports from 6081 through 6090")

    servers = []
    for port in ports:
        server = BridgeServer(("0.0.0.0", port), BridgeHandler)
        server.target_host = "xsa-app"
        server.target_port = port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        print(f"Loopback viewer bridge listening on {port} -> xsa-app:{port}", flush=True)

    threading.Event().wait()


if __name__ == "__main__":
    main()
