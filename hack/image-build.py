#!/usr/bin/env python3
"""Build the images of hack/image-build.yaml from upstream's Dockerfiles or builds, and publish them (#463, #454).

    hack/image-build.py [repo-root] --check     offline: the list is well formed and names images the charts run (lint.yaml)
    hack/image-build.py [repo-root] --build     build and verify every entry not published yet, publish nothing (pull requests)
    hack/image-build.py [repo-root] --publish   the same, then push each target that does not exist yet (image-build.yaml, main)
    hack/image-build.py --self-test             the rules against built-in cases

--check, for every entry: every field is there; the Dockerfile's commit and the source's are full commit ids; both base images
are pinned by digest; the target is under ghcr.io/krateo-blueprints/images/ with a dated tag (it names one build), and no
two entries share it; the charts run the image it replaces, or the target once they moved to it. An entry whose images no
chart runs is stale and fails.

An entry with `removes` is upstream's own build minus those Debian packages (#454): `from` is that build by digest, which must
be the one hack/images.yaml recorded for `replaces`, so the scan has measured it. `dpkg --purge` gets the exact list, with no
network and no resolver: it refuses while anything else depends on a listed package. In the result, the Debian packages must
be upstream's minus exactly that list, the Python packages the same at the same versions, the `provides` modules must import
(here they import in upstream's image too), and User, Entrypoint, Cmd, WorkingDir and PATH must not change.

For every other entry, --build reads the Dockerfile at its commit (a shallow fetch of that commit, checked) and refuses to build while the source branch is
at another commit than the one written here: the Dockerfile clones a branch, so the pin is checked before and after. Then, in
the image it built:
  * /etc/image_info/project.txt names the pinned source commit;
  * every `provides` module imports, and the image it replaces cannot import it (else the entry is no longer needed);
  * User, Entrypoint, Cmd, WorkingDir and PATH are those of the image it replaces, and no Python package of that image is
    missing. Version differences are printed.

A published tag is never moved, so an entry whose target already pulls anonymously is neither rebuilt nor pushed, in both
modes: a later commit upstream can't fail an image that is already out.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = re.compile(r"^ghcr\.io/krateo-blueprints/images/[a-z0-9][a-z0-9._-]*:[A-Za-z0-9._-]*[-_]20[0-9]{6}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
PINNED = re.compile(r"^[a-z0-9.-]+/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
MODULE = re.compile(r"^[a-z_][a-z0-9_.]*$")
PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[a-z0-9_,-]+\])?(==[0-9][A-Za-z0-9.]*)?$")
FIELDS = ("name", "dockerfiles", "dockerfile", "base", "source", "packages", "provides", "replaces", "target", "issue")
MINUS_FIELDS = ("name", "from", "removes", "python", "provides", "replaces", "target", "issue")
DEB = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
PY = "/var/lib/openstack/bin/python3"


def check_common(b, where, seen, run):
    bad = ["%s: not a module name: %s" % (where, m) for m in b["provides"] if not MODULE.match(str(m))]
    if not TARGET.match(str(b["target"])):
        bad.append("%s: target must be ghcr.io/krateo-blueprints/images/<name>:<tag ending -YYYYMMDD>" % where)
    if b["target"] in seen:
        bad.append("%s: another entry has the same target" % where)
    seen.add(b["target"])
    if b["replaces"] not in run and b["target"] not in run:
        bad.append("%s: the charts run neither %s nor %s, so the entry is stale" % (where, b["replaces"], b["target"]))
    return bad


def check(builds, run):
    """Failures for the --check rules. `run` maps every image the charts run to its recorded digest."""
    bad, seen = [], set()
    for i, b in enumerate(builds):
        b = b or {}
        where = "hack/image-build.yaml entry %d (%s)" % (i + 1, b.get("name"))
        minus = "removes" in b
        missing = [f for f in (MINUS_FIELDS if minus else FIELDS) if not b.get(f)]
        if missing:
            bad.append("%s: missing %s" % (where, ", ".join(missing)))
            continue
        bad += check_common(b, where, seen, run)
        if minus:
            if not PINNED.match(str(b["from"])):
                bad.append("%s: from is not pinned by digest: %s" % (where, b["from"]))
            elif b["replaces"] in run and not (b["from"].split("@")[0] == b["replaces"].rsplit(":", 1)[0] and b["from"].split("@")[1] == run[b["replaces"]]):
                bad.append("%s: from is %s, and the build hack/images.yaml recorded for %s is %s" % (where, b["from"], b["replaces"], run[b["replaces"]]))
            bad += ["%s: not a Debian package name: %s" % (where, x) for x in b["removes"] if not DEB.match(str(x))]
            if not str(b["python"]).startswith("/"):
                bad.append("%s: python is not an absolute path" % where)
            continue
        if not COMMIT.match(str(b["dockerfiles"].get("commit"))):
            bad.append("%s: dockerfiles.commit is not a full commit id" % where)
        s = b["source"]
        if not COMMIT.match(str(s.get("commit"))):
            bad.append("%s: source.commit is not a full commit id" % where)
        if not all(s.get(x) for x in ("repository_arg", "repository", "branch_arg", "branch")):
            bad.append("%s: source lacks repository_arg, repository, branch_arg or branch" % where)
        if set(b["base"]) != {"BASE_VENV_BUILDER", "BASE_RUNTIME"}:
            bad.append("%s: base must set BASE_VENV_BUILDER and BASE_RUNTIME" % where)
        for a, v in sorted(b["base"].items()):
            if not PINNED.match(str(v)):
                bad.append("%s: %s is not pinned by digest: %s" % (where, a, v))
        bad += ["%s: not a package requirement: %s" % (where, p) for p in b["packages"] if not PACKAGE.match(str(p))]
    return bad


def verify(b, got, up):
    """Failures for a built image. got/up: facts of the built image and of the one it replaces."""
    bad = []
    head = (got["project"].split() or [""])[0]
    if len(head) < 7 or not b["source"]["commit"].startswith(head):
        bad.append("%s was built from commit %r, want %s" % (b["name"], head, b["source"]["commit"]))
    for m in b["provides"]:
        if m not in got["imports"]:
            bad.append("%s: the built image cannot import %s" % (b["name"], m))
        if m in up["imports"]:
            bad.append("%s: %s already imports %s, so this build is not needed: point the chart at it" % (b["name"], b["replaces"], m))
    for k in ("User", "Entrypoint", "Cmd", "WorkingDir", "PATH"):
        if got["config"].get(k) != up["config"].get(k):
            bad.append("%s: %s is %r, upstream's is %r" % (b["name"], k, got["config"].get(k), up["config"].get(k)))
    lost = sorted(set(up["pip"]) - set(got["pip"]))
    if not up["pip"] or not got["pip"]:
        bad.append("%s: no Python package list was read (built %d, upstream %d)" % (b["name"], len(got["pip"]), len(up["pip"])))
    elif lost:
        bad.append("%s: Python packages of upstream's image are missing: %s" % (b["name"], ", ".join(lost)))
    return bad


def verify_minus(b, got, up):
    """Failures for an upstream build minus packages."""
    bad = []
    want = set(up["deb"]) - set(b["removes"])
    absent = sorted(set(b["removes"]) - set(up["deb"]))
    if not up["deb"] or not up["pip"]:
        return ["%s: no package list was read (Debian %d, Python %d)" % (b["name"], len(up["deb"]), len(up["pip"]))]
    if absent:
        bad.append("%s: upstream's image does not have %s: the list is stale" % (b["name"], ", ".join(absent)))
    if set(got["deb"]) != want:
        bad.append("%s: Debian packages differ beyond the list: lost %s, gained or kept %s" % (b["name"], sorted(want - set(got["deb"])), sorted(set(got["deb"]) - want)))
    if got["pip"] != up["pip"]:
        bad.append("%s: the Python packages changed: %s" % (b["name"], sorted(set(got["pip"].items()) ^ set(up["pip"].items()))))
    for m in b["provides"]:
        for who, f in (("the built image", got), (b["replaces"], up)):
            if m not in f["imports"]:
                bad.append("%s: %s cannot import %s" % (b["name"], who, m))
    for k in ("User", "Entrypoint", "Cmd", "WorkingDir", "PATH"):
        if got["config"].get(k) != up["config"].get(k):
            bad.append("%s: %s is %r, upstream's is %r" % (b["name"], k, got["config"].get(k), up["config"].get(k)))
    return bad


def sh(*args, **kw):
    r = subprocess.run(list(args), capture_output=True, text=True, **kw)
    return r.returncode, r.stdout, r.stderr


def pip_list(text):
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:==(\S+)| @ (\S+))", line.strip())
        if m:
            out[m.group(1).lower().replace("_", "-")] = m.group(2) or "from source"
    return out


def facts(image, modules, py=PY, packages=False):
    rc, out, err = sh("docker", "image", "inspect", image)
    if rc != 0:
        raise RuntimeError("docker image inspect %s: %s" % (image, err.strip()))
    c = json.loads(out)[0]["Config"]
    env = dict(e.split("=", 1) for e in c.get("Env") or [] if "=" in e)
    conf = {k: c.get(k) for k in ("User", "Entrypoint", "Cmd", "WorkingDir")}
    conf["PATH"] = env.get("PATH")
    code = ("import importlib,sys\nfor m in sys.argv[1:]:\n  try:\n    importlib.import_module(m); print('IMPORTS', m)\n"
            "  except ImportError: print('LACKS', m)\n")
    rc, out, err = sh("docker", "run", "--rm", "--network", "none", "--entrypoint", py, image, "-c", code, *modules)
    if rc != 0:
        raise RuntimeError("%s cannot run its Python: %s" % (image, err.strip()[-300:]))
    said = {line.split()[1]: line.split()[0] for line in out.splitlines() if line.startswith(("IMPORTS ", "LACKS "))}
    if set(said) != set(modules):
        raise RuntimeError("%s did not answer for every module: %s" % (image, out))
    if packages:
        code = "import importlib.metadata as m\nfor d in m.distributions(): print(d.metadata['Name'], d.version)"
        rc, out, err = sh("docker", "run", "--rm", "--network", "none", "--entrypoint", py, image, "-c", code)
        rc2, deb, err2 = sh("docker", "run", "--rm", "--network", "none", "--entrypoint", "dpkg-query", image, "-W", "-f", "${db:Status-Status} ${Package}\\n")
        if rc or rc2:
            raise RuntimeError("%s: its packages cannot be listed: %s" % (image, (err + err2).strip()[-300:]))
        return {"config": conf, "imports": {m for m, a in said.items() if a == "IMPORTS"},
                "pip": dict(line.split(" ", 1) for line in out.splitlines() if " " in line),
                "deb": {line.split()[1] for line in deb.splitlines() if line.startswith("installed ")}}
    _, proj, _ = sh("docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", image, "/etc/image_info/project.txt")
    _, pips, _ = sh("docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", image, "/etc/image_info/pip.txt")
    return {"config": conf, "imports": {m for m, a in said.items() if a == "IMPORTS"}, "project": proj, "pip": pip_list(pips)}


def branch_at(repo, branch):
    rc, out, err = sh("git", "ls-remote", repo, "refs/heads/%s" % branch)
    if rc != 0 or not out.strip():
        raise RuntimeError("git ls-remote %s %s: %s" % (repo, branch, err.strip() or "no such branch"))
    return out.split()[0]


def build_minus(b, d):
    rc, _, err = sh("docker", "pull", "-q", "--platform", "linux/amd64", b["from"])
    if rc != 0:
        raise RuntimeError("docker pull %s: %s" % (b["from"], err.strip()[-300:]))
    up = facts(b["from"], b["provides"], b["python"], True)
    user = up["config"].get("User") or "root"
    with open(os.path.join(d, "Dockerfile"), "w") as f:
        f.write("FROM %s\nUSER root\nRUN dpkg --purge %s\nUSER %s\n" % (b["from"], " ".join(b["removes"]), user))
    local = "krateo-build/%s:check" % b["name"]
    if subprocess.run(["docker", "buildx", "build", "--platform", "linux/amd64", "--load", "--provenance=false", "--network", "none", "-t", local,
                       "--label", "org.opencontainers.image.source=https://github.com/krateo-blueprints/krateo-openstack-blueprint",
                       "--label", "io.krateo.build.from=%s" % b["from"], d]).returncode != 0:
        raise RuntimeError("docker build failed (dpkg refuses to remove a package something else depends on)")
    got = facts(local, b["provides"], b["python"], True)
    if not up["config"].get("User"):
        got["config"]["User"] = up["config"].get("User") if got["config"].get("User") == "root" else got["config"].get("User")
    print("   %d Debian package(s) removed, %d left" % (len(up["deb"]) - len(got["deb"]), len(got["deb"])))
    return local, verify_minus(b, got, up)


def build(b, tmp):
    d = os.path.join(tmp, b["name"])
    os.makedirs(d)
    if "removes" in b:
        return build_minus(b, d)
    for args in (("init", "-q"), ("fetch", "-q", "--depth", "1", b["dockerfiles"]["repository"], b["dockerfiles"]["commit"]),
                 ("checkout", "-q", "FETCH_HEAD")):
        rc, _, err = sh("git", *args, cwd=d)
        if rc != 0:
            raise RuntimeError("git %s: %s" % (args[0], err.strip()[-300:]))
    rc, out, _ = sh("git", "rev-parse", "HEAD", cwd=d)
    if out.strip() != b["dockerfiles"]["commit"]:
        raise RuntimeError("the Dockerfile's checkout is at %s, want %s" % (out.strip(), b["dockerfiles"]["commit"]))
    s = b["source"]
    at = branch_at(s["repository"], s["branch"])
    if at != s["commit"]:
        raise RuntimeError("%s is at %s, and this entry pins %s: review the new commits, then move the pin and give the "
                           "target a new date" % (s["branch"], at, s["commit"]))
    local = "krateo-build/%s:check" % b["name"]
    cmd = ["docker", "buildx", "build", "--platform", "linux/amd64", "--load", "--provenance=false", "-f", os.path.join(d, b["dockerfile"]), "-t", local,
           "--build-arg", "%s=%s" % (s["repository_arg"], s["repository"]), "--build-arg", "%s=%s" % (s["branch_arg"], s["branch"]),
           "--build-arg", "EXTRA_PYTHON_PACKAGES=%s" % " ".join(b["packages"]),
           "--label", "org.opencontainers.image.source=https://github.com/krateo-blueprints/krateo-openstack-blueprint",
           "--label", "io.krateo.build.dockerfiles=%s@%s" % (b["dockerfiles"]["repository"], b["dockerfiles"]["commit"]),
           "--label", "io.krateo.build.source=%s@%s" % (s["repository"], s["commit"])]
    for a, v in sorted(b["base"].items()):
        cmd += ["--build-arg", "%s=%s" % (a, v)]
    if subprocess.run(cmd + [d]).returncode != 0:
        raise RuntimeError("docker build failed")
    rc, _, err = sh("docker", "pull", "-q", "--platform", "linux/amd64", b["replaces"])
    if rc != 0:
        raise RuntimeError("docker pull %s: %s" % (b["replaces"], err.strip()[-300:]))
    got, up = facts(local, b["provides"]), facts(b["replaces"], b["provides"])
    for p in sorted(set(got["pip"]) | set(up["pip"])):
        if got["pip"].get(p) != up["pip"].get(p):
            print("   %s: %s -> %s" % (p, up["pip"].get(p), got["pip"].get(p)))
    return local, verify(b, got, up)


def mirror():
    spec = importlib.util.spec_from_file_location("image_mirror", os.path.join(HERE, "image-mirror.py"))
    src = open(spec.origin).read()
    ns = {"__file__": spec.origin, "__name__": "image_mirror"}
    exec(compile(src[:src.rindex("\nmain()")], spec.origin, "exec"), ns)
    return ns


def run_all(root, builds, publish):
    bad = []
    anon = mirror()["anonymous_digest"]
    with tempfile.TemporaryDirectory() as tmp:
        for b in builds:
            print("== %s -> %s" % (b["name"], b["target"]), flush=True)
            try:
                dig, status = anon(root, b["target"])
                if dig:
                    print("present %s (%s)" % (b["target"], dig))    # a published tag is never moved, so never rebuilt
                    continue
                if publish:
                    if status not in (401, 403, 404):
                        bad.append("%s: the registry answered HTTP %s, so whether it exists is unknown" % (b["target"], status))
                        continue
                    if sh("docker", "manifest", "inspect", b["target"])[0] == 0:
                        bad.append("%s exists but is not pullable anonymously (HTTP %s): an org owner must make the GHCR "
                                   "package public once (package settings, Change visibility)" % (b["target"], status))
                        continue
                local, fails = build(b, tmp)
                bad += fails
                if fails or not publish:
                    continue
                for args in (("tag", local, b["target"]), ("push", b["target"])):
                    rc, out, err = sh("docker", *args)
                    if rc != 0:
                        raise RuntimeError("docker %s: %s" % (args[0], err.strip()[-300:]))
                dig, status = anon(root, b["target"])
                print("published %s: %s" % (b["target"], dig))
                if not dig:
                    bad.append("%s was pushed and is not pullable anonymously (HTTP %s): an org owner must make the GHCR "
                               "package public once (package settings, Change visibility)" % (b["target"], status))
            except RuntimeError as e:
                bad.append("%s: %s" % (b["name"], e))
    return bad


def self_test():
    import copy
    ok = {"name": "a", "dockerfiles": {"repository": "https://x/d", "commit": "1" * 40}, "dockerfile": "a/Dockerfile",
          "base": {"BASE_VENV_BUILDER": "quay.io/x/v@sha256:" + "2" * 64, "BASE_RUNTIME": "quay.io/x/b@sha256:" + "3" * 64},
          "source": {"repository_arg": "A_REPO", "repository": "https://x/a", "branch_arg": "A_REF", "branch": "stable/1", "commit": "abcdef0" + "4" * 33},
          "packages": ["keystonemiddleware[memcache_encryption]"], "provides": ["cryptography"], "replaces": "quay.io/x/a:1-20261117",
          "target": "ghcr.io/krateo-blueprints/images/a:1-20261124", "issue": 1}

    def w(path, v):
        b = copy.deepcopy(ok)
        d = b
        for k in path[:-1]:
            d = d[k]
        d[path[-1]] = v
        return b
    run = {ok["replaces"]: "sha256:" + "9" * 64}
    mi = {"name": "m", "from": "quay.io/x/a@sha256:" + "9" * 64, "removes": ["linux-libc-dev", "g++"], "python": "/p/python3", "provides": ["a"],
          "replaces": ok["replaces"], "target": ok["target"], "issue": 1}
    mu = {"config": {"User": "a", "PATH": "/p"}, "imports": {"a"}, "pip": {"a": "1"}, "deb": {"bash", "g++", "linux-libc-dev"}}
    mg = dict(mu, deb={"bash"})
    good = {"config": {"User": "a", "PATH": "/p"}, "imports": {"cryptography"}, "project": "abcdef0 A commit", "pip": {"x": "1", "cryptography": "43"}}
    up = {"config": {"User": "a", "PATH": "/p"}, "imports": set(), "project": "abcdef0 A commit", "pip": {"x": "1"}}
    cases = [
        ("as it must be", check([ok], run), 0),
        ("the charts moved to the target", check([ok], {ok["target"]: None}), 0),
        ("stale", check([ok], {"quay.io/x/z:1.0.0": None}), 1),
        ("a short Dockerfile commit", check([w(["dockerfiles", "commit"], "1eb5ce15")], run), 1),
        ("a branch for a commit", check([w(["source", "commit"], "stable/1")], run), 1),
        ("a base by tag", check([w(["base", "BASE_RUNTIME"], "quay.io/x/b:2026.1")], run), 1),
        ("a base missing", check([w(["base"], {"BASE_RUNTIME": "quay.io/x/b@sha256:" + "3" * 64})], run), 1),
        ("a target with no date", check([w(["target"], "ghcr.io/krateo-blueprints/images/a:1")], run), 1),
        ("a target outside the org", check([w(["target"], "ghcr.io/other/a:1-20261124")], run), 1),
        ("a shared target", check([ok, dict(ok, name="b")], run), 1),
        ("a package line with a shell", check([w(["packages"], ["x; curl y"])], run), 1),
        ("no packages", check([w(["packages"], [])], run), 1),
        ("an empty list", check([], run), 0),
        ("minus: as it must be", check([mi], run), 0),
        ("minus: the charts moved to the target", check([dict(mi, **{"from": "quay.io/x/a@sha256:" + "8" * 64})], {ok["target"]: None}), 0),
        ("minus: from by tag", check([dict(mi, **{"from": "quay.io/x/a:1"})], run), 1),
        ("minus: from another build than the one scanned", check([dict(mi, **{"from": "quay.io/x/a@sha256:" + "8" * 64})], run), 1),
        ("minus: from another image", check([dict(mi, **{"from": "quay.io/x/b@sha256:" + "9" * 64})], run), 1),
        ("minus: a package line with a shell", check([dict(mi, removes=["x; rm -rf /"])], run), 1),
        ("minus: nothing to remove", check([dict(mi, removes=[])], run), 1),
        ("minus: built well", verify_minus(mi, mg, mu), 0),
        ("minus: one listed package still there", verify_minus(mi, dict(mg, deb={"bash", "g++"}), mu), 1),
        ("minus: an unlisted package gone", verify_minus(mi, dict(mg, deb=set()), mu), 1),
        ("minus: a stale list", verify_minus(dict(mi, removes=["linux-libc-dev", "g++", "zz"]), mg, mu), 1),
        ("minus: a Python version changed", verify_minus(mi, dict(mg, pip={"a": "2"}), mu), 1),
        ("minus: cannot import", verify_minus(mi, dict(mg, imports=set()), mu), 1),
        ("minus: another user", verify_minus(mi, dict(mg, config={"User": "root", "PATH": "/p"}), mu), 1),
        ("minus: no package list", verify_minus(mi, mg, dict(mu, deb=set())), 1),
        ("built well", verify(ok, good, up), 0),
        ("another commit", verify(ok, dict(good, project="1234567 other"), up), 1),
        ("no commit", verify(ok, dict(good, project=""), up), 1),
        ("still cannot import it", verify(ok, dict(good, imports=set()), up), 1),
        ("upstream caught up", verify(ok, good, dict(up, imports={"cryptography"})), 1),
        ("another user", verify(ok, dict(good, config={"User": "root", "PATH": "/p"}), up), 1),
        ("a package lost", verify(ok, dict(good, pip={"cryptography": "43"}), up), 1),
        ("no package list", verify(ok, dict(good, pip={}), up), 1),
        ("pip.txt forms", [] if pip_list("Foo_bar==1.2\nmasakari @ file:///tmp/masakari\n# c\n") ==
         {"foo-bar": "1.2", "masakari": "from source"} else ["x"], 0),
    ]
    wrong = [(n, g, x) for n, g, x in cases if len(g) != x]
    for n, g, x in wrong:
        print("SELF-TEST FAIL: %s: %d finding(s), want %d: %s" % (n, len(g), x, g))
    print("self-test: %d case(s), %d wrong" % (len(cases), len(wrong)))
    return 1 if wrong else 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = os.path.abspath(args[0] if args else ".")
    builds = (yaml.safe_load(open(os.path.join(root, "hack", "image-build.yaml"))) or {}).get("builds") or []
    inv = (yaml.safe_load(open(os.path.join(root, "hack", "images.yaml"))) or {}).get("images") or []
    if not inv:
        print("FAILED MEASUREMENT: hack/images.yaml lists no image")
        return 2
    if not builds:
        print("FAILED MEASUREMENT: hack/image-build.yaml lists no build: remove it, its workflow and its gate instead")
        return 2
    bad = check(builds, {e["image"]: (e.get("registry_facts") or {}).get("digest") for e in inv})
    mode = "checked"
    if not bad and ("--build" in sys.argv or "--publish" in sys.argv):
        mode = "published or present" if "--publish" in sys.argv else "built and verified"
        bad = run_all(root, builds, "--publish" in sys.argv)
    for x in bad:
        print("FAIL: " + x)
    if bad:
        return 1
    print("OK: %d image(s) %s" % (len(builds), mode))
    return 0


if __name__ == "__main__":
    sys.exit(main())
