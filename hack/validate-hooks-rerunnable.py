#!/usr/bin/env python3
"""Every install hook can run again, because core-provider runs it again (#457).

    hack/validate-hooks-rerunnable.py [repo-root] [--self-test]

An interrupted FIRST install is installed again by core-provider (2.13.128: core-provider#629), so every post-install hook runs a
second time over whatever the first run left. An upgrade runs them again too. A hook that creates without looking, or that
drops, is then wrong. Measured: the 0.3.71 -> 0.3.74 upgrade re-ran all of them on GKE, and r074g killed two first installs.

Every component chart is rendered at its defaults. For every container of every Job with a post-install hook, the program it runs
(the mounted script its command names, or its inline command) must be judged:
  1. SHELL, by its commands (continuation lines joined, comments and function names out):
     * `openstack <noun> create` has --or-show, or a lookup of the same noun (show, list, or a filter on it) comes first;
     * `openstack <noun> delete`, `rm -r`, and `curl -X POST|PUT|DELETE` only in DESTRUCTIVE, with the reason;
     * no SQL DROP, TRUNCATE or DELETE; every SQL CREATE has IF NOT EXISTS;
     * no `rabbitmqctl add_user`, `rabbitmqctl add_vhost` (they fail the second time), no `rbd create` and `ceph osd pool create`
       without a test of the same object first.
     An `openstack` verb the rule does not know fails. Migration programs (nova-manage, alembic, keystone-manage ...) are NOT
     judged: every upgrade has always run them over an existing database.
  2. PYTHON, by the SQL rules, and by name: REVIEWED names each program with how it survives a second run and a text that
     must be in it, so removing the guard fails. A program not named fails.
A component whose CompositionDefinition keeps the human gate (krateo.io/interrupted-install-recovery) is not exempt: an upgrade
runs its hooks again all the same.

Fails when helm or PyYAML is missing, when a chart does not render, when a Job's program cannot be found, and below MIN_JOBS.
"""
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
ROOT = os.path.abspath(ARGS[0] if ARGS else ".")
sys.path.insert(0, os.path.join(ROOT, "hack"))
import gate_values  # noqa: E402

TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
MIN_JOBS = 150
READ = {"show", "list", "issue", "check", "contains", "validate"}
WRITE_AGAIN = {"set", "add", "unset", "remove"}      # setting a value or a role twice is the same as once
# (script key, what the rule finds) -> why it is safe to run again
DESTRUCTIVE = {
    ("ks-endpoints.sh", "endpoint delete"): "removes duplicate endpoints of one interface, or one whose URL changed, and registers it again in the same run",
    ("storage-init.sh", "curl POST"): "glance: sets the metadata of the Swift account, the same request each time",
}
# python program -> (how it survives a second run, a text that must be in it)
REVIEWED = {
    "db-init.py": ("CREATE DATABASE and USER IF NOT EXISTS, then ALTER USER and GRANT, which set a state", "CREATE USER IF NOT EXISTS"),
    "fernet-manage.py": ("setup stops when the Secret already holds keys; an interrupted first run leaves it empty, and is completed", "Keys already exist, skipping setup"),
    "api-key-init.py": ("leaves a Secret that already holds the key alone, and adds the key to one that does not", "leaving it alone"),
}


def join(text):
    """Logical lines: continuations joined, comments dropped."""
    text = re.sub(r"\\\n\s*", " ", text)
    return [x for x in (re.sub(r"(^|\s)#(?![!{]).*$", "", y).strip() for y in text.splitlines()) if x]


OS_CMD = re.compile(r"\bopenstack\s+((?:--?[a-z-]+(?:[= ]\S+)?\s+)*)([a-z][a-z0-9 -]*?)\s+(create|delete|set|unset|add|remove|show|list|issue|check|save|upload|rename|"
                    r"import|stage|migrate|failover|grant|revoke|contains|validate)\b")


def openstack(line):
    for m in OS_CMD.finditer(line):
        words = m.group(2).split()
        if all(re.match(r"^[a-z][a-z0-9-]*$", w) for w in words):
            yield " ".join(words), m.group(3), m.start()


def sql(text, where):
    bad = []
    for m in re.finditer(r"\b(DROP\s+(?:DATABASE|TABLE|USER|INDEX|VIEW)|TRUNCATE\s+TABLE|DELETE\s+FROM)\b", text):
        bad.append("%s: SQL %s in an install hook" % (where, " ".join(m.group(1).split())))
    for m in re.finditer(r"\bCREATE\s+(DATABASE|TABLE|USER|INDEX)\b(?!\s+IF\s+NOT\s+EXISTS)", text):
        bad.append("%s: SQL CREATE %s without IF NOT EXISTS" % (where, m.group(1)))
    return bad


