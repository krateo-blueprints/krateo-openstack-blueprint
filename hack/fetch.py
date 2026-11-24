#!/usr/bin/env python3
"""Download a pinned file for a gate, trying again when the server says "not now" (#463).

    from fetch import get; body = get(url)
    hack/fetch.py --self-test

A gate's verdict must not depend on one request: raw.githubusercontent.com answered 503 (Backend.max_conn reached) to a gate's
only request three times on 2026-11-23 and 24, and failed pull requests that had nothing to do with it. 429, 5xx and network
errors are tried again, ATTEMPTS times in all, with a growing pause. Any other answer (a 404: the pin is wrong) fails at
once, and the last error is raised, never swallowed: a gate that cannot read its input still fails.
"""
import sys
import time
import urllib.error
import urllib.request

ATTEMPTS = 5
PAUSES = (3, 10, 20, 40)


def retryable(e):
    if isinstance(e, urllib.error.HTTPError):
        return e.code == 429 or e.code >= 500
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError))


def get(url, timeout=60, opener=urllib.request.urlopen, sleep=time.sleep):
    for n in range(ATTEMPTS):
        try:
            with opener(url, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            if n == ATTEMPTS - 1 or not retryable(e):
                raise
            print("fetch: %s: %s; trying again in %d s" % (url, e, PAUSES[n]), file=sys.stderr, flush=True)
            sleep(PAUSES[n])


def self_test():
    class Body:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    def run(answers):
        seen, slept, answers = [], [], list(answers)

        def opener(url, timeout):
            a = answers.pop(0)
            seen.append(a)
            if a == "ok":
                return Body()
            raise a if isinstance(a, Exception) else urllib.error.HTTPError(url, a, "x", {}, None)
        try:
            return get("u", opener=opener, sleep=slept.append), len(seen), len(slept)
        except Exception as e:
            return type(e).__name__ + str(getattr(e, "code", "")), len(seen), len(slept)
    cases = [
        ("first answer", run(["ok"]), (b"ok", 1, 0)),
        ("503 twice, then the file", run([503, 503, "ok"]), (b"ok", 3, 2)),
        ("429", run([429, "ok"]), (b"ok", 2, 1)),
        ("a network error", run([urllib.error.URLError("reset"), "ok"]), (b"ok", 2, 1)),
        ("a 404 fails at once", run([404, "ok"]), ("HTTPError404", 1, 0)),
        ("503 every time fails", run([503] * ATTEMPTS), ("HTTPError503", ATTEMPTS, ATTEMPTS - 1)),
        ("a program error is not retried", run([ValueError("x"), "ok"]), ("ValueError", 1, 0)),
    ]
    wrong = [c for c in cases if c[1] != c[2]]
    for n, got, want in wrong:
        print("SELF-TEST FAIL: %s: %s, want %s" % (n, got, want))
    print("self-test: %d case(s), %d wrong" % (len(cases), len(wrong)))
    return 1 if wrong or len(PAUSES) != ATTEMPTS - 1 else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else "usage: hack/fetch.py --self-test")
