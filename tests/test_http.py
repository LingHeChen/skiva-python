"""Real socket-level HTTP round trip; no external server or credentials."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from skiva_sdk import SkivaClient


async def test_real_http_distribution_roundtrip(distribution):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer test-secret":
                self.send_error(403)
                return
            parsed = urlsplit(self.path)
            version = parse_qs(parsed.query).get("snapshotId", [distribution.head])[0]
            if parsed.path.endswith("/manifest"):
                data = json.dumps(distribution.snapshots[version]).encode()
            else:
                data = distribution.blobs[parsed.path.split("/objects/")[1]]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with SkivaClient(f"http://127.0.0.1:{server.server_port}", "test-secret") as sdk:
            async with sdk.bind(consistency="dynamic") as skills:
                assert (await skills.load_skill("review")).ref.snapshot_id == "A"
                distribution.head = "B"
                await skills.refresh()
                assert (await skills.load_skill("review")).ref.snapshot_id == "B"
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        await asyncio.to_thread(thread.join)
