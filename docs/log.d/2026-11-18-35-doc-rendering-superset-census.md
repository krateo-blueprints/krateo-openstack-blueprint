---
type: Log
title: The docs-rendering screen identifies each document past its own concatenations
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The docs-rendering screen identifies each document past its own concatenations (#388).** `docs/log.md` is rendered
  from every `docs/log.d` entry, so it repeats all of their tokens. Counted against everything, it left 40 of 71
  documents identified only by a shared token. That proves the response wasn't truncated, but not that it was for that
  document.
  - **Tiers:** each document takes the first tier that yields a token (agreed with the nova peer):
    1. the longest token unique across all documents;
    2. else the longest token unique once the documents containing this one's whole token set (its concatenations)
       are left out;
    3. else the longest shared token, reported as shared;
    4. else any block element.

    Every document is still checked.
  - **Why per document:** the rule's first form (18056b1) left every superset out of one global census. Four
    documents that merely quote small entries count as supersets that way: `README.md`, `docs/configuration.md`,
    `docs/release.md` and `docs/supply-chain.md`. Leaving them out for everyone handed 17 documents a "unique" token
    that one of those quoting documents' renders also held.
  - **Measured on 72 documents:** tier 1: 32, tier 2: 34, tier 3: 6, tier 4: 0.
    - Unique tokens that another, non-concatenation document also holds: 17 before, 0 after.
    - Still shared, as before: `docs/log.md` and the 2026-11-16-01 entry.
    - Now shared, honestly: four whose "unique" token was quoted elsewhere (the 2026-11-18-02 and -31 entries,
      `_earlier.md`, `examples/identity-profile/README.md`).
  - **What no token can fix:** an entry whose every token a quoting document holds (8 here, for example 2026-11-17-03
    in `README.md`). That document's render passes the entry's check whatever token is chosen.
  - **Counter:** this adopts nova-kog #181, which counts `<p class="markdown-alert-title"` (prose can't produce a
    literal `<`).
  - **Tests:** three self-test arms.
    - A, B and C = A + B: A and B at tier 2, C shared and still checked.
    - A small document sharing a long token with a quoting document picks its own tier-1 token.
    - A document under a concatenation keeps the token it shares with a quoting document shared.

    Mutants caught: skipping tier 1, excluding every superset at tier 2, and dropping supersets from the check.
