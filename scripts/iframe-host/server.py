#!/usr/bin/env python3
"""Serve any page from localhost so it can be framed, adapted, and read.

Built for the Assistiv Labs setup: a hosted Windows VM running a real screen
reader, which can see the tester's machine only through a localhost tunnel. The
screen reader reads whatever is in *its* browser, so the page under test has to
be rendered there — which rules out CDP, because the harness drives a Chrome on
this machine that the VM cannot see.

So control moves into the page. This proxy fetches a URL, serves it from
localhost, and injects two things: the toolkit's adapter bundle, and a bridge
that lets the surrounding page drive it by postMessage. The screen reader then
has the chat and the page under test in one accessibility tree, and the
adaptations are applied by code running inside the page rather than over a wire.

    python3 server.py --port 8124
    open http://127.0.0.1:8124/

Only the top-level HTML is proxied. A <base> tag points everything else at the
real origin, so images, CSS and scripts load from where they always did — fewer
moving parts, and a page that behaves more like itself.
"""
import argparse
import gzip
import io
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
BUNDLE = HERE.parent.parent / "src" / "browser_harness" / "a11y" / "bundle.js"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

# Dropped rather than forwarded. Framing headers are the whole reason a page
# cannot be put in an iframe, and this proxy exists to put pages in an iframe.
# Encoding and length are dropped because the body is rewritten on the way past.
STRIP = {"x-frame-options", "content-security-policy",
         "content-security-policy-report-only", "content-encoding",
         "content-length", "transfer-encoding", "connection",
         "strict-transport-security", "cross-origin-opener-policy",
         "cross-origin-embedder-policy"}


def decompress(raw, encoding):
    if encoding == "gzip":
        return gzip.decompress(raw)
    if encoding == "deflate":
        return zlib.decompress(raw, -zlib.MAX_WBITS)
    if encoding == "br":
        try:
            import brotli
            return brotli.decompress(raw)
        except Exception:
            return raw  # asked for identity; a server ignoring that is its own answer
    return raw


def inject(html, url, origin):
    """Give the page a base, the adapters, and the bridge.

    The script URLs are absolute on purpose. A relative one would be resolved
    against the <base> we just set — which points at the site being proxied —
    so the page loaded perfectly and quietly fetched its adapters from the wrong
    origin, where they do not exist.
    """
    base = f'<base href="{urllib.parse.quote(url, safe=":/?&=#%")}">'
    tags = (base +
            f'<script src="{origin}/_bundle.js"></script>'
            f'<script src="{origin}/_bridge.js"></script>')
    # After <head> when there is one, before everything when there is not. A
    # <base> that lands after the first relative <link> is too late for it.
    m = re.search(r"<head[^>]*>", html, re.I)
    if m:
        return html[:m.end()] + tags + html[m.end():]
    m = re.search(r"<html[^>]*>", html, re.I)
    if m:
        return html[:m.end()] + tags + html[m.end():]
    return tags + html


class Handler(BaseHTTPRequestHandler):
    server_version = "a11y-iframe-host"

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Same-origin as the host page by construction, so the frame is
        # scriptable and the screen reader sees one document tree.
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if route == "/":
            return self._send(200, (HERE / "host.html").read_text())
        if route == "/_bridge.js":
            return self._send(200, (HERE / "bridge.js").read_text(),
                              "application/javascript; charset=utf-8")
        if route == "/_bundle.js":
            if not BUNDLE.exists():
                return self._send(500, "// bundle not built: python3 scripts/build_a11y.py",
                                  "application/javascript; charset=utf-8")
            return self._send(200, BUNDLE.read_text(),
                              "application/javascript; charset=utf-8")
        if route == "/go":
            q = urllib.parse.parse_qs(parsed.query)
            url = (q.get("url") or [""])[0]
            if not url:
                return self._send(400, "<p>Pass ?url=https://…</p>")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            return self.proxy(url)
        self.send_error(404)

    def proxy(self, url):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
        })
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read()
                headers = dict(r.headers)
                final = r.geturl()          # after redirects — the base must be the real one
                status = r.status
        except urllib.error.HTTPError as e:
            raw, headers, final, status = e.read(), dict(e.headers), url, e.code
        except Exception as e:
            return self._send(502, f"<h2>Could not fetch</h2><p>{url}</p><pre>{e}</pre>")

        ctype = headers.get("Content-Type", "text/html")
        body = decompress(raw, (headers.get("Content-Encoding") or "").lower())

        if "html" not in ctype.lower():
            # Not a document — hand it back untouched, minus the framing headers.
            pass_through = {k: v for k, v in headers.items() if k.lower() not in STRIP}
            return self._send(status, body, ctype, pass_through)

        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        if m:
            charset = m.group(1)
        html = body.decode(charset, errors="replace")
        origin = "http://" + (self.headers.get("Host") or "127.0.0.1:8124")
        self._send(status, inject(html, final, origin), "text/html; charset=utf-8")

    def log_message(self, fmt, *a):
        sys.stderr.write("[iframe-host] " + (fmt % a) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8124)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"[iframe-host] http://{args.host}:{args.port}/")
    print(f"[iframe-host] adapters: {'built' if BUNDLE.exists() else 'MISSING — run scripts/build_a11y.py'}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
