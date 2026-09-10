import http.server
import importlib.util
import os
import pathlib
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "github_artifact_bus", ROOT / "scripts" / "github_artifact_bus.py"
)
BUS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUS)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api":
            if self.headers.get("Authorization") != "Bearer test-token":
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/blob")
            self.end_headers()
            return
        if self.path == "/blob" and self.headers.get("Authorization") is None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"artifact bytes")
            return
        self.send_response(401)
        self.end_headers()

    def log_message(self, _format, *_args):
        pass


class ArtifactRedirectTests(unittest.TestCase):
    def test_bearer_is_not_forwarded_to_signed_blob(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        previous = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "test-token"
        try:
            body = BUS.download_archive(f"http://127.0.0.1:{server.server_port}/api")
            self.assertEqual(body, b"artifact bytes")
        finally:
            if previous is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = previous
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
