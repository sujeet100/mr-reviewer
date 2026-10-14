"""Reasoning effort reaches `claude -p`, per pass, and an override beats the per-pass level.

Effort multiplies output tokens, so getting it wrong is silently expensive rather than broken:
the review still runs and still produces findings, just at the wrong cost or the wrong depth.
That is exactly the kind of regression no other test would catch.
"""

import ast
import pathlib
import types

import pytest

from mr_reviewer import reviewer as R


@pytest.fixture
def captured(monkeypatch) -> list[list[str]]:
    """Capture the argv of every `claude` invocation instead of running it."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(R.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(R.subprocess, "run", fake_run)
    return calls


def effort_of(cmd: list[str]) -> str | None:
    return cmd[cmd.index("--effort") + 1] if "--effort" in cmd else None


def test_each_pass_gets_its_own_effort(captured):
    local = R.LocalReviewer(repo_dir=".", model="opus")
    for stage in ("ARCHITECT", "SENIOR", "BLIND DESIGN/design"):
        local._send_prompt("x", effort=R.EFFORT_BY_STAGE[stage])
    assert [effort_of(c) for c in captured] == ["high", "medium", "medium"]


def _named_arguments(node: ast.AST) -> list[tuple[str | None, ast.AST]]:
    """The (name, value) pairs of a call's keywords, or of a dict literal's string keys.

    Both spellings have to count. The orchestrator builds each pass's arguments as a bundle it
    splats later, so `label="ARCHITECT"` in a call and `{"label": "ARCHITECT"}` in a bundle are
    the same call site — and reading only the first made every batched pass look uncalled.
    """
    if isinstance(node, ast.Call):
        return [(kw.arg, kw.value) for kw in node.keywords]
    if isinstance(node, ast.Dict):
        return [
            (
                key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None,
                value,
            )
            for key, value in zip(node.keys, node.values, strict=True)
        ]
    return []


def _literal_labels(source: str) -> set[str]:
    """Pass names spelled as string literals at a `label` or `focus_stage` argument."""
    return {
        value.value
        for node in ast.walk(ast.parse(source))
        for name, value in _named_arguments(node)
        if name in ("label", "focus_stage")
        and isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and value.value
    }


def labels_named_as_literals() -> set[str]:
    """Every pass name the package spells out at a call site, read out of the source.

    The lookup is `EFFORT_BY_STAGE.get(label)`, so a label the map has never heard of does not
    fail — it returns None, `_send_prompt` omits `--effort`, and the pass quietly runs at whatever
    the session is set to. That is invisible in the output, which is why the call sites are read
    mechanically rather than listed here by hand.

    **Literals only**, which is what the name says: `_send_prompt` and `review_with_prompt`
    forward `label` and `focus_stage` as variables, and those forwards are invisible to this scan.
    So a missing level is always a real gap, while a "stale" level may just be one whose only call
    site passes it through a variable — move a live label into a constant and the staleness check
    below will say it is dead.
    """
    package = pathlib.Path(__file__).resolve().parent.parent / "mr_reviewer"
    return set().union(*(_literal_labels(path.read_text()) for path in package.glob("*.py")))


def test_every_pass_named_at_a_call_site_has_a_level():
    """The regression this exists for: `run_maker` was renamed and its two levels stopped applying."""
    missing = sorted(labels_named_as_literals() - set(R.EFFORT_BY_STAGE))
    assert not missing, f"passes with no mapped effort: {missing}"


def test_no_mapped_level_is_for_a_pass_that_no_longer_runs():
    """A stale key is dead weight that reads as calibration — it made `MAKER/triage` look alive."""
    stale = sorted(set(R.EFFORT_BY_STAGE) - labels_named_as_literals())
    assert not stale, f"mapped efforts for passes nothing calls: {stale}"


def test_no_effort_means_inherit_the_session(captured):
    """Absent a per-pass level and an override, the flag must not be passed at all."""
    R.LocalReviewer(repo_dir=".", model="opus")._send_prompt("x", effort=None)
    assert effort_of(captured[0]) is None


def test_an_explicit_override_beats_the_per_pass_level(captured):
    """`--effort max` has to win, or the flag would silently do nothing on most passes."""
    R.LocalReviewer(repo_dir=".", model="opus", effort="max")._send_prompt("x", effort="low")
    assert effort_of(captured[0]) == "max"


def test_the_cli_reviewer_also_passes_effort(captured):
    R.CLIReviewer(model="sonnet", effort="high")._send_prompt("x")
    assert effort_of(captured[0]) == "high"


def test_a_bad_level_fails_loudly_rather_than_being_dropped(captured):
    """`claude` rejects an unknown level, so catching it here names the real problem."""
    with pytest.raises(ValueError, match="effort must be one of"):
        R.Reviewer(model="opus", use_cli=True, effort="ludicrous")


def test_every_mapped_stage_is_a_level_claude_accepts():
    assert set(R.EFFORT_BY_STAGE.values()) <= set(R.VALID_EFFORTS)


def test_the_craft_pass_is_not_the_most_expensive_level():
    """SENIOR runs many times per MR, so its level is the biggest effort cost in the run.

    Pinned deliberately: raising it is a real decision to make against the benchmark, not
    something to drift into.
    """
    assert R.EFFORT_BY_STAGE["SENIOR"] == "medium"


def test_the_default_model_is_current():
    """Three copies of a stale id used to sit in main.py's signatures; now there is one."""
    from mr_reviewer.main import DEFAULT_MODEL

    assert DEFAULT_MODEL == R.DEFAULT_MODEL == "claude-opus-5"
