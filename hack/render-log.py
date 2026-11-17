#!/usr/bin/env python3
"""Render docs/log.md from docs/log.d/, one file per change.

    hack/render-log.py [repo-root]           write docs/log.md
    hack/render-log.py [repo-root] --check   fail if docs/log.md is not what docs/log.d renders

Every pull request used to add its entry at the top of docs/log.md, so any two branches in flight conflicted there.
Now a change adds one file, docs/log.d/<yyyy-mm-dd>-<nn>-<slug>.md, and this script writes docs/log.md, which stays
checked in because people read it on GitHub. Never edit docs/log.md by hand.

An entry file is OKF front matter (the docs standard's lint-docs reads every docs/**/*.md) and then its bullet:

    ---
    type: Log
    title: <the entry's title>
    description: <one line>
    ---
    - **Title (#issue).** What changed and why ...

Relative links in an entry are written from docs/log.d/ (`../supply-chain.md`), so they resolve where the file is,
and are rebased to docs/ when rendered into docs/log.md (`supply-chain.md`).

docs/log.md is docs/log.d/_header.md (front matter included: it is log.md's), then one "## yyyy-mm" section per month,
newest first, then the body of docs/log.d/_earlier.md (the history from before dated entries, kept verbatim). Within a
month, entries sort newest first by file name: the date, then <nn>, so on one day the higher number is the later
change. Entries are separated by one blank line.

--check fails, naming the problem, when docs/log.md differs from the render, when a file in docs/log.d is neither one
of the two fixed parts nor named <yyyy-mm-dd>-<nn>-<slug>.md, when an entry lacks the front matter or does not start
with "- **", and when there is no entry at all (no input is not a pass).
"""
import difflib
import os
import re
import sys

args = [a for a in sys.argv[1:] if not a.startswith("--")]
CHECK = "--check" in sys.argv[1:]
ROOT = os.path.abspath(args[0] if args else ".")
DOCS = os.path.join(ROOT, "docs")
LOGD = os.path.join(DOCS, "log.d")
OUT = os.path.join(DOCS, "log.md")
NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
FIXED = ("_header.md", "_earlier.md")
LINK = re.compile(r"(\]\()([^)\s]+)(\))")


def split_front_matter(text):
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return None, text
    fm = {}
    for line in text[4:end].splitlines():
        m = re.match(r"^([\w-]+):\s*(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
    return fm, text[end + 5:]


def rebase(text):
    """Links written from docs/log.d/, rewritten from docs/."""
    def one(m):
        target = m.group(2)
        if re.match(r"^[a-z]+://|^mailto:|^#", target):
            return m.group(0)
        path, sep, anchor = target.partition("#")
        new = os.path.relpath(os.path.normpath(os.path.join(LOGD, path)), DOCS)
        return m.group(1) + new + sep + anchor + m.group(3)
    return LINK.sub(one, text)


def body_of(f, problems):
    fm, body = split_front_matter(open(os.path.join(LOGD, f)).read())
    if fm is None or fm.get("type") != "Log" or not fm.get("title") or not fm.get("description"):
        problems.append("docs/log.d/%s needs front matter with type: Log, a title and a description" % f)
        return None
    return rebase(body.strip("\n"))


def render():
    problems = []
    if not os.path.isdir(LOGD):
        return None, ["no docs/log.d directory"]
    files = sorted(os.listdir(LOGD))
    for f in FIXED:
        if f not in files:
            problems.append("docs/log.d/%s is missing" % f)
    entries = []
    for f in files:
        if f in FIXED:
            continue
        if not NAME.match(f):
            problems.append("docs/log.d/%s is not named <yyyy-mm-dd>-<nn>-<slug>.md (lowercase slug)" % f)
            continue
        body = body_of(f, problems)
        if body is None:
            continue
        if not body.startswith("- **"):
            problems.append("docs/log.d/%s: the entry does not start with a \"- **Title.**\" bullet" % f)
            continue
        entries.append((f, body))
    if not entries:
        problems.append("docs/log.d holds no entry")
    earlier = body_of("_earlier.md", problems) if "_earlier.md" in files else None
    if problems:
        return None, problems
    entries.sort(key=lambda e: e[0], reverse=True)
    months = []
    for f, text in entries:
        if not months or months[-1][0] != f[:7]:
            months.append((f[:7], []))
        months[-1][1].append(text)
    parts = [open(os.path.join(LOGD, "_header.md")).read().rstrip("\n")]
    for m, texts in months:
        parts.append("## " + m)
        parts.extend(texts)
    parts.append(earlier)
    return "\n\n".join(parts) + "\n", []


def main():
    out, problems = render()
    if problems:
        for p in problems:
            print("FAIL: " + p)
        return 1
    if not CHECK:
        open(OUT, "w").write(out)
        print("wrote docs/log.md from docs/log.d")
        return 0
    have = open(OUT).read() if os.path.exists(OUT) else ""
    if have != out:
        print("FAIL: docs/log.md is not what docs/log.d renders. Add or edit a file in docs/log.d, then run "
              "`python3 hack/render-log.py .`; never edit docs/log.md by hand.")
        for line in list(difflib.unified_diff(have.splitlines(), out.splitlines(), "docs/log.md (committed)",
                                              "docs/log.md (rendered)", lineterm=""))[:40]:
            print("  " + line)
        return 1
    n = len([f for f in os.listdir(LOGD) if f not in FIXED])
    print("OK: docs/log.md is the render of %d docs/log.d entries" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
