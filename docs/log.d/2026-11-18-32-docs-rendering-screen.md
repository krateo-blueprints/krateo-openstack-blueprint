---
type: Log
title: The docs-rendering screen asks GitHub how admonitions render
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The docs-rendering screen asks GitHub how admonitions render.** Adopted from
  krateo-blueprints/openstack-nova-operator-kog f96b173 (#177). There, a `> [!WARNING]` indented in a list item
  rendered as a plain quote, and a `[!NOTE]` swallowed by an open fence rendered as shell. No static check saw
  either.
  - **What it does:** `hack/check-doc-rendering.py` POSTs every markdown file outside `charts/` (61 here) to GitHub's
    `/markdown` (gfm). Each file's count of `markdown-alert-title` must equal all of its admonition markers, fenced
    or not.
  - **Controls:** a known-good fixture must render 1 alert and a known-bad fenced one 0. Otherwise the run is VOID
    (exit 2), as it is for a file whose render can't be confirmed as that file.
  - **Port:** `scripts/mdfence.py` is byte-for-byte (sha256 bd1264c7…). The check has one change, marked at its line:
    a `docs/log.d` entry is front matter plus a list item, so it renders with no `<p>`, and its `<li>` now counts as
    the same evidence. Without that, all 35 log.d entries came back VOID.
  - **CI:** it runs in lint.yaml's examples-apiversion job (`hack/gates.d/check-doc-rendering.yaml`), with the job's
    token and `contents: read`.
  - **First run (2026-11-18):** 61 documents agreed, 0 disagreed, 0 VOID. No document here declares an admonition
    yet, so the screen guards the next one, and no exemption was needed.
