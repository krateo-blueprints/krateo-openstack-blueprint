---
type: Log
title: The docs-rendering screen's fixture contract with nova-kog
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The docs-rendering screen's fixture contract with nova-kog.** The nova peer couldn't copy this repository's
  `hack/check-doc-rendering.py`, so it implemented the tier ordering itself (its #187). The two repositories now agree
  on five named cases instead of on identical bytes. All five passed against main's file (f260b87) before they were
  added, and both repositories now run them as self-test arms 7a–7e:
  - **7a:** uniqueness beats length.
  - **7b:** a concatenation C = A + B leaves A and B at tier 2 and C shared, at tier 3.
  - **7c:** a document quoting only some of another's tokens isn't a superset of it, which the arm asserts first. The
    quoted document takes the token the quote lacks, at tier 1.
  - **7d:** a superset means containment, and an empty token set has no supersets.
  - **7e:** a document with no tokens falls to tier 4.
