#!/usr/bin/env python3
"""Render docs/log.md from docs/log.d/, one file per change.

    hack/render-log.py [repo-root]                    write docs/log.md (the render-log workflow, on main)
    hack/render-log.py [repo-root] --check            validate docs/log.d alone (the pull-request gate)
    hack/render-log.py [repo-root] --check-rendered   also fail if docs/log.md is not the render

A change adds one file, docs/log.d/<yyyy-mm-dd>-<nn>-<slug>.md, and nothing else: a pull request never touches
docs/log.md (hack/validate-log-untouched.py). Every pull request used to edit the top of docs/log.md, and from #340
every one committed a regenerated copy, so any two branches in flight still conflicted there. Now
.github/workflows/render-log.yaml renders docs/log.md after each merge to main and lands it through a bot pull request
(hack/render-log-pr.sh). docs/log.md stays checked in because people read it on GitHub.

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

--check fails, naming the problem, when a file in docs/log.d is neither one of the two fixed parts nor named
<yyyy-mm-dd>-<nn>-<slug>.md with a real date, when two entries share a <yyyy-mm-dd>-<nn> (their order would be
ambiguous; CI checks the pull request merged with current main, so this fires when an entry merged ahead took the
number), when an entry lacks the front matter or does not start with "- **",
when a relative link in an entry does not resolve from docs/log.d (and so would not after rebasing), and when there is
no entry at all (no input is not a pass). It does not read docs/log.md, which on a pull request is main's.

Arguments are parsed strictly, BEFORE anything is read or written: an unknown flag (a typo such as --chek, or --help
read as "not a check"), two roots, or both modes at once are refused, and so is a root that is not an existing
directory holding docs/log.d. The parser used to drop every argument starting with "--" that it did not know and
fall through to WRITE mode, so `render-log.py --help` rewrote docs/log.md in the working tree, and a pull request
must never carry that file (hack/validate-log-untouched.py). hack/validate-render-log-args.py checks each refusal
writes nothing.
"""
import argparse
import datetime
import difflib
import os
import re
import sys


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="hack/render-log.py",
        description="Render docs/log.md from docs/log.d/ (write mode, the default), or validate docs/log.d.")
    ap.add_argument("root", nargs="?", default=".",
                    help="the repository root: an existing directory holding docs/log.d (default: .)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="validate docs/log.d alone and write nothing (the pull-request gate)")
    mode.add_argument("--check-rendered", action="store_true",
                      help="also fail if docs/log.md is not the render; write nothing")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        ap.error("root %r is not an existing directory" % a.root)
    if not os.path.isdir(os.path.join(root, "docs", "log.d")):
        ap.error("root %r holds no docs/log.d, so it is not this repository" % a.root)
    return root, a


ROOT, ARGS = parse_args(sys.argv[1:]) if __name__ == "__main__" else (os.path.abspath("."), None)
DOCS = os.path.join(ROOT, "docs")
LOGD = os.path.join(DOCS, "log.d")
OUT = os.path.join(DOCS, "log.md")
NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
FIXED = ("_header.md", "_earlier.md")
LINK = re.compile(r"(\]\()([^)\s]+)(\))")
EXTERNAL = re.compile(r"^[a-z]+://|^mailto:|^#")


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
        if EXTERNAL.match(target):
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
    for m in LINK.finditer(body):
        target = m.group(2)
        if not EXTERNAL.match(target) and not os.path.exists(os.path.join(LOGD, target.partition("#")[0])):
            problems.append("docs/log.d/%s: link %r does not resolve from docs/log.d (write it relative to the entry, "
                            "e.g. ../supply-chain.md)" % (f, target))
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
        m = NAME.match(f)
        if not m:
            problems.append("docs/log.d/%s is not named <yyyy-mm-dd>-<nn>-<slug>.md (lowercase slug)" % f)
            continue
        try:
            datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            problems.append("docs/log.d/%s: %s-%s-%s is not a date" % (f, m.group(1), m.group(2), m.group(3)))
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
    by_key = {}
    for f, _ in entries:
        by_key.setdefault(f[:13], []).append(f)
    for key, names in sorted(by_key.items()):
        if len(names) > 1:
            problems.append("docs/log.d: %s share %s, so their order is ambiguous; give the later change the next free "
                            "number for that date" % (" and ".join(names), key))
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
    n = len([f for f in os.listdir(LOGD) if f not in FIXED])
    if ARGS.check:
        print("OK: %d docs/log.d entries are well formed and render (docs/log.md is rendered on main, not here)" % n)
        return 0
    if ARGS.check_rendered:
        have = open(OUT).read() if os.path.exists(OUT) else ""
        if have != out:
            print("FAIL: docs/log.md is not what docs/log.d renders")
            for line in list(difflib.unified_diff(have.splitlines(), out.splitlines(), "docs/log.md (committed)",
                                                  "docs/log.md (rendered)", lineterm=""))[:40]:
                print("  " + line)
            return 1
        print("OK: docs/log.md is the render of %d docs/log.d entries" % n)
        return 0
    open(OUT, "w").write(out)
    print("wrote docs/log.md from %d docs/log.d entries" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
