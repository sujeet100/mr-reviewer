"""The reducer never judges, and what it does mechanically it must do exactly.

`merge_findings.py --self-test` holds the inline fixtures; these tests cover the same pipeline
from pytest so a regression fails the suite, and add the cases that need a real file layout:
the label-to-stage fallback, the diff-evidenced quote, and the rendered list.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "mr-review"
    / "scripts"
    / "merge_findings.py"
)


@pytest.fixture(scope="module")
def mf():
    spec = importlib.util.spec_from_file_location("merge_findings", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["merge_findings"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "Svc.java").write_text(
        "public class Svc {\n"
        "    public int total(List<Item> items) {\n"
        "        int sum = 0;\n"
        "        for (Item i : items) sum += i.price();\n"
        "        return sum;\n"
        "    }\n"
        "}\n"
    )
    return tmp_path


def reduce(mf, repo, sources: dict, **kw) -> dict:
    """`reduce_findings` over already-built findings: the tests vary findings, not JSON."""
    merged: dict = mf.reduce_findings(
        {label: json.dumps(fs) for label, fs in sources.items()}, repo, **kw
    )
    return merged


def a_design_root(**over):
    """A MAKER design finding at the top of `Svc.java` — the root the symptoms belong under."""
    return finding(
        **{
            "line": 1,
            "stage": "MAKER",
            "kind": "design",
            "severity": "WARNING",
            "title": "Service reconstructs the price it was given",
            **over,
        }
    )


def two_craft_symptoms(**over):
    """Two craft findings inside that file: the recognisable surface of one decision."""
    return [
        finding(line=3, title="sum says nothing", **over),
        finding(line=4, title="loop hides the total", **over),
    ]


def finding(**over):
    base = {
        "stage": "FUNCTION",
        "kind": "craft",
        "file": "Svc.java",
        "line": 4,
        "severity": "WARNING",
        "title": "Accumulator loop hides a sum",
        "body": "```java\nfor (Item i : items) sum += i.price();\n```\nA stream states the sum.",
    }
    base.update(over)
    return base


def test_self_test_passes(mf, capsys):
    assert mf._self_test() == 0


def test_a_quoted_finding_survives(mf, repo):
    result = mf.reduce_findings({"craft-1": json.dumps([finding()])}, repo)
    assert result["counts"]["kept"] == 1
    assert result["kept"][0]["source"] == "craft-1"


def test_quote_that_is_not_in_the_code_is_dropped_with_a_reason(mf, repo):
    invented = finding(body="```java\nreturn cache.get(key);\n```\nUnbounded cache.")
    result = mf.reduce_findings({"craft-1": json.dumps([invented])}, repo)
    assert result["counts"]["kept"] == 0
    assert result["dropped"][0]["reason"].startswith("quote not found")


def test_a_deleted_line_is_evidenced_by_the_diff(mf, repo):
    deleted = finding(body="You removed `if (items == null) return 0;` — callers pass null.")
    dropped = mf.reduce_findings({"a": json.dumps([deleted])}, repo)
    assert dropped["counts"]["kept"] == 0
    kept = mf.reduce_findings(
        {"a": json.dumps([deleted])}, repo, diff_text="-        if (items == null) return 0;\n"
    )
    assert kept["counts"]["kept"] == 1


def test_stage_falls_back_to_the_file_label(mf, repo):
    unlabelled = finding()
    del unlabelled["stage"]
    result = mf.reduce_findings(
        {
            "architect": json.dumps([dict(unlabelled, kind="bug")]),
            "craft-2": json.dumps([finding(line=3, body="```java\nint sum = 0;\n```\nName it.")]),
        },
        repo,
    )
    assert sorted(f["stage"] for f in result["kept"]) == ["ARCHITECT", "FUNCTION"]


def test_pattern_at_many_lines_becomes_one_finding_with_locations(mf, repo):
    repeated = [finding(line=n, body=f"```java\nsum\n```\nline {n}") for n in (3, 4, 5)]
    result = mf.reduce_findings({"a": json.dumps(repeated)}, repo)
    assert result["counts"]["kept"] == 1
    assert result["kept"][0]["locations"] == ["Svc.java:3", "Svc.java:4", "Svc.java:5"]
    assert result["counts"]["dropped"] == 0, "collapsing is grouping, not dropping"


def test_one_locus_keeps_the_higher_altitude_in_every_posture(mf, repo):
    """Three passes describing one coupling is noise at any volume, so the collapse is not
    posture-dependent. It used to be calm-only, which is how a single finding reached the author
    three times — once each from the function, architect and integration passes."""
    sources = {
        "craft-1": json.dumps([finding()]),
        "architect": json.dumps(
            [finding(stage="ARCHITECT", kind="bug", title="Service owns pricing arithmetic")]
        ),
    }
    for posture in mf.POSTURES:
        kept = mf.reduce_findings(sources, repo, posture=posture)["kept"]
        assert [f["stage"] for f in kept] == ["ARCHITECT"], posture


def test_two_findings_from_one_pass_on_one_line_both_survive(mf, repo):
    """A pass reporting twice on a line has found two things, not one thing twice — a name that
    hides its direction and a missing check are both true of the same signature. Keying the
    collapse on the line alone dropped the second."""
    two = [
        finding(line=4, title="Accumulator loop hides a sum"),
        finding(line=4, kind="bug", severity="CRITICAL", title="Overflows past 2^31"),
    ]
    kept = mf.reduce_findings({"craft-1": json.dumps(two)}, repo)["kept"]
    assert len(kept) == 2


def test_the_locus_winner_does_not_depend_on_pass_order(mf, repo):
    """Two craft findings and one architect finding on a line must reduce the same way whichever
    agent finished first, so the winner is decided per stage rather than per arriving finding.

    The severities matter here. Craft wins on severity, so per-stage collapse keeps *both* craft
    findings while the per-arriving-finding version this replaced keeps one — which is what makes
    the test able to fail. With the architect finding ranked highest instead, only one finding
    could survive either way and the regression would pass unnoticed.
    """
    craft = [
        finding(line=4, severity="CRITICAL", title="Accumulator loop hides a sum"),
        finding(line=4, severity="CRITICAL", title="sum says nothing"),
    ]
    arch = [
        finding(
            line=4,
            stage="ARCHITECT",
            kind="craft",
            severity="WARNING",
            title="Service owns pricing arithmetic",
        )
    ]
    a = mf.reduce_findings({"craft-1": json.dumps(craft), "architect": json.dumps(arch)}, repo)
    b = mf.reduce_findings({"architect": json.dumps(arch), "craft-1": json.dumps(craft)}, repo)
    assert [f["stage"] for f in a["kept"]] == [f["stage"] for f in b["kept"]] == ["FUNCTION"] * 2


def test_posture_moves_the_craft_ceiling_but_not_the_evidence_bar(mf, repo):
    many = [finding(line=3 + n, severity="SUGGESTION", title=f"craft note {n}") for n in range(5)]
    counts = {
        p: mf.reduce_findings({"a": json.dumps(many)}, repo, posture=p)["counts"]["kept"]
        for p in mf.POSTURES
    }
    assert counts["high"] < counts["brutal"] == 5


def test_high_is_the_default_posture(mf, repo):
    """The skill reviews a local branch before the MR exists, so the pairing threshold is right
    for it and `mr` has to be asked for. The CLI asks for it, because a shared thread has an
    etiquette a private report does not."""
    assert mf.POSTURES == ("mr", "high", "brutal")
    assert mf.reduce_findings({"a": json.dumps([finding()])}, repo)["posture"] == "high"


def test_the_mr_tier_is_quieter_than_the_pairing_tier_at_every_size(mf):
    """One table, both consumers: the CLI posts `mr`, its --dry-run reports `high`. If these ever
    crossed, a posted review would be noisier than the report nobody else reads."""
    for units in (1, 5, 25, 400):
        assert mf.global_cap("mr", units) < mf.global_cap("high", units), units
    assert [mf.global_cap("mr", n) for n in (5, 25, 50)] == [6, 10, 13]
    caps = mf.POSTURE_CAPS
    assert caps["mr"]["craft_per_file"] < caps["high"]["craft_per_file"]
    assert all(caps["mr"]["stage"][s] < caps["high"]["stage"][s] for s in caps["high"]["stage"])


def test_a_design_finding_survives_the_global_ceiling(mf, repo):
    """It names a cost rather than a failure, so it is SUGGESTION-tier by nature and can never win
    a severity-ordered trim — yet it is the one finding that reshapes the change."""
    noise = [finding(line=3 + n, severity="SUGGESTION", title=f"craft note {n}") for n in range(12)]
    root = finding(
        line=2,
        severity="SUGGESTION",
        stage="MAKER",
        kind="design",
        title="Pricing belongs in the policy, not the service",
    )
    kept = mf.reduce_findings(
        {"craft-1": json.dumps(noise), "maker": json.dumps([root])}, repo, posture="mr"
    )["kept"]
    assert any(f["kind"] == "design" for f in kept)


def test_craft_symptoms_fold_into_the_design_finding_that_explains_them(mf, repo):
    """Two or more craft symptoms in a file with a design root are the recognisable surface of one
    decision. Separately they read as nitpicks; listed under the root they justify the rewrite."""
    out = reduce(mf, repo, {"maker": [a_design_root()], "craft-1": two_craft_symptoms()})
    assert out["counts"]["absorbed"] == 2
    kept = [f for f in out["kept"] if f["kind"] == "design"]
    assert len(kept) == 1 and "symptoms of the same decision" in kept[0]["body"]
    assert kept[0]["symptoms"] == ["Svc.java:3", "Svc.java:4"]


def test_a_bug_in_the_root_file_is_never_absorbed(mf, repo):
    """A data-loss bug must not be buried inside a design comment the author may disagree with.

    It gets a pointer instead — on another MR the "distance unit modelled three times" finding and the
    "silently reads unknown units as metres" bug were one decision and its consequence, and both
    needed to post without either becoming the other's footnote."""
    bugs = two_craft_symptoms(kind="bug", severity="CRITICAL")
    out = reduce(mf, repo, {"maker": [a_design_root()], "craft-1": bugs})
    assert out["counts"]["absorbed"] == 0
    assert len(out["kept"]) == 3
    kept_bugs = [f for f in out["kept"] if f["kind"] == "bug"]
    assert all(f["root"] == "Svc.java:1" for f in kept_bugs)
    assert all("Root: see `Svc.java:1`" in f["body"] for f in kept_bugs)


