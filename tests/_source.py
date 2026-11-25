"""ProgramRunner's source for tests that check how it's wired.

ProgramRunner is defined in queues/run_program.py, and its task methods live in mixins
under queues/runner/. Read it through these helpers instead of opening run_program.py,
so a test keeps working when a method moves between those files.
"""
import ast
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER_FILES = [os.path.join(ROOT, 'queues', 'run_program.py')] + sorted(
    f for f in glob.glob(os.path.join(ROOT, 'queues', 'runner', '*.py')) if not f.endswith('__init__.py'))


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def runner_source() -> str:
    """run_program.py and every queues/runner/ module, as one text."""
    return '\n\n'.join(_read(f) for f in RUNNER_FILES)


def runner_tree() -> ast.Module:
    """All of those files parsed into one module."""
    body = []
    for f in RUNNER_FILES:
        body.extend(ast.parse(_read(f)).body)
    return ast.Module(body=body, type_ignores=[])


def runner_method(name: str) -> str:
    """The source of one ProgramRunner method, wherever it's defined."""
    for f in RUNNER_FILES:
        src = _read(f)
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return ast.get_source_segment(src, node)
    raise KeyError(f"ProgramRunner has no method {name!r}")


def project_source(relpath: str) -> str:
    """Source of a project module by its old path, e.g. 'routes/debug_routes.py'.

    Large modules were split into packages of the same name; for those, every
    file in the package is returned as one text, so source checks keep working
    whichever file a function now lives in.
    """
    path = os.path.join(ROOT, relpath)
    if os.path.isfile(path):
        return _read(path)
    pkg = path[:-3] if path.endswith('.py') else path
    if os.path.isdir(pkg):
        return '\n\n'.join(_read(f) for f in sorted(glob.glob(os.path.join(pkg, '*.py'))))
    raise FileNotFoundError(relpath)
