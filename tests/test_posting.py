"""What reaches the MR, and what reaches it where.

Two things are covered here that nothing covered before. The writing bar promises that **nothing
is dropped for length** — an over-long finding leaves the diff and lands in the summary note
instead — and that promise lives entirely in a pairing between a list comprehension and a string
builder, either of which could silently delete the finding from the MR. And `review-all` hands
`_review_single_mr` its arguments as a dict, so a parameter renamed on one side of that dict fails
at call time, per MR, and is logged as an ordinary review error.
"""

import ast
import pathlib
import types

import pytest

from mr_reviewer import main as M
from mr_reviewer.reviewer import ReviewComment

ROOT = pathlib.Path(__file__).resolve().parent.parent


def comment(**over) -> ReviewComment:
    base = {
        "file": "Svc.java",
        "line": 4,
        "severity": "WARNING",
        "title": "Accumulator loop hides a sum",
        "body": "A stream states the sum.",
        "stage": "SENIOR",
        "kind": "craft",
    }
    return ReviewComment(**{**base, **over})


class FakeGitLab:
    """Records what would have been sent, so the split is observable."""

    def __init__(self) -> None:
        self.discussions: list[str] = []
        self.notes: list[str] = []

    def post_discussion(self, project, iid, body, position=None):
        self.discussions.append(body)

    def post_note(self, project, iid, body):
        self.notes.append(body)


@pytest.fixture
def mr():
    change = types.SimpleNamespace(
        new_path="Svc.java",
        old_path="Svc.java",
        deleted_file=False,
        diff="@@ -1,3 +1,5 @@\n+        int sum = 0;\n+        return sum;\n",
    )
    return types.SimpleNamespace(
        iid=7,
        title="Add totals",
        changes=[change],
        diff_refs={"base_sha": "a", "start_sha": "a", "head_sha": "b"},
    )


@pytest.fixture
def reviewer():
    return types.SimpleNamespace(generate_summary=lambda comments, title: "## summary")


def test_a_report_only_finding_leaves_the_diff_and_lands_in_the_summary(mr, reviewer):
    """The writing bar's whole promise: quieter placement, never deletion."""
    gitlab = FakeGitLab()
    inline = comment(title="Posted on the line")
    long_one = comment(line=5, title="Too long for a line", inline=False)
    M._post_review_comments(gitlab, "g/r", mr, [inline, long_one], reviewer)

    assert len(gitlab.discussions) == 1
    assert "Posted on the line" in gitlab.discussions[0]
    assert "Too long for a line" not in gitlab.discussions[0]
    summary = "\n".join(gitlab.notes)
    assert "Too long for a line" in summary, "de-listed is not dropped"
    assert "Svc.java:5" in summary


def test_the_summary_says_nothing_extra_when_every_finding_fits(mr, reviewer):
    gitlab = FakeGitLab()
    M._post_review_comments(gitlab, "g/r", mr, [comment()], reviewer)
    assert gitlab.notes == ["## summary"]


def test_the_report_only_heading_takes_its_threshold_from_the_reducer():
    """A copied literal here would tell the author a number the reducer had moved on from."""
    from mr_reviewer.reduce import report_only_words

    section = M._report_only_section([comment(inline=False)])
    assert f"over {report_only_words()} words" in section


def test_an_empty_report_only_list_renders_nothing():
    assert M._report_only_section([]) == ""


def _bundle_keys(node: ast.AST) -> set[str] | None:
    """The argument names in a kwargs bundle, whichever way it is spelled.

    `dict(a=1)` and `{"a": 1}` build the same object, so a test that reads the source for the
    names being passed must accept both — matching only on `dict(...)` turned a mechanical
    rewrite into a failure here while the code under test was unchanged.
    """
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "dict":
        return {kw.arg for kw in node.keywords if kw.arg}
    if isinstance(node, ast.Dict):
        return {
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    return None


def _dict_keys_at(func_name: str, var_name: str) -> set[str]:
    """The keys of the kwargs bundle assigned to `var_name` inside `func_name`, read from main.py."""
    tree = ast.parse((ROOT / "mr_reviewer" / "main.py").read_text())
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func_name)
    for node in ast.walk(func):
        # `x = {...}` is an Assign and `x: dict[str, Any] = {...}` is an AnnAssign, and the
        # bundle is annotated precisely because a splat of it cannot be type-checked — so the
        # annotated form is the one this test most needs to find.
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(getattr(t, "id", "") == var_name for t in targets):
            keys = _bundle_keys(value)
            if keys is not None:
                return keys
    raise AssertionError(f"no `{var_name} = ...` kwargs bundle found in {func_name}")


def test_review_all_passes_only_arguments_the_worker_accepts():
    """`review_all_pending` builds one kwargs dict and submits it per MR, so a name that matches
    no parameter is a TypeError at call time — once per MR, logged as an ordinary review error.

    That is exactly what happened: `effort` was added to the dict and not to the signature, and
    every MR in a batch run failed with the reason buried in a per-MR error line.
    """
    import inspect

    accepted = set(inspect.signature(M._review_single_mr).parameters)
    passed = _dict_keys_at("review_all_pending", "review_kwargs")
    assert passed, "the kwargs dict moved — this test needs updating with it"
    assert not passed - accepted, f"passed but not accepted: {sorted(passed - accepted)}"