def test_architect_absorbs_craft_symptoms_too(mf, repo):
    """Before this, only a design finding could absorb, so on one real MR one coupling reached the
    author from three altitudes: design at :10, architect at :10, craft at :12."""
    root = a_design_root(
        stage="ARCHITECT", kind="craft", title="Response re-declares domain values"
    )
    out = reduce(mf, repo, {"architect": [root], "craft-1": two_craft_symptoms()})
    assert out["counts"]["absorbed"] == 2
    assert [f["stage"] for f in out["kept"]] == ["ARCHITECT"]


def test_a_design_root_absorbs_the_architect_finding_restating_it(mf, repo):
    """Altitude decides, so the framing that names the decision wins over the one describing it."""
    arch = two_craft_symptoms(stage="ARCHITECT")
    out = reduce(mf, repo, {"maker": [a_design_root()], "architect": arch})
    assert out["counts"]["absorbed"] == 2
    assert [f["stage"] for f in out["kept"]] == ["MAKER"]


def test_a_root_only_absorbs_inside_its_own_unit_when_the_plan_says_so(mf, repo):
    """A design root in one method has no business absorbing a naming problem in another."""
    plan = {
        "units": [
            {"file": "Svc.java", "start_line": 2, "end_line": 3, "symbol": "Svc.total"},
            {"file": "Svc.java", "start_line": 4, "end_line": 6, "symbol": "Svc.other"},
        ]
    }
    elsewhere = [
        finding(line=4, title="sum says nothing"),
        finding(line=5, title="loop hides it", body="```java\nreturn sum;\n```\nProse."),
    ]
    out = reduce(mf, repo, {"maker": [a_design_root(line=2)], "craft-1": elsewhere}, plan=plan)
    assert out["counts"]["absorbed"] == 0, "different unit, so not symptoms of this decision"
    assert len(out["kept"]) == 3