def judge_shell(key, text, where):
    # a message or a sed pattern that mentions SQL runs none
    bad = sql("\n".join(x for x in join(text) if not re.match(r"^(echo|printf)\b", x) and not re.search(r"\bsed\s", x)), where)
    looked, n = set(), 0
    for line in join(text):
        # helm-toolkit's lookup (#295): openstack_count <name> <noun> list ...; it fails when the API does not answer
        for m in re.finditer(r"\bopenstack_count\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s+([a-z][a-z -]*?)\s+list\b", line):
            looked.add(m.group(1))
        for noun, verb, at in openstack(line):
            n += 1
            if verb in READ:
                looked.add(noun)
            elif verb == "create":
                if "--or-show" not in line and noun not in looked:
                    bad.append("%s: `openstack %s create` with no lookup before it and no --or-show" % (where, noun))
            elif verb == "delete":
                if (key, "%s delete" % noun) not in DESTRUCTIVE:
                    bad.append("%s: `openstack %s delete` in an install hook, and no reason in DESTRUCTIVE" % (where, noun))
            elif verb not in WRITE_AGAIN:
                bad.append("%s: `openstack %s %s`: this gate does not know whether it can run twice" % (where, noun, verb))
        # every `openstack` call must have been read: one it cannot parse is not judged
        calls = len(re.findall(r"(?:^|[|&;(`!]\s*|\bthen\s+|\belse\s+|\bdo\s+|\$\(\s*|=\s*)openstack\s+[a-z-]", line))
        if calls > len(list(openstack(line))):
            bad.append("%s: an openstack call this gate cannot read: %s" % (where, line[:120]))
        m = re.search(r"\bcurl\b.*?-X\s*(POST|PUT|DELETE)", line)
        if m and (key, "curl %s" % m.group(1)) not in DESTRUCTIVE:
            bad.append("%s: curl -X %s, and no reason in DESTRUCTIVE" % (where, m.group(1)))
        if re.search(r"\brm\s+-[a-z]*r", line) and not re.search(r"\brm\s+-[a-z]*r[a-z]*\s+\"?(/tmp|\$\{?TMP|\$\{?tmp)", line):
            bad.append("%s: rm -r outside a temporary directory" % where)
        if re.search(r"\brabbitmqctl\s+add_(user|vhost)\b", line):
            bad.append("%s: rabbitmqctl add_user or add_vhost fails the second time" % where)
        for obj, test in (("rbd create", r"\brbd\s+(info|ls)\b"), ("ceph osd pool create", r"\bceph\s+osd\s+pool\s+(stats|get|ls)\b")):
            if obj in re.sub(r"\s+", " ", line) and not re.search(test, text):
                bad.append("%s: %s without a test of the object first" % (where, obj))
    return bad


def judge_python(key, text, where, reviewed):
    bad = sql(text, where)
    if key not in reviewed:
        bad.append("%s: a Python program this gate has not reviewed (%s): say in REVIEWED how it survives a second run" % (where, key))
    elif reviewed[key][1] not in text:
        bad.append("%s: the guard that made %s safe to run again (%r) is gone" % (where, key, reviewed[key][1]))
    return bad


def programs(job, files):
    """[(key, text)] a Job's containers run; a container whose program is not found yields (name, None)."""
    sp = job["spec"]["template"]["spec"]
    vols = {v["name"]: v for v in sp.get("volumes") or []}
    for c in (sp.get("initContainers") or []) + sp["containers"]:
        if c["name"] == "init":       # kubernetes-entrypoint: it waits, and changes nothing
            continue
        cmd = " ".join((c.get("command") or []) + (c.get("args") or []))
        got = []
        for m in c.get("volumeMounts") or []:
            if m["mountPath"] in cmd:
                v = vols.get(m["name"], {})
                name = (v.get("configMap") or {}).get("name") or (v.get("secret") or {}).get("secretName")
                if (name, m.get("subPath")) in files:
                    got.append((m["subPath"], files[(name, m["subPath"])]))
        if not got and re.match(r"^/?\S*\.(sh|py)\b", cmd):
            yield c["name"], None
        else:
            for g in got or [("inline", cmd)]:
                yield g


def check(chart, docs, reviewed):
    files, bad, n = {}, [], 0
    for d in docs:
        if d.get("kind") in ("ConfigMap", "Secret"):
            for k, v in (d.get("data") or {}).items():
                files[(d["metadata"]["name"], k)] = base64.b64decode(v).decode("utf-8", "replace") if d["kind"] == "Secret" else v
    for d in docs:
        hook = ((d["metadata"].get("annotations") or {}).get("helm.sh/hook") or "") if d.get("kind") == "Job" else ""
        if "post-install" not in hook and "pre-install" not in hook:
            continue
        n += 1
        for key, text in programs(d, files):
            where = "%s/%s %s" % (chart, d["metadata"]["name"], key)
            if text is None:
                bad.append("%s: the program it runs was not found in the render" % where)
            elif key.endswith(".py"):
                bad += judge_python(key, text, where, reviewed)
            else:
                bad += judge_shell(key, text, where)
    return bad, n


