#!/usr/bin/env python3
"""GFM fence and admonition primitives, shared by every screen that needs them.

WHY THIS IS A MODULE AND NOT A COPY IN EACH SCRIPT. The defect that produced
these functions was a `[!NOTE]` rendered as shell script in README.md AND in
docs/usage.md -- the same note, two directories apart, both copies inserted
under an open fence. Two copies of a thing is how both copies end up wrong, and
a fence scanner duplicated across `scripts/validate.py` and
`hack/check-doc-rendering.py` would be the same mistake one layer up: the two
screens would disagree about what "inside a fence" means, and the disagreement
would surface as one of them passing a document the other failed.

THE ONE RULE WORTH KNOWING, because it is the part that is easy to get wrong: a
closing fence must sit at the SAME blockquote depth as the fence that opened the
block. GFM treats `> ``` ` inside a top-level block as four literal characters
of code, not as a close. A scanner that toggles on any fence at any depth
inverts inside/outside for every line after a blockquoted fence appears within
an open block -- and a blockquoted example block inside an admonition is exactly
that shape, so the documents this repo actually has are the ones that break it.
"""
import re

FENCE = re.compile(r"^\s*>?\s*(```+)\s*([A-Za-z0-9_+-]*)\s*$")
ADMONITION = re.compile(r"^\s*>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]")


def fence_scan(lines):
    """Walk a document's fences the way GFM does. Returns

        inside          list of bools, True where the line is CONTENT of a
                        fenced code block (the fence lines themselves are False)
        tagged_closers  [(index, tag)] for fences carrying a language tag in a
                        CLOSING position
        unclosed_at     index of a fence that is never closed, else None
        fences          how many fence lines were interpreted as fences
    """
    inside = [False] * len(lines)
    tagged_closers = []
    open_at = None
    fences = 0
    for n, line in enumerate(lines):
        m = FENCE.match(line)
        if m is None:
            if open_at is not None:
                inside[n] = True
            continue
        bq = line[: m.start(1)].count(">")
        if open_at is None:
            fences += 1
            open_at = (n, bq)
            continue
        if bq != open_at[1]:
            inside[n] = True          # literal content, cannot close
            continue
        fences += 1
        if m.group(2):
            tagged_closers.append((n, m.group(2)))
        open_at = None
    return inside, tagged_closers, (open_at[0] if open_at else None), fences


if __name__ == "__main__":
    # Enough of a self-test that `python3 scripts/mdfence.py` is meaningful.
    assert FENCE.match("> ```bash").group(2) == "bash"
    assert FENCE.match("```").group(2) == ""
    assert FENCE.match("   ```yaml").group(2) == "yaml"
    assert FENCE.match("use ``` to open a block") is None
    assert ADMONITION.match("> [!NOTE]")
    assert ADMONITION.match("> [!note]") is None, "GFM markers are upper case"
    assert ADMONITION.match("[!NOTE]") is None, "a marker needs its blockquote"
    ins, _, unclosed, n = fence_scan(
        ["```console", "> [!NOTE]", "> ```", "> x", "> ```", "```"])
    assert ins[1] and ins[2] and ins[4], "blockquoted fences stay literal"
    assert unclosed is None and n == 2, (unclosed, n)
    print("mdfence.py: self-test passed")
