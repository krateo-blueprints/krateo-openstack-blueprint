#!/usr/bin/env python3
"""hack/render-log.py refuses arguments it does not know, and a refused run writes nothing.

    hack/validate-render-log-args.py [repo-root]

render-log.py used to drop every argument starting with "--" that it did not know and fall through to WRITE mode, so
`render-log.py --help` (or a typo such as --chek) rewrote docs/log.md in the working tree, and a pull request must
never carry that file (hack/validate-log-untouched.py). This runs this repo's renderer in a throwaway copy holding its
docs/ tree, with docs/log.md replaced by a marker, and checks, per case, the exit status and that docs/log.md is byte-for-byte the
marker afterwards (or, for a root that is not the repository, that nothing was created there):

  refused, nothing written:
    1. an unknown flag (--chek), before and after the root;
    2. two roots;
    3. --check and --check-rendered together;
    4. a root that does not exist, a root that is a file, and an existing directory without docs/log.d;
  help, nothing written:
    5. --help and -h exit 0 and print the usage;
  the documented forms still work (positive controls, so the refusals are not a renderer that refuses everything):
    6. `.` writes docs/log.md; `. --check` and `--check .` pass and write nothing; `. --check-rendered` fails against
       the marker and passes against the render.

Fails when a case cannot be set up, and when no case ran.
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
RENDERER = os.path.join(ROOT, "hack", "render-log.py")
MARKER = "MARKER: docs/log.md before the run\n"
failures = []
ran = 0


def run(args, cwd):
    return subprocess.run([sys.executable, "hack/render-log.py", *args], cwd=cwd, capture_output=True, text=True)


def fresh(tmp, name):
    """A throwaway repository: the renderer, this repo's docs/ (log.d and the pages it links), docs/log.md = MARKER."""
    w = os.path.join(tmp, name)
    os.makedirs(os.path.join(w, "hack"))
    shutil.copy(RENDERER, os.path.join(w, "hack", "render-log.py"))
    # The whole docs/ tree: entries link to docs/*.md, and the renderer checks those links resolve.
    shutil.copytree(os.path.join(ROOT, "docs"), os.path.join(w, "docs"))
    open(os.path.join(w, "docs", "log.md"), "w").write(MARKER)
    return w


def log_of(w):
    p = os.path.join(w, "docs", "log.md")
    return open(p).read() if os.path.exists(p) else None


def case(label, args, want_ok, want_written, tmp, cwd_name=None, check_dir=None, out_has=None):
    global ran
    w = fresh(tmp, cwd_name or label.replace(" ", "-").replace("/", "-"))
    r = run(args(w) if callable(args) else args, w)
    ran += 1
    ok = r.returncode == 0
    if ok != want_ok:
        failures.append("%s: exit %d, want %s\n      %s" % (label, r.returncode, "0" if want_ok else "non-zero",
                                                         (r.stdout + r.stderr).strip()[-300:]))
    written = log_of(w) != MARKER
    if written != want_written:
        failures.append("%s: docs/log.md was %s" % (label, "rewritten" if written else "not written"))
    if check_dir is not None:
        d = check_dir(w)
        if os.path.exists(os.path.join(d, "docs", "log.md")):
            failures.append("%s: a docs/log.md was created under the refused root %s" % (label, d))
    if out_has and out_has not in r.stdout + r.stderr:
        failures.append("%s: the output does not say %r" % (label, out_has))
    return w


def main():
    global ran
    if not os.path.isfile(RENDERER) or not os.path.isdir(os.path.join(ROOT, "docs", "log.d")):
        print("FAILED MEASUREMENT: no hack/render-log.py or docs/log.d under %s" % ROOT)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        # 1-4: refused, nothing written
        case("unknown flag after the root", [".", "--chek"], False, False, tmp, out_has="unrecognized arguments")
        case("unknown flag before the root", ["--chek", "."], False, False, tmp, out_has="unrecognized arguments")
        case("unknown flag alone", ["--force"], False, False, tmp, out_has="unrecognized arguments")
        case("two roots", [".", "."], False, False, tmp, out_has="unrecognized arguments")
        case("both modes", [".", "--check", "--check-rendered"], False, False, tmp, out_has="not allowed with")
        case("root does not exist", ["no-such-dir"], False, False, tmp, out_has="not an existing directory")
        case("root is a file", ["hack/render-log.py"], False, False, tmp, out_has="not an existing directory")
        os.makedirs(os.path.join(tmp, "elsewhere"))
        case("root without docs/log.d", [os.path.join(tmp, "elsewhere")], False, False, tmp,
             check_dir=lambda w: os.path.join(tmp, "elsewhere"), out_has="holds no docs/log.d")
        # 5: help
        case("--help", ["--help"], True, False, tmp, out_has="usage:")
        case("-h", ["-h"], True, False, tmp, out_has="usage:")
        # 6: positive controls
        w = case("write mode", ["."], True, True, tmp, out_has="wrote docs/log.md")
        rendered = log_of(w)
        case(". --check", [".", "--check"], True, False, tmp, out_has="well formed")
        case("--check .", ["--check", "."], True, False, tmp, out_has="well formed")
        case(". --check-rendered against the marker", [".", "--check-rendered"], False, False, tmp,
             out_has="is not what docs/log.d renders")
        w2 = fresh(tmp, "check-rendered-current")
        open(os.path.join(w2, "docs", "log.md"), "w").write(rendered or "")
        r = run([".", "--check-rendered"], w2)
        ran += 1
        if r.returncode != 0 or log_of(w2) != rendered:
            failures.append(". --check-rendered against the render: exit %d, or docs/log.md changed" % r.returncode)
    if not ran:
        print("FAILED MEASUREMENT: no case ran")
        return 2
    if failures:
        print("FAIL: %d failure(s) in %d cases:" % (len(failures), ran))
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: render-log.py refuses unknown flags, two roots, both modes and a non-repository root, writing nothing "
          "(%d cases, positive controls included)" % ran)
    return 0


if __name__ == "__main__":
    sys.exit(main())
