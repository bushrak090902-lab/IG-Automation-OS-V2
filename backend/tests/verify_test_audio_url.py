"""Static verification of test_audio_url.py against the review spec."""
import ast, importlib.util, sys, re
from pathlib import Path

TARGET = Path("/app/backend/tests/test_audio_url.py")
src = TARGET.read_text()
tree = ast.parse(src)

results = {}

# 1. Required functions
funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
results["has_async_run"] = isinstance(funcs.get("run"), ast.AsyncFunctionDef)
results["has_async_dump_all"] = isinstance(funcs.get("dump_all"), ast.AsyncFunctionDef)
results["has_async_search_dom"] = isinstance(funcs.get("search_dom"), ast.AsyncFunctionDef)
results["has_sync_main"] = isinstance(funcs.get("main"), ast.FunctionDef)

# 2. KEYWORDS + INTERESTING_PAYLOAD_KEYS literals
required_keywords = {"Use Audio", "Create Reel", "Reel", "Create", "Audio", "Save Audio"}
required_payload = {"audio_id", "music_id", "music_info", "clips_metadata", "soundtrack"}

def collect_list_literal(name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    return set()

kw_set = collect_list_literal("KEYWORDS")
pl_set = collect_list_literal("INTERESTING_PAYLOAD_KEYS")
results["keywords_ok"] = required_keywords.issubset(kw_set)
results["keywords_missing"] = list(required_keywords - kw_set)
results["payload_keys_ok"] = required_payload.issubset(pl_set)
results["payload_missing"] = list(required_payload - pl_set)

# 3. Load module (module-only) & check artifact paths
spec = importlib.util.spec_from_file_location("t_audio", TARGET)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for attr in ("HTML_OUT", "JSON_OUT", "PNG_OUT"):
    p = Path(getattr(mod, attr)).resolve()
    results[f"{attr}_path"] = str(p)
    results[f"{attr}_under_backend_logs"] = str(p).startswith("/app/backend/logs/")

# 4. Recording gate ordering
lines = src.splitlines()
true_lines = [i+1 for i, ln in enumerate(lines) if 'recording["on"] = True' in ln]
false_lines = [i+1 for i, ln in enumerate(lines) if 'recording["on"] = False' in ln]
# find "use_audio_loc is not None" check
gate_lines = [i+1 for i, ln in enumerate(lines) if "use_audio_loc is not None" in ln]
results["recording_true_lines"] = true_lines
results["recording_false_lines"] = false_lines
results["gate_check_lines"] = gate_lines
# Ignore initial `recording = {"on": False}` init (that's dict literal, not assignment shown above).
# True must appear AFTER the gate check line.
results["recording_true_after_gate"] = bool(true_lines and gate_lines and min(true_lines) > min(gate_lines))
# A False must appear after the True (to close recording before context.close)
results["recording_false_after_true"] = bool(true_lines and false_lines and max(false_lines) > min(true_lines))

# 5. Conclusions keys
required_conclusions = {
    "use_audio_found_in_dom", "use_audio_locator_clickable", "click_navigated",
    "landed_in_composer", "audio_metadata_leaked_in_network", "path_viable_for_automation",
}
found = {k for k in required_conclusions if f'"{k}"' in src}
results["conclusions_keys_ok"] = required_conclusions.issubset(found)
results["conclusions_missing"] = list(required_conclusions - found)

# 6. Publish/upload safety - actual code calls (not comments/docstrings)
code_only = re.sub(r'""".*?"""', '', src, flags=re.S)
code_only = re.sub(r"'''.*?'''", '', code_only, flags=re.S)
code_only = re.sub(r'#.*', '', code_only)
danger = {
    "set_input_files(": "set_input_files(" in code_only,
    "/media/configure": "/media/configure" in code_only,
    "/media/upload": "/media/upload" in code_only,
    "_perform_publish": "_perform_publish" in code_only,
    "publish_job": "publish_job" in code_only,
    "share_click": bool(re.search(r"\.click\([^)]*Share", code_only)),
}
results["danger_calls"] = {k: v for k, v in danger.items() if v}
results["safe"] = not any(danger.values())

# 7. Import isolation on AST
forbidden_mods = ("publisher", "browser_session", "publisher_queue", "app.automation")
bad_imports = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for a in node.names:
            if any(f in a.name for f in forbidden_mods):
                bad_imports.append(a.name)
    elif isinstance(node, ast.ImportFrom):
        if node.module and any(f in node.module for f in forbidden_mods):
            bad_imports.append(node.module)
results["forbidden_imports"] = bad_imports
results["import_isolation_ok"] = not bad_imports

import json
print(json.dumps(results, indent=2))
overall_ok = all([
    results["has_async_run"], results["has_async_dump_all"],
    results["has_async_search_dom"], results["has_sync_main"],
    results["keywords_ok"], results["payload_keys_ok"],
    results["HTML_OUT_under_backend_logs"], results["JSON_OUT_under_backend_logs"], results["PNG_OUT_under_backend_logs"],
    results["recording_true_after_gate"], results["recording_false_after_true"],
    results["conclusions_keys_ok"], results["safe"], results["import_isolation_ok"],
])
print("OVERALL_OK:", overall_ok)
sys.exit(0 if overall_ok else 1)
