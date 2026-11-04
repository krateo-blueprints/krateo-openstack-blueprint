#!/usr/bin/env python3
"""No gate may exit 0 when it could not measure anything.

Four of this repo's gates printed `SKIP: PyYAML unavailable` and exited 0 for weeks. The lint
job was green on every run while nothing was checked -- including the gate written specifically
to protect the keystone fernet fix, the most serious defect the repo had shipped, where 100% of
authentication returned 500 while every layer reported Ready. Its guard could not fire.

#120 fixed the CI side by adding `pip install PyYAML`, which made them run. It did NOT remove
the code path that passes when they cannot, so the vacuous pass sat in all four for weeks more:
remove that one line from the workflow, or have the install fail, and the suite goes quiet and
green again. This gate removes the possibility rather than the symptom.

The rule: an `except ImportError` handler in a gate must end the process with a NON-ZERO status,
or re-raise. Printing a message and exiting 0 is the defect; printing a message and exiting 2 is
the fix. Checked structurally with `ast`, not by grepping for "SKIP" -- a text search over these
files also matches the comments that document the history, which has produced false readings in
this repo twice.

Exits non-zero when it finds no gates to inspect, for the reason it exists.
"""
import ast
import os
import sys

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HACK = os.path.join(ROOT, "hack")


def handles_importerror(handler):
    t = handler.type
    if t is None:
        return True  # a bare `except:` catches ImportError too
    names = []
    if isinstance(t, ast.Name):
        names = [t.id]
    elif isinstance(t, ast.Tuple):
        names = [e.id for e in t.elts if isinstance(e, ast.Name)]
    return any(n in ("ImportError", "ModuleNotFoundError", "Exception") for n in names)


def exits_nonzero_call(node, terminators):
    """A call to a module-level helper that itself always exits non-zero."""
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    f = node.value.func
    return isinstance(f, ast.Name) and f.id in terminators


def terminates_nonzero(body, terminators=frozenset()):
    """True if this handler body certainly ends the process with a non-zero status.

    `terminators` names module-level helpers that always exit non-zero, because most gates here
    route through one (`measurement_failed`, `fail`) rather than calling sys.exit inline. The
    first version of this check did not follow that one level of indirection and reported SIX
    already-correct gates as defective -- a convincing false instance, produced because the
    check was written while primed for the bug it was looking for. One level is enough for this
    codebase; a helper that calls a helper would be reported, which is the safe direction.
    """
    for node in body:
        if exits_nonzero_call(node, terminators):
            return True
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            f = node.value.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name == "exit":
                args = node.value.args
                if not args:
                    return False                      # sys.exit() is status 0
                a = args[0]
                if isinstance(a, ast.Constant):
                    # A string argument exits 1 and prints it; an int must be non-zero.
                    if isinstance(a.value, str):
                        return bool(a.value)
                    return a.value not in (0, None)
                return True                            # a computed status; assume intentional
    return False


def main():
    if not os.path.isdir(HACK):
        sys.exit(f"FAIL: no hack directory at {HACK} -- nothing was measured")
    gates = sorted(f for f in os.listdir(HACK)
                   if f.startswith("validate-") and f.endswith(".py"))
    if not gates:
        sys.exit("FAIL: no validate-*.py gates found -- this check inspected nothing, which is "
                 "the condition it exists to forbid")

    problems = []
    handlers = 0
    for g in gates:
        path = os.path.join(HACK, g)
        try:
            tree = ast.parse(open(path).read(), filename=path)
        except SyntaxError as e:
            problems.append(f"{g}: does not parse: {e}")
            continue
        # Module-level helpers that always exit non-zero: the handlers call these.
        terminators = {fn.name for fn in tree.body
                       if isinstance(fn, ast.FunctionDef) and terminates_nonzero(fn.body)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if not any(isinstance(n, ast.Import) or isinstance(n, ast.ImportFrom)
                       for n in node.body):
                continue
            for h in node.handlers:
                if not handles_importerror(h):
                    continue
                handlers += 1
                if not terminates_nonzero(h.body, terminators):
                    problems.append(
                        f"{g}:{h.lineno} catches a failed import and does not exit non-zero -- "
                        "a missing parser would make this gate report success without "
                        "measuring anything")

    if handlers == 0:
        print("FAIL: no gate guards an import at all -- either the gates stopped importing "
              "their parsers or this check is looking in the wrong place")
        for p in problems:
            print("  " + p)
        return 1
    if problems:
        print(f"FAIL: {len(problems)} gate(s) can pass without measuring "
              f"({handlers} import guards inspected across {len(gates)} gates)")
        for p in sorted(set(problems)):
            print("  " + p)
        return 1
    print(f"OK: all {handlers} import guards in {len(gates)} gates exit non-zero when their "
          "parser is missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
