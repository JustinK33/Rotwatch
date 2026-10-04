import unittest

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


if __name__ == "__main__":
    unittest.main()
