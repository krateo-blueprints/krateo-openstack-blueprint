---
type: Log
title: The docs-rendering screen asks GitHub how admonitions render
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The docs-rendering screen asks GitHub how admonitions render.** Adopted from
  krateo-blueprints/openstack-nova-operator-kog at ed871ad (#177, #179). There, a `> [!WARNING]` indented in a list
  item rendered as a plain quote, and a `[!NOTE]` swallowed by an open fence rendered as shell. No static check saw
  either.
  - **What it does:** `hack/check-doc-rendering.py` POSTs every markdown file outside `charts/` (63 here) to GitHub's
    `/markdown` (gfm). Each file's count of `markdown-alert-title` must equal all of its admonition markers, fenced
    or not.
  - **Controls:** a known-good fixture must render 1 alert and a known-bad fenced one 0. Otherwise the run is VOID
    (exit 2).
  - **Per-document check:** each response must contain the document's own identifying token. That is its longest
    corpus-unique token outside fences and HTML comments, else its longest shared one, else any block element.
  - **Port:** byte-for-byte for both files (`hack/check-doc-rendering.py` sha256 f5c6e4f5…, `scripts/mdfence.py`
    bd1264c7…).
  - **CI:** it runs in lint.yaml's examples-apiversion job (`hack/gates.d/check-doc-rendering.yaml`), with the job's
    token and `contents: read`.
  - **First run (2026-11-18):** all 63 documents agreed, 0 VOID. No document here declares an admonition yet.
  - **Shared tokens:** 39 documents are identified by a shared token, not a unique one: the 37 files in
    `docs/log.d` plus `docs/log.md`, because `docs/log.md` is their concatenation, and
    `examples/identity-profile/README.md`. That still proves the response wasn't truncated. It doesn't prove the
    response was for this document.
  - **Upstream defect, fixed upstream:** the screen counted `markdown-alert-title` as a substring anywhere in the
    rendered HTML, so a document naming the class in prose counted as one alert. This entry did, and failed. nova-kog
    #181 now counts `<p class="markdown-alert-title"` (prose can't produce a literal `<`), and this entry names the
    class plainly again, as the live test of that counter (#388).
