import contextlib
import http.server
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import api
from worker import classify, extract_links, spread_by_host


class ExtractLinksTest(unittest.TestCase):
    def test_resolves_filters_and_dedupes(self):
        page = """
            <a href="/about">About</a>
            <a href="docs/intro.html#setup">Intro</a>
            <a href="docs/intro.html">Intro again</a>
            <a href="#top">Top</a>
            <a href="mailto:me@example.com">Mail</a>
            <a href="javascript:void(0)">Nothing</a>
            <a href=" https://other.example/x ">Other</a>
            <a name="anchor-without-href">No href</a>
            <link href="/style.css" rel="stylesheet">
        """
        self.assertEqual(extract_links(page, "https://example.com/blog/post.html"), [
            "https://example.com/about",
            "https://example.com/blog/docs/intro.html",
            "https://example.com/blog/post.html",
            "https://other.example/x",
        ])

    def test_empty_page(self):
        self.assertEqual(extract_links("", "https://example.com/"), [])


class ClassifyTest(unittest.TestCase):
    def test_outcomes(self):
        cases = {
            200: (True, False),
            301: (True, False),
            404: (False, False),
            410: (False, False),
            429: (False, True),
            500: (False, True),
            503: (False, True),
            None: (False, True),
        }
        for status, want in cases.items():
            with self.subTest(status=status):
                self.assertEqual(classify(status), want)


class SpreadByHostTest(unittest.TestCase):
    def test_spaces_each_host_on_its_own(self):
        urls = ["https://a.test/1", "https://b.test/1", "https://a.test/2", "https://a.test/3", "https://b.test/2"]
        self.assertEqual(spread_by_host(urls, 2), [
            ("https://a.test/1", 0),
            ("https://b.test/1", 0),
            ("https://a.test/2", 2),
            ("https://a.test/3", 4),
            ("https://b.test/2", 2),
        ])

    def test_host_ignores_port_and_case(self):
        offsets = [o for _, o in spread_by_host(["http://A.test:8080/", "http://a.test/"], 1)]
        self.assertEqual(offsets, [0, 1])


class LocalOnlyTest(unittest.TestCase):
    """The API has no auth, so a browser page must not be able to drive it."""

    @classmethod
    def setUpClass(cls):
        # create_site rejects a missing url before it touches the database
        cls.no_db = mock.patch.object(api.db, "connect", lambda: contextlib.nullcontext())
        cls.no_db.start()
        quiet = type("Quiet", (api.Handler,), {"log_message": lambda *a: None})
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), quiet)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/sites"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.no_db.stop()

    def status(self, headers):
        req = urllib.request.Request(self.url, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_rejects_a_foreign_host(self):
        # what a DNS rebinding page sends
        self.assertEqual(self.status({"Host": "evil.example", "Content-Type": "application/json"}), 403)

    def test_rejects_a_non_json_post(self):
        # a form or text/plain POST is what a page can send without a CORS preflight
        self.assertEqual(self.status({"Content-Type": "text/plain"}), 415)

    def test_lets_a_local_json_post_through(self):
        # past the guard it fails on the missing url, not on the guard
        self.assertEqual(self.status({"Content-Type": "application/json"}), 400)


if __name__ == "__main__":
    unittest.main()
