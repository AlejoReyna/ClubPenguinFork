"""Scripts that CI runs as one file's source plus an appended snippet (python -c) must define every
name they use. ruff only sees the source file, so it can drop an import the snippet relies on, as
happened with `json` in bot_accounts.py and the bots registration step."""
import ast
import builtins
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
BASE = 'scripts/bot_accounts.py'
USERS = ['scripts/bots_integration.py', 'scripts/live_bots_integration.py', 'scripts/test-work-provision.py']


def snippets(path):
    """String literals appended to the source of BASE in `path`."""
    text = (ROOT / path).read_text()
    tree = ast.parse(text)
    holders, found = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and BASE in (ast.get_source_segment(text, node.value) or ''):
            holders |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    for node in ast.walk(tree):
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
                and BASE in (ast.get_source_segment(text, node.left) or '')
                and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            found.append(node.right.value)
        elif (isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add)
                and isinstance(node.target, ast.Name) and node.target.id in holders
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            found.append(node.value.value)
    return found


def undefined_names(source):
    """Names read anywhere but never bound anywhere (scopes flattened: catches missing imports)."""
    tree = ast.parse(source)
    bound = set(dir(builtins)) | {'__file__', '__name__', '__doc__', '__spec__', '__loader__', '__package__'}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {(alias.asname or alias.name).split('.')[0] for alias in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    return used - bound


class AppendedScriptTests(unittest.TestCase):
    def test_every_registration_snippet_is_found(self):
        for path in USERS:
            self.assertTrue(snippets(path), f'{path}: no snippet appended to {BASE}')

    def test_appended_scripts_define_every_name_they_use(self):
        base = (ROOT / BASE).read_text()
        for path in USERS:
            for snippet in snippets(path):
                with self.subTest(path=path):
                    self.assertEqual(undefined_names(base + snippet), set())

    def test_detects_a_missing_import(self):
        self.assertEqual(undefined_names('import sys\npayload = json.load(sys.stdin)\n'), {'json'})


if __name__ == '__main__':
    unittest.main()
