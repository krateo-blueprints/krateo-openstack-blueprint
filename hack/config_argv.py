"""How an oslo.config program's --config-file/--config-dir arguments are read from text -- ONE parser for the gates.

    import config_argv   (importlib from hack/, as the gates load their helpers)
    config_argv.probe_config(words) -> [(flag, path)]   a worker probe's exec command, in order
    config_argv.dirs_read(text)     -> {dir}            every directory the text reads as a --config-dir
    config_argv.strip_dirs(text)    -> text             the same text with every directory argument removed
    config_argv.guarded(text)       -> {path}           every path passed only when it exists

A directory is read in any of four spellings, which mean the same thing to oslo.config:
  --config-dir D, --config-dir=D            always
  $([ -d D ] && echo --config-dir D)         the start scripts: only when D exists (oslo.config aborts on a missing one)
  --config-dir-if-present D                  the worker readiness probe's form of the line above
                                             (helm-toolkit.snippets.oslo_config_args renders both from one list)
hack/validate-credentials.py (check 22m: a probe that parses a carrier reads its drop-in directory),
hack/validate-probe-config-args.py (the probe's list equals its exec line's) and hack/validate-config-file-refs.py
(which references are optional because they are passed only when present) all read through this, so they cannot
disagree on what a program or a probe reads. A trailing slash on a directory is not significant.
"""
import re

PROBE_FLAGS = ("--config-file", "--config-dir", "--config-dir-if-present")
_DIR = re.compile(r"--config-dir(?:-if-present)?(?:=|\s+)[\"']?(/[^\s\"'\\)]+)")
_GUARDED = re.compile(r"\$\(\s*\[\s+-d\s+(/[^\s\]]+?)/?\s+\]\s*&&\s*echo\s+--config-dir[=\s]+(/[^\s)]+?)/?\s*\)")


def probe_config(words):
    """[(flag, path)] for every config argument of an argv list, in order."""
    words = list(words)
    out = []
    for i, w in enumerate(words):
        if w in PROBE_FLAGS and i + 1 < len(words):
            out.append((w, words[i + 1]))
        else:
            for f in PROBE_FLAGS:
                if w.startswith(f + "="):
                    out.append((f, w.split("=", 1)[1]))
    return out


def dirs_read(text):
    """Every directory `text` reads as a --config-dir, in any of the four spellings."""
    out = {d.rstrip("/") for d in _DIR.findall(text or "")}
    out |= {a.rstrip("/") for a, b in _GUARDED.findall(text or "") if a.rstrip("/") == b.rstrip("/")}
    return out


_GUARDED_ANY = re.compile(r"\[\s+-[df]\s+(/[^\s\]]+?)/?\s+\]\s*&&\s*echo\s+--(?:config-file|config-dir)[=\s]+(\S+?)/?(?=[\s)\"']|$)")
_IF_PRESENT = re.compile(r"--config-dir-if-present(?:=|\s+)[\"']?(/[^\s\"'\\)]+)")


def guarded(text):
    """Every path `text` passes only when it exists: `[ -d|-f P ] && echo --config-dir|--config-file P` in a script,
    --config-dir-if-present P in a probe. Such a reference is optional by construction (oslo.config aborts on a missing
    --config-dir, so a chart that delivers its drop-ins file by file has no conf.d at defaults)."""
    out = {a.rstrip("/") for a, b in _GUARDED_ANY.findall(text or "") if a.rstrip("/") == b.rstrip("/")}
    return out | {p.rstrip("/") for p in _IF_PRESENT.findall(text or "")}


def strip_dirs(text):
    """`text` with every directory argument removed: the positive control's input."""
    text = _GUARDED.sub("", text or "")
    return _DIR.sub("", text)


SELF_TEST = [
    # (text, directory, read)
    ("prog --config-file /etc/x/x.conf --config-dir /etc/x/x.conf.d", "/etc/x/x.conf.d", True),
    ("prog --config-file /etc/x/x.conf --config-dir=/etc/x/x.conf.d/", "/etc/x/x.conf.d", True),
    ("prog --config-file /etc/x/x.conf $([ -d /etc/x/x.conf.d ] && echo --config-dir /etc/x/x.conf.d)",
     "/etc/x/x.conf.d", True),
    ("python /tmp/worker-health-probe.py --config-file /etc/x/x.conf --config-dir-if-present /etc/x/x.conf.d",
     "/etc/x/x.conf.d", True),
    ("python /tmp/worker-health-probe.py --config-file /etc/x/x.conf", "/etc/x/x.conf.d", False),
    ("prog --config-file /etc/x/x.conf --config-dir /etc/y/y.conf.d", "/etc/x/x.conf.d", False),
    ("prog $([ -d /etc/x/x.conf.d ] && echo --config-dir /etc/y/y.conf.d)", "/etc/x/x.conf.d", False),
]


GUARDED_TEST = [
    ("prog $([ -d /etc/x/x.conf.d ] && echo --config-dir /etc/x/x.conf.d)", {"/etc/x/x.conf.d"}),
    ("prog $([ -f /tmp/h.conf ] && echo --config-file /tmp/h.conf)", {"/tmp/h.conf"}),
    ("probe --config-file /etc/x/x.conf --config-dir-if-present /etc/x/x.conf.d", {"/etc/x/x.conf.d"}),
    ("prog --config-file /etc/x/x.conf --config-dir /etc/x/x.conf.d", set()),
    ("prog $([ -d /etc/x/x.conf.d ] && echo --config-dir /etc/y/y.conf.d)", set()),
]


def self_test():
    """[problem] when the parser misreads a known case, its stripped form included."""
    bad = ["%r: guarded paths %s, want %s" % (t, sorted(guarded(t)), sorted(w)) for t, w in GUARDED_TEST
           if guarded(t) != w]
    for text, d, want in SELF_TEST:
        if (d in dirs_read(text)) != want:
            bad.append("%r: reads %s is %s, want %s" % (text, d, not want, want))
        if d in dirs_read(strip_dirs(text)):
            bad.append("%r: still reads %s once its directory arguments are removed" % (text, d))
    return bad