def test_a_root_outside_every_unit_still_owns_its_file(mf, repo):
    """A finding on a class declaration sits in no function unit, and legitimately covers them."""
    plan = {"units": [{"file": "Svc.java", "start_line": 2, "end_line": 6, "symbol": "Svc.total"}]}
    out = reduce(mf, repo, {"maker": [a_design_root()], "craft-1": two_craft_symptoms()}, plan=plan)
    assert out["counts"]["absorbed"] == 2


def test_preserve_all_keeps_the_symptoms_and_lifts_the_caps(mf, repo):
    """Learning mode's reason and brutal mode's reason are different; what they want is identical."""
    out = reduce(
        mf, repo, {"maker": [a_design_root()], "craft-1": two_craft_symptoms()}, preserve_all=True
    )
    assert out["counts"]["absorbed"] == 0
    assert len(out["kept"]) == 3


def test_without_a_checkout_the_quote_is_checked_against_the_diff(mf):
    """The CLI's deprecated diff mode has no clone. Dropping every finding for a file that is
    merely not on disk would silence the pass rather than filter it."""
    diff = "+        for (Item i : items) sum += i.price();\n"
    out = mf.reduce_findings(
        {"a": json.dumps([finding(file="NotOnDisk.java", line=12)])}, None, diff_text=diff
    )
    assert out["counts"]["kept"] == 1
    invented = mf.reduce_findings(
        {
            "a": json.dumps(
                [finding(file="NotOnDisk.java", line=12, body="```java\nnever written\n```")]
            )
        },
        None,
        diff_text=diff,
    )
    assert invented["counts"]["kept"] == 0


