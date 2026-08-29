"""Real-browser check of the response interception fetch.py relies on.

Reading a response body inside a sync Playwright event handler is the part
most likely to break, so we exercise it against a local server that imitates
the Likes GraphQL call rather than trusting it by inspection.
"""

import json
import os
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

playwright_api = pytest.importorskip("playwright.sync_api")

from xlikes.fetch import _discover_handle, ingest, is_likes_response  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "likes_response.json").read_text())

PAGE = b"""<!doctype html><title>likes</title><body><div id=s>loading</div>
<script>
fetch('/i/api/graphql/aBc123/Likes?variables=%7B%7D')
  .then(r => r.json()).then(() => { document.getElementById('s').textContent = 'done'; });
</script></body>"""


# The two shapes of logged-in nav we read a handle from.
PROFILE_LINK_PAGE = b"""<!doctype html><title>x</title><body>
<nav><a data-testid="AppTabBar_Profile_Link" href="/sha_manav" role="link">Profile</a></nav>
</body>"""

SWITCHER_PAGE = b"""<!doctype html><title>x</title><body>
<div data-testid="SideNav_AccountSwitcher_Button">Manav Shah<br>@sha_manav</div>
</body>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/profile-link"):
            body, ctype = PROFILE_LINK_PAGE, "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/switcher"):
            body, ctype = SWITCHER_PAGE, "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if is_likes_response(self.path):
            body = json.dumps(FIXTURE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = PAGE
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _chrome_path():
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    if root.is_dir():
        for exe in sorted(root.glob("chromium-*/chrome-linux/chrome")):
            return str(exe)
    return None


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/"
    httpd.shutdown()


def test_handler_captures_likes_json_from_a_live_page(server, tmp_path):
    collected: dict = {}
    errors: list = []

    def on_response(response):
        if not is_likes_response(response.url):
            return
        try:
            ingest(collected, response.json())
        except Exception as exc:  # the sync-handler gotcha would land here
            errors.append(repr(exc))

    launch = {"headless": True}
    if path := _chrome_path():
        launch["executable_path"] = path

    with playwright_api.sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(str(tmp_path / "profile"), **launch)
        except Exception as exc:
            pytest.skip(f"no usable chromium: {exc}")
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", on_response)
        page.goto(server, wait_until="domcontentloaded")
        page.wait_for_selector("#s:text('done')", timeout=15000)
        page.wait_for_timeout(300)
        context.close()

    assert not errors, f"reading the response body failed: {errors}"
    assert list(collected) == [
        "1900000000000000010",
        "1900000000000000011",
        "1900000000000000012",
    ]
    assert collected["1900000000000000010"]["quoted_handle"] == "swyx"


@pytest.mark.parametrize("route,expected", [("profile-link", "sha_manav"), ("switcher", "sha_manav")])
def test_handle_is_read_from_real_logged_in_markup(server, tmp_path, route, expected):
    """Both nav shapes must yield the handle; which one renders depends on
    window size and on whichever markup X is shipping."""
    launch = {"headless": True}
    if path := _chrome_path():
        launch["executable_path"] = path

    with playwright_api.sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(str(tmp_path / route), **launch)
        except Exception as exc:
            pytest.skip(f"no usable chromium: {exc}")
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(server + route, wait_until="domcontentloaded")
        handle = _discover_handle(page, attempts=1)
        context.close()

    assert handle == expected
