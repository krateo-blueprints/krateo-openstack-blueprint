#!/usr/bin/env python3
"""Every OpenStack program a CronJob starts is given a command that program has (issue #289).

    hack/validate-cron-commands.py [repo-root]

trove's db-purge CronJob, rendered by default, ran `trove-manage ... db_purge --age_in_days 30`. trove-manage on
stable/2026.1 has no db_purge, so every scheduled run would fail at argument parsing, and nothing reports a failing
CronJob the way a failing hook Job blocks its release. The CronJob was removed (trove has no database purge at all).

Checked statically, with no render, so every CronJob a chart can render is covered whether or not it is on by default:
for each templates/*.yaml declaring `kind: CronJob`, the scripts it mounts from its chart's bin ConfigMap (each subPath
that configmap-bin.yaml fills from a bin/ template) and the template itself are read, and every <project>-* program
started in them (a shell command line, or a Python subprocess call) must be in hack/cron-program-commands.yaml,
measured from the program's source:
  - a program with commands: the words after it, skipping --config-file/--config-dir and their values, must begin with
    one of its listed command chains. An option before the command that is not one of those two, or a template
    expression there, fails: the gate cannot tell what it hides.
  - a program with `commands: none` takes options only and is accepted as listed.
  - a Python script that passes the command as a variable needs an `indirect` entry naming every command it can pass;
    each must be one of the program's, and each literal argument the CronJob passes the script must be in that list.
  - a chain listed under `lacks` fails by name; any other unlisted program or chain fails as unmeasured.
Shell lines are joined across backslash continuations, $(...) substitutions are dropped (the conf.d guard), and
template comments are removed. Out of scope: the API clients (`openstack`, whose nouns
hack/validate-job-service-dependencies.py maps, and `gnocchi`), mysql and kubectl, which are not service programs.

Fails when PyYAML is missing, when the map is unreadable, and when no CronJob or no program was found (no input is not a
pass).
"""
import os
import re
import shlex
import sys

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
BLUEPRINTS = os.path.join(ROOT, "blueprints")
MAP = os.path.join(ROOT, "hack", "cron-program-commands.yaml")
PROJECTS = ("aodh", "barbican", "blazar", "ceilometer", "cinder", "cloudkitty", "cyborg", "designate", "freezer",
            "glance", "gnocchi", "heat", "ironic", "keystone", "magnum", "manila", "masakari", "masakarimonitors",
            "mistral", "neutron", "nova", "octavia", "placement", "swift", "tacker", "trove", "watcher", "zaqar")
SH_PROGRAM = re.compile(r"(?:^\s*|[;&|(]\s*|\bexec\s+|\btimeout\s+\d+\s+)((?:%s)-[a-z0-9_-]+)(?=\s|$)"
                        % "|".join(PROJECTS))
PY_PROGRAM = re.compile(r"""(?:subprocess\.(?:call|run|check_call|check_output|Popen)|os\.exec\w+)\(\s*\[\s*"""
                        r"""['"]((?:%s)-[a-z0-9_-]+)['"]\s*(?:,\s*([^,\]\s]+))?""" % "|".join(PROJECTS))
VALUE_OPTIONS = ("--config-file", "--config-dir")
TPL = "@TEMPLATE@"
failures = []


def load_map():
    try:
        m = yaml.safe_load(open(MAP)) or {}
    except (OSError, yaml.YAMLError) as e:
        print("FAILED MEASUREMENT: cannot read %s: %s" % (MAP, e))
        sys.exit(2)
    progs = m.get("programs") or {}
    if not progs:
        print("FAILED MEASUREMENT: %s lists no programs" % MAP)
        sys.exit(2)
    return progs, m.get("indirect") or []


def strip_template_comments(text):
    return re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", text, flags=re.S)


def logical_lines(text):
    out, cur = [], ""
    for raw in strip_template_comments(text).splitlines():
        s = raw.rstrip()
        if not cur and s.lstrip().startswith("#"):
            continue
        if s.endswith("\\"):
            cur += s[:-1] + " "
            continue
        out.append(cur + s)
        cur = ""
    if cur:
        out.append(cur)
    return out


def words_after(line, end):
    rest = re.sub(r"\$\([^()]*\)", " ", line[end:])
    rest = re.sub(r"\{\{.*?\}\}", " %s " % TPL, rest)
    rest = re.split(r"\s(?:&&|\|\||;|\|)\s", rest)[0]
    try:
        return shlex.split(rest)
    except ValueError:
        return rest.split()


def command_words(words):
    """The positional words that name the command, or (None, reason) when something unreadable precedes them."""
    i = 0
    while i < len(words):
        w = words[i]
        if w in VALUE_OPTIONS:
            i += 2
            continue
        if any(w.startswith(o + "=") for o in VALUE_OPTIONS):
            i += 1
            continue
        if w == TPL:
            return None, "a template expression comes before the command"
        if w.startswith("-"):
            return None, "option %r comes before the command and is not one the gate knows takes a value" % w
        return words[i:], None
    return [], None


