#!/usr/bin/env python3
{{/*
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/}}
"""krateo (#444, upstream divergence skyline-444): make skyline-nginx-generator's config tolerate an endpoint whose
host does not resolve.

    skyline-nginx-tolerant.py <nginx.conf> [<resolv.conf>]     rewrites <nginx.conf> in place

skyline-nginx-generator (skyline-apiserver cmd/generate_nginx.py, templates/nginx.conf.j2) writes one location per
catalog endpoint, of the form `proxy_pass <scheme>://<host:port><path>;`, with a literal host and no `resolver`. nginx
resolves every literal proxy_pass host when it LOADS its config, so a single catalog entry whose host does not resolve
makes nginx refuse to start. The 0.3.71 -> 0.3.74 upgrade test found exactly that: manila's endpoints outlived its
removal, pointing at the deleted manila-api Service, and Skyline's nginx crash-looped. This turns each such location into
the same proxy, resolved per request:

    set $sky_up "<host:port>";
    rewrite ^<location>(.*)$ <path>$1 break;
    proxy_pass <scheme>://$sky_up;

- **The URI it passes is the same:** the literal form replaced the location prefix of the normalized request URI with
  <path>; the rewrite does the same, and a proxy_pass with a variable and no URI passes the rewritten URI. The query
  string is kept, because the rewrite target has no `?`.
- **The resolver is derived:** `resolver` is set to the nameservers of the pod's own resolv.conf, the cluster DNS. It
  is never hard-coded. An IPv6 address is bracketed.
- **The result:** a host that does not resolve fails its own location at request time, with a 502, and nginx still
  starts.

Locations that proxy to an `upstream` block (skyline's own API, over its unix socket) are left alone, since they
resolve nothing. With no nameserver in resolv.conf, the script refuses (exit 1): every rewritten location would fail.
hack/validate-skyline-nginx.py runs this script on a config generated from upstream's template, runs `nginx -t` on the
config before (it must fail on the unresolvable host) and after (it must pass), and proxies a request through it.
"""
import re
import sys


def nameservers(path):
    out = []
    for line in open(path):
        f = line.split()
        if len(f) >= 2 and f[0] == "nameserver":
            out.append("[%s]" % f[1] if ":" in f[1] and not f[1].startswith("[") else f[1])
    return out


PROXY = re.compile(r"^(?P<ind>[ \t]*)proxy_pass[ \t]+(?P<scheme>https?)://(?P<host>[^/\s;]+)(?P<path>/[^\s;]*)?;[ \t]*$", re.M)
LOCATION = re.compile(r"location[ \t]+(?P<loc>/\S*)[ \t]*\{[^{}]*$")


def tolerant(text, ns):
    upstreams = set(re.findall(r"^[ \t]*upstream[ \t]+(\S+)[ \t]*\{", text, re.M))
    out, pos, n = [], 0, 0
    for m in PROXY.finditer(text):
        if m.group("host") in upstreams:
            continue
        # the location this proxy_pass is in: the nearest `location <prefix> {` above it, with no brace in between
        head = text[:m.start()]
        lm = None
        for lm in LOCATION.finditer(head):
            pass
        if lm is None or "}" in head[lm.end():]:
            raise SystemExit("skyline-nginx-tolerant: a proxy_pass to %s is not inside a prefix location: refusing"
                             % m.group("host"))
        loc, path, ind = lm.group("loc"), m.group("path") or "/", m.group("ind")
        if not loc.endswith("/") or not path.endswith("/"):
            raise SystemExit("skyline-nginx-tolerant: location %s / path %s do not both end in '/': refusing" % (loc, path))
        out.append(text[pos:m.start()])
        out.append('%sset $sky_up "%s";\n%srewrite ^%s(.*)$ %s$1 break;\n%sproxy_pass %s://$sky_up;'
                   % (ind, m.group("host"), ind, re.escape(loc).replace("\\/", "/"), path, ind, m.group("scheme")))
        pos, n = m.end(), n + 1
    out.append(text[pos:])
    text = "".join(out)
    http = re.search(r"^[ \t]*http[ \t]*\{[ \t]*$", text, re.M)
    if not http:
        raise SystemExit("skyline-nginx-tolerant: no `http {` block: refusing")
    text = text[:http.end()] + "\n    # krateo (#444): endpoint hosts resolve per request, through the cluster DNS\n" \
        "    resolver %s valid=30s;\n" % " ".join(ns) + text[http.end():]
    return text, n


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit("usage: skyline-nginx-tolerant.py <nginx.conf> [<resolv.conf>]")
    conf, resolv = sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else "/etc/resolv.conf"
    ns = nameservers(resolv)
    if not ns:
        sys.exit("skyline-nginx-tolerant: no nameserver in %s: every endpoint location would fail, refusing" % resolv)
    text, n = tolerant(open(conf).read(), ns)
    open(conf, "w").write(text)
    print("skyline-nginx-tolerant: %d endpoint location(s) resolve per request through %s" % (n, " ".join(ns)))


if __name__ == "__main__":
    main()
