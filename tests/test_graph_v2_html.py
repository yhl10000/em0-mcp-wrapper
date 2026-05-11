"""Tests for the inline Graph Explorer v2 HTML."""

import ast
import pathlib


def _graph_v2_html() -> str:
    module = ast.parse(
        (pathlib.Path(__file__).resolve().parents[1] / "server" / "main.py").read_text()
    )
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == "graph_visualizer_v2":
            for child in node.body:
                if isinstance(child, ast.Return) and isinstance(child.value, ast.Constant):
                    return child.value.value
    raise AssertionError("graph_visualizer_v2 HTML return not found")


def test_graph_v2_script_defines_load_slice_without_broken_regex():
    html = _graph_v2_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]

    assert "async function loadSlice()" in script
    assert "function jsString(s)" in script
    assert "replace(/\\/g" not in script
