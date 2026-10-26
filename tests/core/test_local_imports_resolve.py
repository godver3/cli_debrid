"""Every `from <project module> import name` must name something that module defines.

Most of these imports sit inside functions wrapped in try/except, so a rename
or deletion turns them into a silent no-op instead of an error: ReNZB's
re-scrape fallback, the AI remove-duplicates tool and upgrade scoring's season
counts all broke that way. pyflakes doesn't catch it (the import itself is
well-formed), so check the names statically here.
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKIP_DIRS = {'tests', 'phalanx_db_hyperswarm', 'node_modules', '__pycache__', '.git'}


def _py_files():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        for f in files:
            if f.endswith('.py'):
                yield os.path.relpath(os.path.join(root, f), ROOT)


FILES = set(_py_files())


def _module_file(dotted, importer, level):
    if level:
        base = os.path.dirname(importer)
        for _ in range(level - 1):
            base = os.path.dirname(base)
        parts = [base] + (dotted.split('.') if dotted else [])
    else:
        parts = dotted.split('.')
    path = os.path.normpath(os.path.join(*parts))
    for candidate in (path + '.py', os.path.join(path, '__init__.py')):
        if candidate in FILES:
            return candidate
    return None


_names_cache = {}


def _top_level_names(path):
    """Names a module defines at top level (including under if/try), and whether it star-imports."""
    if path in _names_cache:
        return _names_cache[path]
    with open(os.path.join(ROOT, path), encoding='utf-8') as f:
        tree = ast.parse(f.read())
    names, dynamic = set(), False

    def collect(body):
        nonlocal dynamic
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name == '*':
                        dynamic = True
                    names.add((alias.asname or alias.name).split('.')[0])
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for attr in ('body', 'orelse', 'finalbody'):
                    collect(getattr(node, attr, []) or [])
                for handler in getattr(node, 'handlers', []) or []:
                    collect(handler.body)
            elif isinstance(node, ast.Expr) and 'exec(' in ast.unparse(node):
                dynamic = True
    collect(tree.body)
    _names_cache[path] = (names, dynamic)
    return names, dynamic


def _declared_submodules(init_path):
    """The `submodules = [...]` list a package __init__ star-exports from, if it has one."""
    with open(os.path.join(ROOT, init_path), encoding='utf-8') as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'submodules'
                                                 for t in node.targets)
                and isinstance(node.value, ast.List)):
            return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    return None


def _exported_names(path):
    """Names importable from a module; a package that star-exports its submodules gets theirs too."""
    names, dynamic = _top_level_names(path)
    if not dynamic:
        return names
    if not path.endswith('__init__.py'):
        return None  # star import from elsewhere: can't tell statically
    pkg = os.path.dirname(path)
    merged = set(names)
    listed = _declared_submodules(path)  # database/__init__ star-exports only these
    for f in FILES:
        if os.path.dirname(f) == pkg and f != path:
            if listed is not None and os.path.basename(f)[:-3] not in listed:
                continue
            merged.add(os.path.basename(f)[:-3])
            sub_names, sub_dynamic = _top_level_names(f)
            if sub_dynamic:
                return None
            merged |= sub_names
    return merged


class LocalImportsResolveTest(unittest.TestCase):
    def test_from_imports_name_existing_definitions(self):
        missing = []
        for path in sorted(FILES):
            with open(os.path.join(ROOT, path), encoding='utf-8') as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                target = _module_file(node.module or '', path, node.level)
                if not target:
                    continue  # third-party / stdlib
                exported = _exported_names(target)
                if exported is None:
                    continue
                pkg_dir = os.path.dirname(target) if target.endswith('__init__.py') else None
                for alias in node.names:
                    if alias.name == '*' or alias.name in exported:
                        continue
                    if pkg_dir and (os.path.join(pkg_dir, alias.name + '.py') in FILES
                                    or os.path.join(pkg_dir, alias.name, '__init__.py') in FILES):
                        continue
                    missing.append(f'{path}:{node.lineno}: from {"." * node.level}{node.module or ""} '
                                   f'import {alias.name}')
        self.assertEqual(missing, [], 'imports of names that no longer exist:\n' + '\n'.join(missing))


if __name__ == '__main__':
    unittest.main()
