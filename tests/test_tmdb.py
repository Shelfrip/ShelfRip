"""
test_tmdb.py - your own TMDB key, or Shelfrip's shared one through the disc database server.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discs  # noqa: E402

discs.setup_env({"tmdb_api_key": ""})
import identify  # noqa: E402
import paths  # noqa: E402


class Choice(unittest.TestCase):
    def test_own_key_first(self):
        t = identify.tmdb_for({"tmdb_api_key": " abc123 "})
        self.assertEqual((t.key, t.base, t.shared), ("abc123", identify.TMDB.BASE, False))

    def test_shared_without_a_key(self):
        for key in ("", "paste your key here"):
            t = identify.tmdb_for({"tmdb_api_key": key})
            self.assertTrue(t.shared)
            self.assertEqual(t.base, paths.COMMUNITY_URL.rstrip("/") + "/v1/tmdb")
        t = identify.tmdb_for({"community_url": "http://db.test/"})
        self.assertEqual(t.base, "http://db.test/v1/tmdb")

    def test_off(self):
        self.assertIsNone(identify.tmdb_for({"shared_tmdb": False}))
        with mock.patch.object(paths, "COMMUNITY_URL", ""):
            self.assertIsNone(identify.tmdb_for({}))


class Requests(unittest.TestCase):
    def fetch(self, tmdb, path, **params):
        seen = []

        def fake(req, timeout=0):
            seen.append(req)
            return io.BytesIO(json.dumps({"ok": 1}).encode())
        with mock.patch.object(urllib.request, "urlopen", fake):
            self.assertEqual(tmdb.get(path, **params), {"ok": 1})
        return seen[0]

    def test_shared_sends_no_key(self):
        req = self.fetch(identify.TMDB("", base="http://db.test/v1/tmdb"), "/search/movie", query="The Matrix")
        self.assertEqual(req.full_url, "http://db.test/v1/tmdb/search/movie?query=The+Matrix")
        self.assertIsNone(req.get_header("Authorization"))
        req = self.fetch(identify.TMDB("", base="http://db.test/v1/tmdb"), "/movie/603")
        self.assertEqual(req.full_url, "http://db.test/v1/tmdb/movie/603")

    def test_own_key(self):
        req = self.fetch(identify.TMDB("abc"), "/movie/603")
        self.assertEqual(req.full_url, "https://api.themoviedb.org/3/movie/603?api_key=abc")
        req = self.fetch(identify.TMDB("eyJtoken"), "/movie/603")
        self.assertEqual(req.full_url, "https://api.themoviedb.org/3/movie/603")
        self.assertEqual(req.get_header("Authorization"), "Bearer eyJtoken")


class Check(unittest.TestCase):
    def test_check_sends_shelfrip_agent(self):
        """Cloudflare (the shared key's server) refuses Python's default User-Agent, so the
        Settings check said "TMDB answered 403" while lookups worked."""
        import health
        seen = []
        def fake(req, timeout=0):
            seen.append(req)
            return io.BytesIO(b"{}")
        with mock.patch.object(urllib.request, "urlopen", fake):
            res = health.tmdb_check("", wait=5, base="http://db.test/v1/tmdb/agent-check")
        self.assertEqual(res["detail"], "using Shelfrip's shared key")
        self.assertEqual(seen[0].get_header("User-agent"), paths.USER_AGENT)


if __name__ == "__main__":
    unittest.main()