def self_test():
    S = lambda t, k="x.sh": len(judge_shell(k, t, "w"))
    cases = [
        ("openstack_count is a lookup", S("n=$(openstack_count \"My image\" image list --name \"My image\")\nopenstack image create x"), 0),
        ("openstack_count of another noun is not", S("n=$(openstack_count net network list --name net)\nopenstack image create x"), 1),
        ("SQL in a message", S("echo \"no CREATE TABLE statement found\" >&2\nE=$(sed -nE 's/^CREATE TABLE ([a-z]+).*/x/p' f)"), 0),
        ("SQL in a heredoc", S("mysql <<EOF\nDROP TABLE x;\nEOF"), 1),
        ("a bare flavor create", S("openstack flavor create m1.tiny --ram 64"), 1),
        ("a create after a lookup", S("openstack flavor show m1.tiny || \\\n  openstack flavor create m1.tiny"), 0),
        ("a lookup of another noun does not count", S("openstack image list\nopenstack flavor create m1"), 1),
        ("--or-show, on a continued line", S("ID=$(openstack project create --or-show --enable -f value -c id \\\n  --domain=\"${D}\" \"${P}\")"), 0),
        ("an unconditional DROP", S("mysql -e 'DROP DATABASE nova'"), 1),
        ("CREATE DATABASE bare", S("mysql -e 'CREATE DATABASE nova'"), 1),
        ("CREATE DATABASE IF NOT EXISTS", S("mysql -e 'CREATE DATABASE IF NOT EXISTS nova'"), 0),
        ("a delete with no reason", S("openstack image delete x"), 1),
        ("ks-endpoints' delete", S("openstack endpoint list\nopenstack endpoint delete ${ID}", "ks-endpoints.sh"), 0),
        ("a verb it does not know", S("openstack server migrate x"), 1),
        ("a call it cannot read", S("openstack frobnicate it"), 1),
        ("set, add role", S("openstack user set --password=x u\nopenstack role add --user u r"), 0),
        ("curl POST", S("curl -fsS -X POST http://x"), 1),
        ("rm -rf of a path", S("rm -rf /var/lib/x"), 1),
        ("rm -rf of tmp", S("rm -rf /tmp/x"), 0),
        ("rabbitmqctl add_user", S("rabbitmqctl add_user a b"), 1),
        ("a pool tested first", S("ceph osd pool stats \"$1\" || ceph osd pool create \"$1\""), 0),
        ("a comment is not a command", S("# openstack flavor create x\ntrue"), 0),
        ("python: unreviewed", len(judge_python("new.py", "x", "w", {})), 1),
        ("python: its guard gone", len(judge_python("a.py", "x", "w", {"a.py": ("y", "IF NOT EXISTS")})), 1),
        ("python: reviewed", len(judge_python("a.py", "CREATE USER IF NOT EXISTS", "w", {"a.py": ("y", "IF NOT EXISTS")})), 0),
        ("python: a drop", len(judge_python("a.py", "IF NOT EXISTS; DROP TABLE x", "w", {"a.py": ("y", "IF NOT EXISTS")})), 1),
    ]
    wrong = [c for c in cases if c[1] != c[2]]
    for c in wrong:
        print("SELF-TEST FAIL: %s: %d finding(s), want %d" % c)
    print("self-test: %d case(s), %d wrong" % (len(cases), len(wrong)))
    return 1 if wrong else 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    if shutil.which("helm") is None:
        print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
        return 2
    comps = [c["name"] for c in yaml.safe_load(open(os.path.join(ROOT, "blueprints", "openstack", "chart", "values.yaml")))["components"]]
    fails, jobs, charts = [], 0, 0
    with tempfile.TemporaryDirectory() as tmp:
        for c in comps:
            src = os.path.join(ROOT, "blueprints", c, "chart")
            if not os.path.isdir(src):
                continue
            w = os.path.join(tmp, c)
            shutil.copytree(src, w)
            cy = os.path.join(w, "Chart.yaml")
            text = open(cy).read()
            open(cy, "w").write(text.replace("CHART_VERSION", "0.0.0-gate"))
            if "helm-toolkit" in text:
                os.makedirs(os.path.join(w, "charts"), exist_ok=True)
                shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
            r = subprocess.run(["helm", "template", "gate-" + c, w, "--namespace", "openstack"] + gate_values.helm_args(w), capture_output=True, text=True)
            if r.returncode != 0:
                fails.append("%s does not render: %s" % (c, r.stderr.strip()[-300:]))
                continue
            bad, n = check(c, [d for d in yaml.safe_load_all(r.stdout) if d], REVIEWED)
            fails += bad
            jobs += n
            charts += 1
    if jobs < MIN_JOBS:
        fails.append("only %d install-hook Jobs found, fewer than %d: this gate sees too little" % (jobs, MIN_JOBS))
    for f in fails:
        print("FAIL: " + f)
    print("%d chart(s), %d install-hook Job(s); %d failure(s)" % (charts, jobs, len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
