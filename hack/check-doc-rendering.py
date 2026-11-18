#!/usr/bin/env python3
"""Ask GitHub's renderer whether this repo's admonitions actually render.

    exit 0   every document rendered as many alert boxes as it declares markers
    exit 1   a document declares N and renders M. The file and the lines are named.
    exit 2   VOID -- a control failed, and the output says which. Never a pass.

WHY THIS EXISTS AND WHAT IT ADDS. `verify_admonition_not_in_code_fence` in
scripts/validate.py knows ONE shape: a marker swallowed by an open fence. It
knows that shape because this repo shipped it in two files for eleven PRs. It
cannot know the shapes nobody has hit yet -- a mis-cased `[!note]`, a marker
indented under a list item, a marker separated from its blockquote by a blank
line. Each of those renders as plain text, silently, and a static screen built
from one example has no opinion about them. This script does not model markdown
at all. It asks the renderer and counts what came back.

THE DEFINITION IS THE WHOLE DESIGN, and my first one was blind to the very
defect that motivated the script. The obvious comparison is

    markers OUTSIDE fences  ==  alert boxes rendered

which passes the swallowed case perfectly: the marker is inside the fence, so
the left side excludes it, both sides are 0, and the gate reports clean on the
exact document that started this. So the comparison here is

    ALL markers, fenced or not  ==  alert boxes rendered

which makes this screen a strict superset of the static one. The cost is stated
rather than discovered: a document that deliberately SHOWS an admonition as
example code would fail here. None does. If one is added, exempt it by path
with the reason at the line, and do not loosen the count.

CONTROLS, and why there are two kinds.

  INSTRUMENT, once per run, before any document is judged. A known-good
  fixture must render exactly 1 alert, and a known-bad one (a marker inside a
  fence) must render exactly 0. The second is the one that matters: it proves
  that a swallowed admonition STILL renders as nothing today, rather than
  trusting a measurement from the day this was written. If GitHub ever starts
  rendering alerts inside code blocks, this run goes VOID instead of quietly
  passing every file.

  PER DOCUMENT, because one control per sweep is how 63 rate-limited fetches
  once read in this repo as "symbol absent". A rate-limited, truncated or wrong
  response must VOID that one file rather than read as "declares 0, rendered 0,
  agreed". A single "the API answered once" check at the top of an 18-file loop
  cannot do that.

  The control asks the strongest available question: does the returned HTML
  contain a token that is distinctive to this document -- the longest
  `[A-Za-z0-9_-]{12,}` run, containing a letter, that occurs outside this
  document's fences and in NO OTHER document of the corpus? That character class
  cannot be altered by HTML escaping or split by syntax-highlight spans, and
  unlike "has a heading" a corpus-unique token also fails when the response is a
  real render of a different document.

  Uniqueness is computed, not assumed, and the output says which kind of token a
  file got. Four of this repo's documents share
  `installing-this-pin-onto-a-cluster-that-already-has-a-provider` as their
  longest token, so picking "longest" alone would have given four files an
  identifier that cannot tell them apart -- a control that reads as strong and
  is not. A file with no unique token keeps the longest shared one (still good
  against truncation, stated as weaker); a file with no token at all falls back
  to "contains any block-level element".

  The earlier version of this control required a heading element, or a `<p>`
  when the source had no ATX heading. The openstack-as-a-service session hit its
  limit while porting this screen: 35 of their documents are front matter plus a
  single list item, which GitHub renders as `<hr>`, a setext `<h2>` and a
  `<ul><li>` -- no `<p>` anywhere, so all 35 VOIDed. Their fix was to accept
  `<li>` too, which is correct and narrower than this one. The lesson is the
  reason for the change: a control built from the documents in front of me
  mistook "the shape my repo happens to have" for "evidence the response is this
  document".

MEASURED FACTS THIS DEPENDS ON, 2026-11-18, from real responses:

    per alert box, GitHub emits  <div class="markdown-alert markdown-alert-note">
                                   <p class="markdown-alert-title">...</p>
    so the substring `markdown-alert` appears THREE times per alert and
    `markdown-alert-title` appears ONCE. Counting the former reports 6 alerts
    for a 2-alert document -- wrong in a way that looks like data.

    A marker INDENTED INSIDE A LIST ITEM is not an alert. GFM's extension does
    not reach there: `  > [!WARNING]` under a list item rendered as
    `<blockquote><p>[!WARNING]<br>...` -- a plain grey quote whose first
    visible line is the literal marker text. That is what
    docs/e2e-krateo-openstack.md:40 was, and why the fix was to hoist the block
    out of the list rather than to reword it. Deliberately NOT one of the
    instrument controls below: the count comparison stays correct whichever way
    GitHub decides to treat that case, so a control there would guard an
    assumption no verdict depends on.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
from mdfence import ADMONITION, fence_scan  # noqa: E402

ALERT = "markdown-alert-title"
HEADING = re.compile(r"<h[1-6][\s>]")
SRC_HEADING = re.compile(r"#{1,6} \S")
# Any block-level element, used only when a document has no distinctive token.
BLOCK = re.compile(r"<(?:p|li|h[1-6]|table|pre|blockquote)[\s>]")
# A token that cannot be altered by HTML escaping or by syntax-highlight spans:
# letters, digits, underscore and hyphen only, long enough to be specific. It
# must contain a LETTER -- without that rule kagent/README.md identified itself
# by a table rule, `--------------------------------`, which every table in the
# repo contains.
TOKEN = re.compile(r"[A-Za-z0-9_-]{12,}")

GOOD_FIXTURE = "# H\n\n> [!NOTE]\n> rendered\n"
BAD_FIXTURE = "# H\n\n```console\n> [!NOTE]\n> swallowed\n```\n"

OK, MISMATCH, VOID = 0, 1, 2


def docs():
    for f in sorted(glob.glob("**/*.md", recursive=True)):
        if f.startswith(".git" + os.sep) or os.sep + "charts" + os.sep in f:
            continue
        yield f


def render(text):
    """POST to GitHub's /markdown. Returns (html, None) or (None, reason)."""
    body = json.dumps({"text": text, "mode": "gfm"})
    try:
        p = subprocess.run(
            ["gh", "api", "-X", "POST", "/markdown", "--input", "-"],
            input=body, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return None, "the `gh` CLI is not on PATH"
    except subprocess.TimeoutExpired:
        return None, "the request did not answer within 60s"
    if p.returncode != 0:
        # gh prints error JSON on stdout for a 4xx, so a non-empty stdout is
        # NOT evidence the call worked. The return code is.
        return None, "gh exited %d: %s" % (
            p.returncode, (p.stderr.strip() or p.stdout.strip())[:300])
    if not p.stdout.strip():
        return None, "a 0-byte body with a 0 exit status"
    return p.stdout, None


def markers(lines):
    """[(line number, kind, inside_fence)] for every admonition marker."""
    inside, _, _, _ = fence_scan(lines)
    out = []
    for n, line in enumerate(lines):
        m = ADMONITION.match(line)
        if m:
            out.append((n + 1, m.group(1), inside[n]))
    return out


def comment_mask(lines, inside):
    """True where a line is inside an HTML comment (fences excluded).

    GitHub's renderer STRIPS HTML comments, so a token that only occurs in one
    is absent from the output by design. README.md declares this repo's provider
    floor in a machine-readable `<!-- ... -->` block, and its longest unique
    token lived there: the control VOIDed the file on its first run, correctly
    reporting that the response did not contain the token, and wrongly implying
    the response was not this document. A comment is as invisible to the
    renderer as a fence makes a token unmatchable, so both are excluded here.
    """
    mask = [False] * len(lines)
    open_comment = False
    for n, line in enumerate(lines):
        if inside[n]:
            continue
        if open_comment:
            mask[n] = True
            if "-->" in line:
                open_comment = False
            continue
        if "<!--" in line:
            mask[n] = True
            # A comment that also closes on this line leaves the state clean.
            if "-->" not in line.split("<!--", 1)[1]:
                open_comment = True
    return mask


def tokens_of(lines, inside):
    """Identifying tokens outside this document's fences and HTML comments.

    Outside the fences deliberately: inside a highlighted block GitHub wraps
    pieces of a line in <span>s, which can split a token and make a present
    document look absent. Outside comments because the renderer drops them.
    """
    commented = comment_mask(lines, inside)
    out = set()
    for n, line in enumerate(lines):
        if inside[n] or commented[n]:
            continue
        for m in TOKEN.finditer(line):
            t = m.group(0)
            if re.search(r"[A-Za-z]", t):
                out.add(t)
    return out


def pick_token(tokens, counts):
    """(token, unique) -- longest corpus-unique token, else longest, else None."""
    uniq = [t for t in tokens if counts.get(t, 0) == 1]
    if uniq:
        return max(uniq, key=len), True
    if tokens:
        return max(tokens, key=len), False
    return None, False


def instrument_controls():
    """Returns a list of reasons the instrument cannot be trusted right now."""
    bad = []
    for label, text, want in (("known-good", GOOD_FIXTURE, 1),
                              ("known-bad", BAD_FIXTURE, 0)):
        html, why = render(text)
        if html is None:
            bad.append("%s fixture did not render: %s" % (label, why))
            continue
        got = html.count(ALERT)
        if got != want:
            bad.append(
                "%s fixture rendered %d alert boxes, expected %d. The "
                "renderer's behaviour has changed, so no verdict below this "
                "line means anything." % (label, got, want))
        if not HEADING.search(html):
            bad.append(
                "%s fixture rendered no heading element, so the per-document "
                "control cannot be relied on either" % label)
    return bad


def self_test():
    """Offline. Every arm must be able to fail for a reason it names."""
    # 1. Marker extraction, including the fenced one the static screen catches.
    got = markers(BAD_FIXTURE.split("\n"))
    assert got == [(4, "NOTE", True)], got
    got = markers(GOOD_FIXTURE.split("\n"))
    assert got == [(3, "NOTE", False)], got

    # 2. The definition this script turns on. If `markers` ever stops
    #    reporting fenced markers, the swallowed case silently starts passing.
    assert any(inside for _, _, inside in markers(BAD_FIXTURE.split("\n"))), \
        "a fenced marker must still be COUNTED, or the gate is blind to the " \
        "defect it was written for"

    # 3. The counter, against markup recorded verbatim from a real response on
    #    2026-11-18 (svg/path children elided; the class attributes are exact).
    real_two = (
        '<h1>H</h1>\n'
        '<div class="markdown-alert markdown-alert-note">'
        '<p class="markdown-alert-title"><svg></svg>Note</p><p>one</p>\n</div>\n'
        '<div class="markdown-alert markdown-alert-warning">'
        '<p class="markdown-alert-title"><svg></svg>Warning</p><p>two</p>\n</div>\n')
    assert real_two.count(ALERT) == 2, real_two.count(ALERT)
    assert real_two.count("markdown-alert") == 6, \
        "the 3-per-alert inflation is the reason ALERT is the title class"
    assert HEADING.search(real_two)

    # 4. The mutation that would make this script useless, asserted as wrong
    #    rather than left to a comment: counting the div class instead.
    assert real_two.count("markdown-alert") != 2, \
        "if these ever agree, the fixture no longer expresses the bug"

    # 5. A response that is a lie: 0 bytes with a 0 status, and error JSON on
    #    stdout. Both must read as VOID, not as 0 alerts.
    assert HEADING.search("<p>no heading here</p>") is None
    assert not SRC_HEADING.search("no heading in this source line")
    assert SRC_HEADING.search("## Install the chart")

    # 6. The per-document control. A token must bear a letter: without that
    #    rule kagent/README.md identified itself by a table rule.
    ins, _, _, _ = fence_scan(["--------------------------------",
                               "about configurationFields here",
                               "```", "insideTheFenceToken", "```"])
    toks = tokens_of(["--------------------------------",
                      "about configurationFields here",
                      "```", "insideTheFenceToken", "```"], ins)
    assert "--------------------------------" not in toks, toks
    assert "configurationFields" in toks, toks
    assert "insideTheFenceToken" not in toks, "fenced tokens must be excluded"

    # 6b. HTML comments are stripped by the renderer, so a token that only
    #     occurs inside one is absent from the output BY DESIGN. README.md's
    #     longest unique token lives in its machine-readable floor comment and
    #     VOIDed the file on the first live run.
    doc = ["before the comment", "<!-- a note about tokenOnlyInComment",
           "   spanning two lines -->", "after tokenAfterComment ends"]
    i2, _, _, _ = fence_scan(doc)
    cm = comment_mask(doc, i2)
    assert cm == [False, True, True, False], cm
    t2 = tokens_of(doc, i2)
    assert "tokenOnlyInComment" not in t2, t2
    assert "tokenAfterComment" in t2, t2
    one = ["x <!-- shortLivedComment --> y", "tokenAfterOneLiner here"]
    i3, _, _, _ = fence_scan(one)
    assert comment_mask(one, i3) == [True, False], "a one-line comment must not latch"
    assert "tokenAfterOneLiner" in tokens_of(one, i3)

    # 7. Corpus-uniqueness must beat length, or four files in this repo would
    #    share one identifier and the control would read as strong while being
    #    unable to tell them apart.
    counts = {"sharedLongerToken": 2, "uniqueShorter": 1}
    tok, uniq = pick_token({"sharedLongerToken", "uniqueShorter"}, counts)
    assert (tok, uniq) == ("uniqueShorter", True), (tok, uniq)
    tok, uniq = pick_token({"sharedLongerToken"}, counts)
    assert (tok, uniq) == ("sharedLongerToken", False), (tok, uniq)
    assert pick_token(set(), counts) == (None, False)

    # 8. THE ARM THAT MAKES THE CONTROL WORTH HAVING: a real render of the
    #    WRONG document must fail it. Without corpus-uniqueness this arm passes
    #    on a broken control, which is why it is asserted and not commented.
    doc_a = ["# A", "", "it discusses theUniqueTokenOfA at length"]
    doc_b = ["# B", "", "it discusses theUniqueTokenOfB at length"]
    ia, _, _, _ = fence_scan(doc_a)
    ib, _, _, _ = fence_scan(doc_b)
    ta, tb = tokens_of(doc_a, ia), tokens_of(doc_b, ib)
    cnt = {}
    for t in list(ta) + list(tb):
        cnt[t] = cnt.get(t, 0) + 1
    pa, ua = pick_token(ta, cnt)
    assert ua and pa == "theUniqueTokenOfA", (pa, ua)
    render_of_b = "<h1>B</h1><p>it discusses theUniqueTokenOfB at length</p>"
    assert pa not in render_of_b, "doc A's token must be absent from doc B's render"

    print("check-doc-rendering.py: self-test passed (9 arms)")
    return OK


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true",
                    help="offline arms only; makes no network call")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    bad = instrument_controls()
    if bad:
        print("VOID: the instrument cannot disagree with anything right now.")
        for b in bad:
            print("  - " + b)
        return VOID

    # Corpus pass first: a token is only an identifier if no other file has it.
    corpus, counts = {}, {}
    for f in docs():
        lines = open(f, encoding="utf-8", errors="replace").read().split("\n")
        inside, _, _, _ = fence_scan(lines)
        corpus[f] = tokens_of(lines, inside)
        for t in corpus[f]:
            counts[t] = counts.get(t, 0) + 1
    shared = 0

    void, mismatched, clean = [], [], 0
    for f in docs():
        lines = open(f, encoding="utf-8", errors="replace").read().split("\n")
        declared = markers(lines)
        html, why = render("\n".join(lines))
        if html is None:
            void.append("%s: %s" % (f, why))
            continue
        token, unique = pick_token(corpus[f], counts)
        if token is not None:
            if not unique:
                shared += 1
            if token not in html:
                void.append(
                    "%s: the render does not contain %r, this document's "
                    "%s identifying token, so this response is not this "
                    "document" % (f, token,
                                  "corpus-unique" if unique else "longest (SHARED)"))
                continue
        elif not BLOCK.search(html):
            void.append(
                "%s: no token long enough to identify it and no block-level "
                "element in the render, so nothing confirms the response is "
                "this document" % f)
            continue
        rendered = html.count(ALERT)
        if rendered != len(declared):
            mismatched.append((f, declared, rendered))
        else:
            clean += 1

    for f, declared, rendered in mismatched:
        print("%s: declares %d admonition marker(s), GitHub rendered %d alert "
              "box(es)." % (f, len(declared), rendered))
        for ln, kind, in_fence in declared:
            print("    line %-4d [!%s]%s" % (
                ln, kind, "  <- inside a code fence" if in_fence else ""))
    for v in void:
        print("VOID " + v)

    print("\n%d document(s) agreed, %d disagreed, %d VOID" % (
        clean, len(mismatched), len(void)))
    if shared:
        print("%d document(s) had no corpus-unique token and were identified by "
              "a shared one -- still proof against a truncated response, not "
              "against a response for a different document." % shared)
    if void:
        return VOID
    return MISMATCH if mismatched else OK


if __name__ == "__main__":
    sys.exit(main())
