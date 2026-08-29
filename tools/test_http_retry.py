#!/usr/bin/env python3
import io
import unittest
import urllib.error

from http_retry import read_url


def _http_error(code):
    return urllib.error.HTTPError("https://example.test", code, "boom", {}, None)


class HttpRetryTests(unittest.TestCase):
    def test_retries_transient_http_then_succeeds(self):
        calls = []
        sleeps = []

        def opener(url, timeout=30):
            calls.append(url)
            if len(calls) < 3:
                raise _http_error(503)
            return io.BytesIO(b"ok")

        self.assertEqual(read_url("https://example.test", opener=opener,
                                 sleeper=sleeps.append, jitter=lambda: 0), b"ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [1, 2])

    def test_does_not_retry_ordinary_4xx(self):
        calls = []
        with self.assertRaises(urllib.error.HTTPError) as caught:
            read_url("https://example.test", opener=lambda *a, **k: calls.append(1) or
                     (_ for _ in ()).throw(_http_error(404)), sleeper=lambda _: None)
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()
        self.assertEqual(len(calls), 1)

    def test_retries_timeout_and_exhausts(self):
        calls = []
        with self.assertRaises(TimeoutError):
            read_url("https://example.test", attempts=3,
                     opener=lambda *a, **k: calls.append(1) or
                     (_ for _ in ()).throw(TimeoutError("slow")),
                     sleeper=lambda _: None, jitter=lambda: 0)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
