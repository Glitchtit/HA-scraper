"""Minimal ingress web server for the Grocy Scraper add-on."""

import os
from http.server import HTTPServer, BaseHTTPRequestHandler

_PORT = int(os.environ.get("INGRESS_PORT", "8099"))

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Grocy Scraper</title>
  <style>
    body { font-family: sans-serif; margin: 2rem; background: #fafafa; color: #333; }
    h1 { color: #03a9f4; }
    .card { background: #fff; border-radius: 8px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,.12); max-width: 600px; }
    p { line-height: 1.6; }
    code { background: #e8e8e8; padding: 2px 6px; border-radius: 4px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>&#128722; Grocy Scraper</h1>
    <p>The add-on is <strong>running</strong>. It periodically discovers products
       from K-Ruoka and syncs them to your Grocy database.</p>
    <p>Configure the add-on from the <em>Configuration</em> tab on its
       add-on info page.</p>
  </div>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_HTML.encode())

    def log_message(self, format, *args):  # noqa: A002
        pass  # silence per-request logs


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", _PORT), _Handler)
    print(f"Ingress server listening on port {_PORT}")
    server.serve_forever()
