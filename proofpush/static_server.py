"""
Static file server for audit copies of static sites: `python -m proofpush.static_server <port>`.

Same as `python -m http.server <port> --bind 127.0.0.1`, minus one thing: http.server looks up the host's
fully qualified name (socket.getfqdn, a reverse-DNS query) after binding and before listening. Where reverse
DNS is slow (seen on macOS CI: 15-30+ s) the server isn't listening yet when ProofPush's health check runs.
"""

from __future__ import annotations

import socketserver
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class LoopbackServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup in server_bind."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)  # bind only; HTTPServer.server_bind would also call socket.getfqdn
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def make_server(port: int, directory: str = ".") -> LoopbackServer:
    return LoopbackServer(("127.0.0.1", port), partial(SimpleHTTPRequestHandler, directory=directory))


if __name__ == "__main__":
    make_server(int(sys.argv[1])).serve_forever()
