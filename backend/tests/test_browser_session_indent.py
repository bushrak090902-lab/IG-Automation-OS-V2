"""Tests for the indentation-only fix on browser_session.py.

These tests validate that:
 1. The module compiles (no IndentationError / SyntaxError).
 2. `ast.parse` shows the expected structure: connect_account & verify_account
    are module-level async functions, each contains a nested `async def _worker`
    with `async with lock:` inside `_worker`.
 3. All required public names exist in the module.
 4. Important logic substrings are still present in the source.
 5. publisher.py still compiles and its Phase 2 helpers are intact.
"""

import ast
import py_compile
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path("/app")
BROWSER_SESSION = REPO_ROOT / "backend/app/automation/browser_session.py"
PUBLISHER = REPO_ROOT / "backend/app/automation/publisher.py"
SERVER = REPO_ROOT / "backend/server.py"


# --- 1. Compilation ---------------------------------------------------------

def test_browser_session_py_compile():
    """`python -m py_compile` must succeed with no IndentationError."""
    py_compile.compile(str(BROWSER_SESSION), doraise=True)


def test_publisher_py_compile():
    py_compile.compile(str(PUBLISHER), doraise=True)


def test_server_py_compile():
    py_compile.compile(str(SERVER), doraise=True)


# --- 2. Import via subprocess (relative imports resolvable from /app) ------

def test_browser_session_import_no_syntax_error():
    """Importing should not fail with IndentationError/SyntaxError.

    Other runtime failures (missing playwright, missing DB) are acceptable —
    we only care about syntax-level correctness of this file.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import backend.app.automation.browser_session"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    combined = (result.stderr or "") + (result.stdout or "")
    assert "IndentationError" not in combined, f"IndentationError raised:\n{combined}"
    assert "SyntaxError" not in combined, f"SyntaxError raised:\n{combined}"


# --- 3. AST structural checks ----------------------------------------------

def _load_module_ast():
    source = BROWSER_SESSION.read_text(encoding="utf-8")
    return ast.parse(source), source


def _find_module_async_func(tree, name):
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    return None


def _find_nested_worker(func_node):
    """Find `async def _worker` directly nested inside func_node body."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_worker" and node is not func_node:
            return node
    return None


def test_connect_account_is_module_level_async():
    tree, _ = _load_module_ast()
    node = _find_module_async_func(tree, "connect_account")
    assert node is not None, "connect_account must be a module-level async def"


def test_verify_account_is_module_level_async():
    tree, _ = _load_module_ast()
    node = _find_module_async_func(tree, "verify_account")
    assert node is not None, "verify_account must be a module-level async def"


def test_connect_account_has_nested_worker_with_async_with_lock():
    tree, _ = _load_module_ast()
    parent = _find_module_async_func(tree, "connect_account")
    worker = _find_nested_worker(parent)
    assert worker is not None, "_worker must be nested inside connect_account"

    # Ensure `async with lock:` is inside _worker body
    found = False
    for sub in ast.walk(worker):
        if isinstance(sub, ast.AsyncWith):
            for item in sub.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Name) and ctx.id == "lock":
                    found = True
                    break
        if found:
            break
    assert found, "`async with lock:` must be inside connect_account._worker"


def test_verify_account_has_nested_worker_with_async_with_lock():
    tree, _ = _load_module_ast()
    parent = _find_module_async_func(tree, "verify_account")
    worker = _find_nested_worker(parent)
    assert worker is not None, "_worker must be nested inside verify_account"

    found = False
    for sub in ast.walk(worker):
        if isinstance(sub, ast.AsyncWith):
            for item in sub.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Name) and ctx.id == "lock":
                    found = True
                    break
        if found:
            break
    assert found, "`async with lock:` must be inside verify_account._worker"


def test_async_with_lock_not_at_module_or_parent_level():
    """Ensure `async with lock:` only appears inside `_worker` funcs, not at
    module scope or directly at the connect_account/verify_account body level."""
    tree, _ = _load_module_ast()

    # module-level statements should have no AsyncWith with `lock` name
    for node in tree.body:
        if isinstance(node, ast.AsyncWith):
            for item in node.items:
                assert not (isinstance(item.context_expr, ast.Name)
                            and item.context_expr.id == "lock"), \
                    "`async with lock:` must not be at module scope"

    # Direct children of connect_account/verify_account must not include
    # `async with lock:` at their top-level body (must be inside _worker)
    for fname in ("connect_account", "verify_account"):
        parent = _find_module_async_func(tree, fname)
        assert parent is not None
        for stmt in parent.body:
            if isinstance(stmt, ast.AsyncWith):
                for item in stmt.items:
                    assert not (isinstance(item.context_expr, ast.Name)
                                and item.context_expr.id == "lock"), \
                        f"`async with lock:` must be inside {fname}._worker, not directly in {fname}"


# --- 4. Public names --------------------------------------------------------

REQUIRED_NAMES = [
    "PLAYWRIGHT_AVAILABLE",
    "_has_display",
    "_browsers_dir",
    "_lock_for",
    "is_active",
    "_update_account",
    "_write_log",
    "_verify_persistent_profile",
    "connect_account",
    "verify_account",
    "get_environment",
    "_locks",
    "_tasks",
]


def test_module_exports_required_names():
    """Walk the entire module AST (including try/except blocks) to collect
    all assigned/defined names at module scope, then verify each required
    name exists."""
    tree, _ = _load_module_ast()
    defined = set()

    def collect(stmts):
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        defined.add(tgt.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined.add(node.target.id)
            elif isinstance(node, ast.Try):
                collect(node.body)
                for handler in node.handlers:
                    collect(handler.body)
                collect(node.orelse)
                collect(node.finalbody)
            elif isinstance(node, ast.If):
                collect(node.body)
                collect(node.orelse)

    collect(tree.body)
    missing = [n for n in REQUIRED_NAMES if n not in defined]
    assert not missing, f"Missing required public names: {missing}"


# --- 5. Required substrings survived the rewrite ----------------------------

REQUIRED_SUBSTRINGS = [
    "launch_persistent_context",
    "sessionid",
    "identity_mismatch",
    "accounts/edit",
    'session_status = "connecting"',
    'session_status = "verifying"',
    "_verify_persistent_profile(profile_path, account_name)",
    "asyncio.create_task(_worker())",
    "asyncio.wait_for(",
    'context.on("close", _on_close)',
]


def test_required_substrings_present():
    """Each critical logic substring must survive the rewrite (>=1 occurrence).
    Some naturally appear in both connect_account and verify_account, which is fine."""
    source = BROWSER_SESSION.read_text(encoding="utf-8")
    missing = [needle for needle in REQUIRED_SUBSTRINGS if source.count(needle) < 1]
    assert not missing, f"Missing critical substrings: {missing}"


# --- 6. publisher.py Phase 2 helpers intact --------------------------------

def test_publisher_phase2_helpers_present():
    source = PUBLISHER.read_text(encoding="utf-8")
    assert "_build_publish_launch_kwargs" in source
    assert "_publish_headless_mode" in source


def test_publisher_imports_browser_session():
    source = PUBLISHER.read_text(encoding="utf-8")
    assert "from . import browser_session" in source or \
           "from .browser_session" in source or \
           "import browser_session" in source, \
           "publisher.py should still reference browser_session"


def test_publisher_import_no_syntax_error():
    result = subprocess.run(
        [sys.executable, "-c", "import backend.app.automation.publisher"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    combined = (result.stderr or "") + (result.stdout or "")
    assert "IndentationError" not in combined, combined
    assert "SyntaxError" not in combined, combined