def check_chain(where, prog, entry, words):
    cmds = entry.get("commands")
    chains = [c.split() for c in (cmds or [])]
    got = [c for c in chains if words[:len(c)] == c]
    if got:
        return " ".join(max(got, key=len))
    lacks = {l["command"]: l.get("measured", "") for l in entry.get("lacks") or []}
    for n in range(len(words), 0, -1):
        if " ".join(words[:n]) in lacks:
            failures.append("%s: runs `%s %s`, a command %s does not have (measured: %s)"
                            % (where, prog, " ".join(words[:n]), prog, lacks[" ".join(words[:n])]))
            return None
    failures.append("%s: runs `%s %s`, which hack/cron-program-commands.yaml does not list for %s (listed: %s). "
                    "Measure it in the program's source and list it, or fix the command"
                    % (where, prog, " ".join(words[:3]) or "(no command)", prog,
                       ", ".join(cmds) if isinstance(cmds, list) else cmds))
    return None


def bin_scripts(chart_dir, template_text):
    """{subPath name: (template path, text)} for each bin script the CronJob mounts."""
    cm = os.path.join(chart_dir, "templates", "configmap-bin.yaml")
    cm_text = open(cm).read() if os.path.exists(cm) else ""
    out = {}
    for name in sorted(set(re.findall(r"subPath:\s*['\"]?([A-Za-z0-9_.-]+)", template_text))):
        m = re.search(r"^\s+%s:\s*\|\s*\n\s*\{\{-?\s*tuple\s+\"(bin/[^\"]+)\"" % re.escape(name), cm_text, re.M)
        if not m:
            continue
        p = os.path.join(chart_dir, "templates", m.group(1))
        if os.path.exists(p):
            out[name] = (m.group(1), open(p).read())
    return out


def script_args(template_text, name):
    """Literal arguments a container command passes after /tmp/<name> (block-style command list)."""
    lines = template_text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\s*-\s*['\"]?/tmp/%s['\"]?\s*$" % re.escape(name), line):
            args = []
            for nxt in lines[i + 1:]:
                m = re.match(r"\s*-\s*['\"]?([A-Za-z0-9_.-]+)['\"]?\s*$", nxt)
                if not m or nxt.strip().endswith(":"):
                    break
                args.append(m.group(1))
            return args
    return []


def main():
    progs, indirect = load_map()
    crons, checked = 0, []
    for chart in sorted(os.listdir(BLUEPRINTS)):
        tdir = os.path.join(BLUEPRINTS, chart, "chart", "templates")
        if not os.path.isdir(tdir):
            continue
        for f in sorted(os.listdir(tdir)):
            if not f.endswith(".yaml"):
                continue
            text = open(os.path.join(tdir, f)).read()
            if not re.search(r"^kind:\s*CronJob\s*$", strip_template_comments(text), re.M):
                continue
            crons += 1
            sources = [("templates/" + f, text, None)]
            sources += [("templates/" + p, t, name) for name, (p, t) in bin_scripts(
                os.path.join(BLUEPRINTS, chart, "chart"), text).items()]
            for path, body, name in sources:
                where = "%s %s (CronJob %s)" % (chart, path, f)
                if path.endswith(".py.tpl") or path.endswith(".py"):
                    for m in PY_PROGRAM.finditer(body):
                        prog, arg = m.group(1), (m.group(2) or "")
                        entry = progs.get(prog)
                        if entry is None:
                            failures.append("%s: starts %s, which hack/cron-program-commands.yaml does not list; "
                                            "measure its commands first" % (where, prog))
                            continue
                        lit = re.fullmatch(r"""['"]([A-Za-z0-9_-]+)['"]""", arg)
                        if lit:
                            c = check_chain(where, prog, entry, [lit.group(1)])
                            if c:
                                checked.append("%s: %s %s" % (where, prog, c))
                            continue
                        ind = [e for e in indirect if e.get("chart") == chart and e.get("script") == name
                               and e.get("program") == prog]
                        if not ind:
                            failures.append("%s: passes %s a command in a variable (%s); add an `indirect` entry "
                                            "naming every command it can pass" % (where, prog, arg or "?"))
                            continue
                        allowed = list(ind[0].get("commands") or [])
                        for c in allowed:
                            if check_chain(where, prog, entry, c.split()):
                                checked.append("%s: %s %s (indirect)" % (where, prog, c))
                        for a in script_args(text, name):
                            if a not in allowed:
                                failures.append("%s: the CronJob passes %s the command %r, which its indirect entry "
                                                "does not list (%s)" % (where, name, a, ", ".join(allowed)))
                    continue
                for line in logical_lines(body):
                    for m in SH_PROGRAM.finditer(line):
                        prog = m.group(1)
                        entry = progs.get(prog)
                        if entry is None:
                            failures.append("%s: starts %s, which hack/cron-program-commands.yaml does not list; "
                                            "measure its commands first" % (where, prog))
                            continue
                        if entry.get("commands") == "none":
                            checked.append("%s: %s (takes no command)" % (where, prog))
                            continue
                        words, why = command_words(words_after(line, m.end()))
                        if words is None:
                            failures.append("%s: `%s`: %s, so its command cannot be read" % (where, prog, why))
                            continue
                        c = check_chain(where, prog, entry, words)
                        if c:
                            checked.append("%s: %s %s" % (where, prog, c))
    if crons == 0:
        failures.append("no CronJob template was found under blueprints/*/chart/templates; the check had no input")
    elif not checked and not failures:
        failures.append("%d CronJob template(s) found but no OpenStack program in any of them; the parser read nothing"
                        % crons)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: %d CronJob template(s); %d program command(s), each one its program has:" % (crons, len(checked)))
    for c in checked:
        print("  " + c)


main()
