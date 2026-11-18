---
type: Log
title: render-log.py refuses arguments it does not know
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **render-log.py refuses arguments it does not know.**
  - **What happened:** `hack/render-log.py` ignored every `--` argument it did not recognise and fell through to write
    mode. So `render-log.py --help`, or a typo such as `--chek`, rewrote `docs/log.md` in the working tree, a file a
    pull request must never carry (`hack/validate-log-untouched.py`). `-h` was taken as the repository root.
  - **The fix:** arguments are now parsed with argparse before anything is read or written. Only the root and
    `--check` or `--check-rendered` (one of them) are accepted. Unknown flags, two roots and both modes at once are
    refused, and so is a root that is not an existing directory holding `docs/log.d`. `--help` prints the usage.
  - **The new gate:** `hack/validate-render-log-args.py` runs the renderer in a throwaway copy and checks:
    - each refusal exits non-zero with its reason and leaves `docs/log.md` byte-identical;
    - `--help` and `-h` only print the usage;
    - as positive controls, write mode, both `--check` orders and `--check-rendered` still work.

    Against the previous renderer it fails on the unknown-flag, two-roots, both-modes and `--help` cases, which
    rewrote `docs/log.md`, and on `-h`.
