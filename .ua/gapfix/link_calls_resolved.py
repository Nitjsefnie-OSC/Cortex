"""Call graph v2: `calls` edges resolved through imports, re-exports, classes and known
types.

Resolution rules (no name-similarity guessing; an unresolvable call is skipped):
  f(...)                 -> def in this module, or an imported symbol (followed through
                            re-exporting modules, depth <= 6)
  mod.f(...)             -> symbol f of the module bound to `mod` by an import
  C(...)                 -> class C (and C.__init__ when C defines it)
  C.m(...)               -> method m of class C or of its resolved bases
  self.m / cls.m(...)    -> method m of the enclosing class or its resolved bases
  super().m(...)         -> method m of a resolved base of the enclosing class
  v.m(...)               -> method m of class C when v is a parameter or local annotated
  `C`
                            (or Optional[C] / C | None) or assigned `v = C(...)` in the
                            function
Methods are graph nodes `function:<file>:<name>`; a method edge is added only when that
name is defined in the class body. Evidence = caller file:line.

usage: python3 link_calls_resolved.py <repo root> <knowledge-graph.json>
"""

import ast
import json
import sys
from pathlib import Path

# source: bound on followed re-exports / base classes; a project chain deeper
# than this was not observed in Cortex (cycle guard, not a tuned value)
MAX_DEPTH = 6

ROOT = Path(sys.argv[1])
GP = Path(sys.argv[2])
SCAN = ("mcp_server/", "scripts/", "benchmarks/", "plugins/", "tests_py/", "fuzz/")


def mod_path(mod):
    p = ROOT / Path(*mod.split("."))
    if p.with_suffix(".py").is_file():
        return p.with_suffix(".py").relative_to(ROOT).as_posix()
    if (p / "__init__.py").is_file():
        return (p / "__init__.py").relative_to(ROOT).as_posix()
    return None


def mod_name(path):
    parts = path[:-3].split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


class Module:
    def __init__(self, path, tree):
        self.path, self.tree, self.name = path, tree, mod_name(path)
        self.pkg = (
            self.name if path.endswith("__init__.py") else self.name.rpartition(".")[0]
        )
        self.defs, self.classes, self.imports = {}, {}, {}
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defs[n.name] = n
            elif isinstance(n, ast.ClassDef):
                self.defs[n.name] = n
                self.classes[n.name] = n
        self.imports = import_table(tree.body, self.pkg, top=True)


def import_table(stmts, pkg, top=False):
    table = {}
    walker = stmts if top else [s for st in stmts for s in ast.walk(st)]
    for n in walker:
        if isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if n.level:
                base = pkg.split(".") if pkg else []
                base = base[: len(base) - (n.level - 1)] if n.level > 1 else base
                mod = ".".join(base + ([mod] if mod else []))
            for a in n.names:
                if a.name == "*":
                    continue
                sub = mod_path(f"{mod}.{a.name}") if mod else None
                table[a.asname or a.name] = (
                    ("module", sub) if sub else ("symbol", mod, a.name)
                )
        elif isinstance(n, ast.Import):
            for a in n.names:
                p = mod_path(a.name)
                if p:
                    table[a.asname or a.name.split(".")[0]] = (
                        "module",
                        p if a.asname else mod_path(a.name.split(".")[0]) or p,
                    )
    return table


MODS = {}


def load(path):
    if path in MODS:
        return MODS[path]
    try:
        MODS[path] = Module(path, ast.parse((ROOT / path).read_text(encoding="utf-8")))
    except (SyntaxError, OSError, UnicodeDecodeError):
        MODS[path] = None
    return MODS[path]


def resolve_symbol(path, name, depth=0):
    """(path, name, node) where `name` is defined, following imports/re-exports."""
    m = load(path) if path else None
    if not m or depth > MAX_DEPTH:
        return None
    if name in m.defs:
        return (path, name, m.defs[name])
    imp = m.imports.get(name)
    if imp and imp[0] == "symbol":
        p = mod_path(imp[1]) if imp[1] else None
        return resolve_symbol(p, imp[2], depth + 1) if p else None
    return None


