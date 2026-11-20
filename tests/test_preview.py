from __future__ import annotations

import http.client
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ai_team.server import make_server


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        package = self.root / "package"
        package.mkdir()
        web = package / "web"
        web.mkdir()
        self.package = package
        self.preview = web / "modern"
        (web / "chat.html").write_text('<a href="/chat/preview" __MODERN_CHAT_LINK_HIDDEN__>Chat Preview</a>', encoding="utf-8")
        (web / "index.html").write_text('<a href="__CHAT_MODE_URL__">Classic Code</a>', encoding="utf-8")
        with patch("ai_team.server.files", return_value=package):
            self.server = make_server(self.root, 0, 'quoted"&token', chat_only=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def get(self, path, headers=None, *, server=None):
        with closing(http.client.HTTPConnection("127.0.0.1", (server or self.server).server_port, timeout=5)) as connection:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")

    def test_preview_is_optional_for_a_python_only_installation(self):
        status, _, page = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('<a href="/chat/preview" hidden>', page)
        self.assertEqual(self.get("/chat/preview")[0], 404)

    def test_built_preview_serves_only_local_assets_and_keeps_api_protection(self):
        assets = self.preview / "assets"
        assets.mkdir(parents=True)
        (self.preview / "index.html").write_text(
            '<meta name="ai-team-token" content="__AI_TEAM_TOKEN__">'
            '<meta name="ai-team-code-mode" content="__AI_TEAM_CODE_MODE__">', encoding="utf-8")
        (assets / "index-123xyz.js").write_text('console.log("loaded");', encoding="utf-8")
        (self.preview / "notes.txt").write_text("private fixture", encoding="utf-8")

        status, headers, page = self.get("/chat/preview")
        self.assertEqual(status, 200)
        self.assertIn("quoted&quot;&amp;token", page)
        self.assertIn('name="ai-team-code-mode" content="disabled"', page)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn('<a href="/chat/preview" >', self.get("/chat")[2])

        path = "/chat/preview/assets/index-123xyz.js"
        status, headers, asset = self.get(path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertNotIn("quoted", asset)
        self.assertEqual(self.get(path, {"Host": "other.invalid"})[0], 403)
        self.assertEqual(self.get("/chat/preview/assets/../notes.txt")[0], 404)
        self.assertEqual(self.get("/api/v1/sessions")[0], 403)

    def test_complete_bundle_selects_the_default_workspace_and_keeps_classic_routes(self):
        self.preview.mkdir()
        (self.preview / "index.html").write_text(
            '<meta name="mode" content="__AI_TEAM_DEFAULT_MODE__">'
            '<meta name="code" content="__AI_TEAM_CODE_MODE__">', encoding="utf-8")
        self.assertIn('name="mode" content="chat"', self.get("/")[2])
        self.assertIn('name="code" content="disabled"', self.get("/")[2])
        self.assertIn("Chat Preview", self.get("/classic")[2])

        with patch("ai_team.server.files", return_value=self.package):
            code = make_server(self.root, 0, 'test-token')
        thread = threading.Thread(target=code.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertIn('name="mode" content="code"', self.get("/", server=code)[2])
            self.assertIn('name="code" content="enabled"', self.get("/", server=code)[2])
            for path in ("/chat", "/chat/preview"):
                self.assertIn('name="mode" content="chat"', self.get(path, server=code)[2])
            self.assertIn('<a href="/classic/chat">Classic Code</a>', self.get("/classic", server=code)[2])
            self.assertIn("Chat Preview", self.get("/classic/chat", server=code)[2])
            self.assertEqual(self.get("/classic", {"Host": "other.invalid"}, server=code)[0], 403)
        finally:
            code.shutdown()
            code.server_close()
            thread.join(timeout=2)
