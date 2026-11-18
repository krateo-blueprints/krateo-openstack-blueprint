---
type: Log
title: The docs-rendering screen leaves concatenations out of its census
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The docs-rendering screen leaves concatenations out of its census (#388).** `docs/log.md` is rendered from every
  `docs/log.d` entry, so it repeats all of their tokens. Counted in the census, it left 40 of 71 documents identified
  only by a shared token, which proves the response wasn't truncated but not that it was for that document.
  - **The rule:** a document whose token set contains another document's whole token set is left out of the census,
    not out of the check. It is still checked, identified against the full count, where everything it holds is shared
    at best.
  - **Measured on 71 documents:** shared-token identification goes from 40 to 2.
    - The 2 still shared: `docs/log.md` itself, the concatenation, and one entry whose tokens all occur in other
      documents.
    - No document went from unique to shared, and none falls back to the block element.
  - **Quoting-heavy documents:** 5 documents count as supersets. `docs/log.md` is a real concatenation. `README.md`,
    `docs/configuration.md`, `docs/release.md` and `docs/supply-chain.md` each happen to contain every token of
    one to four small entries. They stay identified by unique tokens of their own, but the tokens they share with those
    entries now count as unique to the entries.
  - **Counter:** this adopts nova-kog #181, which counts `<p class="markdown-alert-title"` (prose can't produce a
    literal `<`). The census rule is applied on top of it.
  - **Tests:** a self-test arm (A, B and C = A + B): A and B are unique, C is shared and still checked, and without
    the rule A is shared. A mutant that drops C from the check instead of the census is caught.