def resolve_ref(m, local_imports, name):
    if name in m.defs:
        return (m.path, name, m.defs[name])
    imp = local_imports.get(name) or m.imports.get(name)
    if imp and imp[0] == "symbol":
        p = mod_path(imp[1]) if imp[1] else None
        return resolve_symbol(p, imp[2]) if p else None
    return None


def module_of(m, local_imports, name):
    imp = local_imports.get(name) or m.imports.get(name)
    return imp[1] if imp and imp[0] == "module" else None


def class_bases(cpath, cnode):
    m = load(cpath)
    out = []
    for b in cnode.bases:
        r = None
        if isinstance(b, ast.Name):
            r = resolve_ref(m, {}, b.id)
        elif isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name):
            mp = module_of(m, {}, b.value.id)
            r = resolve_symbol(mp, b.attr) if mp else None
        if r and isinstance(r[2], ast.ClassDef):
            out.append(r)
    return out


def find_method(cls, meth, depth=0, include_self=True):
    cpath, cname, cnode = cls
    if include_self:
        for n in cnode.body:
            if (
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == meth
            ):
                return (cpath, meth)
    if depth > MAX_DEPTH:
        return None
    for b in class_bases(cpath, cnode):
        r = find_method(b, meth, depth + 1)
        if r:
            return r
    return None


def ann_class(m, local_imports, ann):
    if ann is None:
        return None
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(ann, ast.BinOp):  # C | None
        return ann_class(m, local_imports, ann.left) or ann_class(
            m, local_imports, ann.right
        )
    if isinstance(ann, ast.Subscript):  # Optional[C]
        return ann_class(m, local_imports, ann.slice)
    if isinstance(ann, ast.Name):
        r = resolve_ref(m, local_imports, ann.id)
    elif isinstance(ann, ast.Attribute) and isinstance(ann.value, ast.Name):
        mp = module_of(m, local_imports, ann.value.id)
        r = resolve_symbol(mp, ann.attr) if mp else None
    else:
        return None
    return r if r and isinstance(r[2], ast.ClassDef) else None


def callee_targets(m, fn, cls, call, local_imports, types):
    f = call.func
    if isinstance(f, ast.Name):
        r = resolve_ref(m, local_imports, f.id)
        if not r:
            return []
        out = [("class" if isinstance(r[2], ast.ClassDef) else "function", r[0], r[1])]
        if isinstance(r[2], ast.ClassDef):
            init = find_method(r, "__init__")
            if init:
                out.append(("function", init[0], "__init__"))
        return out
    if not isinstance(f, ast.Attribute):
        return []
    base, meth = f.value, f.attr
    target_cls = None
    if isinstance(base, ast.Name):
        if base.id in ("self", "cls") and cls:
            target_cls = cls
        elif base.id in types:
            target_cls = types[base.id]
        else:
            mp = module_of(m, local_imports, base.id)
            if mp:
                r = resolve_symbol(mp, meth)
                return (
                    [
                        (
                            "class" if isinstance(r[2], ast.ClassDef) else "function",
                            r[0],
                            r[1],
                        )
                    ]
                    if r
                    else []
                )
            r = resolve_ref(m, local_imports, base.id)
            if r and isinstance(r[2], ast.ClassDef):
                target_cls = r
    elif (
        isinstance(base, ast.Call)
        and isinstance(base.func, ast.Name)
        and base.func.id == "super"
        and cls
    ):
        r = find_method(cls, meth, include_self=False)
        return [("function", r[0], meth)] if r else []
    if target_cls:
        r = find_method(target_cls, meth)
        if r:
            return [("function", r[0], meth)]
        if isinstance(base, ast.Name) and base.id in ("self", "cls"):
            return [("function", rp, meth) for rp in composed_method(target_cls, meth)]
    return []


def composed_method(cls, meth):
    """`self.m` inside a mixin: m defined by a sibling base of a class composing the
    mixin."""
    out = set()
    for sub in SUBCLASSES.get((cls[0], cls[1]), ()):
        r = find_method(sub, meth)
        if r:
            out.add(r[0])
    return sorted(out)


SUBCLASSES = {}


def index_subclasses(paths):
    """(mixin path, name) -> classes having it among their transitive bases."""
    for p in paths:
        m = load(p)
        if not m:
            continue
        for cname, cnode in m.classes.items():
            stack, seen = [(p, cname, cnode)], set()
            while stack:
                cur = stack.pop()
                for b in class_bases(cur[0], cur[2]):
                    if (b[0], b[1]) in seen:
                        continue
                    seen.add((b[0], b[1]))
                    stack.append(b)
                    SUBCLASSES.setdefault((b[0], b[1]), []).append((p, cname, cnode))


