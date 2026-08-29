#!/usr/bin/env python3
"""Small bounded retry helper for transient upstream metadata fetches."""

import random
import time
import urllib.error
import urllib.request


RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}


def read_url(url, *, timeout=30, attempts=4, opener=None, sleeper=None, jitter=None):
    """Read *url*, retrying only transient transport and HTTP failures."""
    opener = opener or urllib.request.urlopen
    sleeper = sleeper or time.sleep
    jitter = jitter or (lambda: random.uniform(0.0, 0.25))
    for attempt in range(attempts):
        try:
            return opener(url, timeout=timeout).read()
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_CODES or attempt + 1 == attempts:
                raise
            exc.close()
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise
        sleeper((2 ** attempt) + jitter())
    raise AssertionError("unreachable")