def test_global_ceiling_scales_with_units_sublinearly_and_is_bounded(mf):
    assert [mf.global_cap("high", n) for n in (1, 9, 25, 400)] == [14, 22, 30, 40]
    assert mf.global_cap("brutal", 1) > 1000, "brutal is uncapped"


def test_an_overlong_body_is_counted_but_never_dropped(mf, repo):
    """Length is measured, not enforced. Losing a demonstrated bug because its author was wordy is
    a worse outcome than a wordy comment, but a prose rule nobody counts is only a preference —
    and this one was ignored at 330 words."""
    wordy = finding(body="```java\nsum\n```\n" + "proof " * 200)
    result = mf.reduce_findings({"a": json.dumps([wordy])}, repo)
    assert result["counts"]["kept"] == 1
    assert result["counts"]["overlong"] == 1
    assert result["kept"][0]["words"] == 200, "fenced code is evidence, not verbosity"
    assert f"over {mf.BODY_WORD_CEILING} words" in mf.render_markdown(result)

    brief = mf.reduce_findings({"a": json.dumps([finding()])}, repo)
    assert brief["counts"]["overlong"] == 0
    assert "over" not in mf.render_markdown(brief).splitlines()[0]


def test_unparseable_output_is_recorded_not_fatal(mf, repo):
    result = mf.reduce_findings(
        {
            "craft-1": "I could not find anything worth reporting.",
            "craft-2": json.dumps([finding()]),
        },
        repo,
    )
    assert result["sources"]["craft-1"]["unparseable"] is True
    assert result["counts"]["kept"] == 1


def test_markdown_names_the_enclosing_function_from_the_plan(mf, repo):
    result = mf.reduce_findings({"craft-1": json.dumps([finding()])}, repo)
    plan = {"units": [{"file": "Svc.java", "start_line": 2, "end_line": 6, "symbol": "Svc.total"}]}
    rendered = mf.render_markdown(result, plan)
    assert "Svc.java:4 · Svc.total" in rendered
    assert "**WARNING**" in rendered