def call_result_class(m, local_imports, func):
    """Class a call evaluates to: a constructor, or a factory annotated `-> C`."""
    r = None
    if isinstance(func, ast.Name):
        r = resolve_ref(m, local_imports, func.id)
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        mp = module_of(m, local_imports, func.value.id)
        r = resolve_symbol(mp, func.attr) if mp else None
    if not r:
        return None
    if isinstance(r[2], ast.ClassDef):
        return r
    if (
        isinstance(r[2], (ast.FunctionDef, ast.AsyncFunctionDef))
        and r[2].returns is not None
    ):
        return ann_class(load(r[0]), {}, r[2].returns)
    return None


def local_types(m, fn, local_imports):
    types = {}
    args = fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
    for a in args:
        c = ann_class(m, local_imports, a.annotation)
        if c:
            types[a.arg] = c
    for n in ast.walk(fn):
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            c = ann_class(m, local_imports, n.annotation)
            if c:
                types[n.target.id] = c
        elif (
            isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and isinstance(n.value, ast.Call)
        ):
            r = call_result_class(m, local_imports, n.value.func)
            if r:
                types[n.targets[0].id] = r
    return types


def references(fn):
    """Function objects passed as values (callbacks, registrations), not called here."""
    called = {id(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    for c in ast.walk(fn):
        if isinstance(c, ast.Call):
            for a in list(c.args) + [k.value for k in c.keywords]:
                if (
                    isinstance(a, (ast.Name, ast.Attribute))
                    and id(a) not in called
                    and isinstance(a.ctx, ast.Load)
                ):
                    yield a


def functions(m):
    for n in m.tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n, None
        elif isinstance(n, ast.ClassDef):
            for b in n.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield b, (m.path, n.name, n)


def main():
    g = json.loads(GP.read_text())
    ids = {n["id"] for n in g["nodes"]}
    have = {(e["source"], e["target"], e["type"]) for e in g["edges"]}
    paths = sorted(
        {
            n["filePath"]
            for n in g["nodes"]
            if n["filePath"].endswith(".py") and n["filePath"].startswith(SCAN)
        }
    )
    added = 0
    index_subclasses(paths)
    for p in paths:
        m = load(p)
        if not m:
            continue
        for fn, cls in functions(m):
            src = f"function:{p}:{fn.name}"
            if src not in ids:
                continue
            local_imports = import_table(fn.body, m.pkg)
            types = local_types(m, fn, local_imports)
            for call in (c for c in ast.walk(fn) if isinstance(c, ast.Call)):
                for kind, tp, tn in callee_targets(
                    m, fn, cls, call, local_imports, types
                ):
                    tid = f"{kind}:{tp}:{tn}"
                    if tid not in ids or tid == src or (src, tid, "calls") in have:
                        continue
                    g["edges"].append(
                        {
                            "source": src,
                            "target": tid,
                            "type": "calls",
                            "direction": "forward",
                            "weight": 0.8,
                            "evidence": f"{p}:{call.lineno} resolved call",
                        }
                    )
                    have.add((src, tid, "calls"))
                    added += 1
            for ref in references(fn):
                for kind, tp, tn in callee_targets(
                    m,
                    fn,
                    cls,
                    ast.Call(func=ref, args=[], keywords=[]),
                    local_imports,
                    types,
                ):
                    tid = f"{kind}:{tp}:{tn}"
                    if (
                        kind != "function"
                        or tid not in ids
                        or tid == src
                        or (src, tid, "calls") in have
                    ):
                        continue
                    g["edges"].append(
                        {
                            "source": src,
                            "target": tid,
                            "type": "calls",
                            "direction": "forward",
                            "weight": 0.6,
                            "evidence": (
                                f"{p}:{ref.lineno} passed as a callable reference"
                            ),
                        }
                    )
                    have.add((src, tid, "calls"))
                    added += 1
    GP.write_text(json.dumps(g, indent=2, ensure_ascii=False))
    print("calls edges added:", added)


main()
