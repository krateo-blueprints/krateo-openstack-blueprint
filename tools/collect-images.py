#!/usr/bin/env python3
"""Print every container image referenced by a rendered Helm manifest stream.

Reads one or more multi-document YAML files and walks them structurally, so a
string that merely *looks* like an image field -- a shell comment inside a
ConfigMap, a line of documentation in a mounted python script -- cannot be
mistaken for one. Only a mapping key named `image` whose sibling key `name`
exists (i.e. a container spec) is taken, which is what distinguishes a real
container entry from `values.yaml`-style `images:` dictionaries that survive
into annotations.

Exits non-zero when the input yields nothing, because a loader that silently
pre-loads zero images reports success for a cluster that will then pull at
runtime -- the exact failure pre-loading exists to prevent.
"""
import sys

try:
    import yaml
except ImportError:
    sys.exit("FATAL: PyYAML is required by collect-images.py and is not installed")


def walk(node, out):
    if isinstance(node, dict):
        img = node.get("image")
        # A container spec always carries a name alongside its image. Anything else
        # with an `image` key is configuration data, not something to pull.
        if isinstance(img, str) and img.strip() and "name" in node:
            out.add(img.strip())
        for v in node.values():
            walk(v, out)
    elif isinstance(node, list):
        for v in node:
            walk(v, out)


def main(argv):
    if len(argv) < 2:
        sys.exit("usage: collect-images.py <rendered.yaml> [...]")
    found = set()
    for path in argv[1:]:
        with open(path) as fh:
            for doc in yaml.safe_load_all(fh):
                walk(doc, found)
    if not found:
        sys.exit("FATAL: no container images found in: " + " ".join(argv[1:]))
    for img in sorted(found):
        print(img)


if __name__ == "__main__":
    main(sys.argv)
