"""The page renderer joins findings to the diff so the reader does not have to.

`render_review.py --self-test` holds the inline fixtures; these run the same checks under pytest
and add the ones that need a real file layout or the CLI's cache shape.
"""

import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "mr-review"
    / "scripts"
    / "render_review.py"
)


@pytest.fixture(scope="module")
def rr():
    spec = importlib.util.spec_from_file_location("render_review", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_review"] = module
    spec.loader.exec_module(module)
    return module


def test_self_test_passes(rr):
    assert rr._self_test() == 0


def test_cache_and_reducer_shapes_both_load(rr, tmp_path):
    finding = {
        "stage": "FUNCTION",
        "kind": "craft",
        "file": "a.py",
        "line": 1,
        "severity": "WARNING",
        "title": "t",
        "body": "`x`",
    }
    cache = tmp_path / "cache.json"
    cache.write_text(
        json.dumps({"version": 1, "mr_title": "MR 42", "summary": "s", "comments": [finding]})
    )
    kept, dropped, meta = rr.load_findings(cache)
    assert kept == [finding] and dropped == [] and meta["title"] == "MR 42"

    merged = tmp_path / "merged.json"
    merged.write_text(
        json.dumps(
            {
                "kept": [finding],
                "dropped": [{"finding": finding, "reason": "r"}],
                "posture": "brutal",
            }
        )
    )
    kept, dropped, meta = rr.load_findings(merged)
    assert len(kept) == 1 and len(dropped) == 1 and meta["posture"] == "brutal"

    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps([finding]))
    assert rr.load_findings(bare)[0] == [finding]


def test_finding_ids_are_stable_across_renders(rr):
    f = {"stage": "FUNCTION", "file": "a.py", "line": 3, "title": "Long method — does five things"}
    assert rr.finding_id(f) == rr.finding_id(dict(f, body="different body", severity="CRITICAL"))
    assert rr.finding_id(f) != rr.finding_id(dict(f, line=4))


def test_page_escapes_agent_text(rr, tmp_path):
    hostile = {
        "stage": "FUNCTION",
        "kind": "craft",
        "file": "a.py",
        "line": 1,
        "severity": "WARNING",
        "title": "<img src=x onerror=alert(1)>",
        "body": "<script>alert(1)</script> and `<b>`",
    }
    page = rr.render_page([hostile], [], "", tmp_path, "t")
    assert "<img src=x" not in page
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_finding_on_a_file_absent_from_the_diff_still_renders(rr, tmp_path):
    (tmp_path / "b.py").write_text("x = 1\ny = 2\nz = 3\n")
    f = {
        "stage": "INTEGRATION",
        "kind": "bug",
        "file": "b.py",
        "line": 2,
        "severity": "CRITICAL",
        "title": "Caller still expects the old shape",
        "body": "`y = 2`",
    }
    page = rr.render_page([f], [], "", tmp_path, "t")
    assert "b.py" in page and "y = 2" in page
    assert "outside the diff — working-tree context" in page


def test_sarif_and_intellij_exports_are_well_formed(rr):
    kept = [
        {
            "stage": "FUNCTION",
            "kind": "craft",
            "file": "a.py",
            "line": 3,
            "severity": "CRITICAL",
            "title": "t & u",
            "body": "b < c",
        }
    ]
    sarif = rr.to_sarif(kept)
    json.dumps(sarif)
    assert sarif["runs"][0]["results"][0]["level"] == "error"
    root = ET.fromstring(rr.to_intellij_xml(kept))
    assert root.find("problem/problem_class").get("severity") == "ERROR"
    assert "t & u" in root.find("problem/problem_class").text
