"""What posture must and must not change, on the consumer that posts.

These exist because the failure mode of a volume setting is silent: a run that trims more than it
should looks exactly like a run that trimmed correctly, and the only way to notice is to count.
The reviewer has already lost function-level review once to a change nobody measured.

This file replaced `test_brutal_mode.py`, which asserted the counts of a reduce chain the CLI no
longer has — a parse-time per-stage cap, a per-file cap and a global cap, none of them shared with
the skill. There is now one reducer and one table of numbers, so what is worth pinning is the
*relationship* between the tiers rather than any single count: the tier that posts must never be
noisier than the tier nobody else reads, and the drop rules must not move between them.
"""

import pytest

from mr_reviewer import prompts, reviewer
from mr_reviewer import reduce as R
from mr_reviewer.reviewer import ReviewComment


@pytest.fixture(autouse=True)
def _calm(monkeypatch):
    """Every test starts in normal mode; the brutal ones opt in explicitly."""
    monkeypatch.delenv("MR_REVIEWER_BRUTAL", raising=False)


@pytest.fixture
def brutal(monkeypatch):
    """Turn brutal mode on. It is read from the environment at call time, so no reload."""
    monkeypatch.setenv("MR_REVIEWER_BRUTAL", "1")


def findings(
    count, *, stage="SENIOR", kind="craft", file="a/B.java", severity="SUGGESTION", offset=0
):
    """`count` distinct findings — distinct loci, so dedup and locus-collapse stay out of it.

    Each body quotes a line that the `diff` fixture below contains, because the reducer drops an
    unquoted finding in every posture. That is the point: the evidence bar is not a volume setting.
    """
    return [
        ReviewComment(
            file=file,
            line=10 + offset + i,
            severity=severity,
            title=f"finding {offset + i}",
            body=f"```java\nint v{offset + i} = {offset + i};\n```\nProse.",
            stage=stage,
            kind=kind,
        )
        for i in range(count)
    ]


def diff(comments) -> str:
    """A diff containing every line those findings quote, so the quote check passes."""
    return "".join(f"+        int v{n} = {n};\n" for n in range(1200))


def reduce(comments, posture="mr", units=3, **kw):
    """Run the shared reduce step with no checkout, as the deprecated diff mode does."""
    return R.reduce_comments(
        comments, None, diff_text=diff(comments), posture=posture, units=units, **kw
    )


def mixed_corpus():
    """18 findings across three files: enough to trip every cap in the chain."""
    return (
        findings(9)
        + findings(6, file="a/C.java", offset=100)
        + findings(3, stage="MAKER", kind="design", file="a/D.java", offset=200)
    )


# --- which posture a run reports at ----------------------------------------------------


def test_posture_follows_who_reads_the_result():
    assert R.posture_for(dry_run=False) == "mr"
    assert R.posture_for(dry_run=True) == "high"


def test_an_explicit_brutal_wins_over_both():
    """It is a request for everything meaningful, so it cannot be a quieter tier's business."""
    assert R.posture_for(dry_run=False, brutal=True) == "brutal"
    assert R.posture_for(dry_run=True, brutal=True) == "brutal"


# --- the runaway guard, which replaced the parse-time per-stage cap --------------------


def test_a_normal_pass_is_not_trimmed_at_parse_time():
    """The old cap deleted five of nine findings here, before dedup could say which were dupes."""
    assert len(reviewer._guard_runaway(findings(9))) == 9


def test_a_runaway_pass_is_trimmed_loudly():
    kept = reviewer._guard_runaway(findings(40, severity="CRITICAL") + findings(10, offset=600))
    assert len(kept) == reviewer.RUNAWAY_GUARD
    assert all(c.severity == "CRITICAL" for c in kept), "the guard keeps the worst, not the first"


def test_the_guard_is_not_a_posture_setting(brutal):
    """It marks a malfunction — a pass that ignored its ranking instruction — not a volume choice."""
    assert len(reviewer._guard_runaway(findings(40))) == reviewer.RUNAWAY_GUARD


# --- volume by posture ----------------------------------------------------------------


def test_the_posted_tier_is_quieter_than_the_report_tier():
    corpus = mixed_corpus()
    counts = {p: len(reduce(corpus, posture=p)) for p in ("mr", "high", "brutal")}
    assert counts["mr"] < counts["high"] < counts["brutal"] == 18


def test_brutal_keeps_everything_that_cleared_the_bar(brutal):
    assert len(reduce(mixed_corpus(), posture="brutal")) == 18


def test_dedup_runs_in_every_posture():
    """Two copies of one finding are not two findings, at any volume."""
    one = findings(1)
    for posture in ("mr", "high", "brutal"):
        assert len(reduce(one + one, posture=posture)) == 1, posture


def test_the_evidence_bar_does_not_move_with_posture():
    """The obvious failure of a "more findings" tier is more *wrong* findings."""
    invented = findings(1)
    invented[0].body = "```java\nint neverWritten = 0;\n```\nProse."
    for posture in ("mr", "high", "brutal"):
        assert reduce(invented, posture=posture) == [], posture


def test_no_cap_drops_a_critical():
    """A cap bounds how much optional advice reaches a thread, never a demonstrated bug."""
    corpus = findings(12, severity="CRITICAL") + findings(30, severity="SUGGESTION", offset=500)
    kept = reduce(corpus, posture="mr")
    assert sum(1 for c in kept if c.severity == "CRITICAL") == 12


# --- symptom absorption ---------------------------------------------------------------


def test_symptoms_are_absorbed_into_the_design_root_normally():
    corpus = findings(4) + findings(1, stage="MAKER", kind="design", offset=900)
    kept = reduce(corpus)
    assert len(kept) == 1
    assert "symptoms of the same decision" in kept[0].body


def test_preserve_all_keeps_the_symptoms_standalone():
    """A lesson needs a pattern's second and third instance; a review does not."""
    corpus = findings(4) + findings(1, stage="MAKER", kind="design", offset=900)
    kept = reduce(corpus, posture="high", preserve_all=True)
    assert len(kept) == 5
    assert not any("symptoms of the same decision" in (c.body or "") for c in kept)


def test_brutal_implies_it():
    """Someone asking to see everything is asking for the symptoms, which are the surface they
    would learn to recognise. Lifting the ceilings while still folding them would be half a mode."""
    corpus = findings(4) + findings(1, stage="MAKER", kind="design", offset=900)
    kept = reduce(corpus, posture="brutal")
    assert len(kept) == 5
    assert not any("symptoms of the same decision" in (c.body or "") for c in kept)


# --- the writing bar ------------------------------------------------------------------


def wordy(words, **kw):
    c = findings(1, **kw)[0]
    c.body = "```java\nint v0 = 0;\n```\n" + "proof " * words
    return [c]


def test_an_over_long_finding_is_demoted_not_dropped():
    """Losing a demonstrated bug to wordiness is the worse outcome, so length never rejects."""
    kept = reduce(wordy(200, severity="CRITICAL", kind="bug"))
    assert len(kept) == 1
    assert kept[0].severity == "WARNING"
    assert kept[0].inline is True


def test_a_finding_within_the_ceiling_keeps_its_severity():
    assert reduce(wordy(80, severity="CRITICAL", kind="bug"))[0].severity == "CRITICAL"


def test_a_design_finding_gets_the_wider_ceiling():
    """Its alternative *is* the finding: "do this instead, and here is the cost" needs the room."""
    assert (
        reduce(wordy(170, stage="MAKER", kind="design", severity="WARNING"))[0].severity
        == "WARNING"
    )
    assert reduce(wordy(170, severity="WARNING"))[0].severity == "SUGGESTION"


def test_a_very_long_finding_leaves_the_thread_but_stays_in_the_report():
    kept = reduce(wordy(300, severity="WARNING"))
    assert len(kept) == 1
    assert kept[0].inline is False
    assert kept[0].severity == "WARNING", "de-listed instead of demoted, not both"


def test_the_bar_runs_after_the_caps_so_it_can_never_cause_a_drop():
    """A demotion before the caps could turn a wordy CRITICAL into a cappable WARNING.

    Both halves are asserted. The count alone would still pass with the bar removed entirely —
    nothing is dropped either way — so the severities are what say the bar ran at all.
    """
    corpus = [wordy(200, severity="CRITICAL", kind="bug", offset=i)[0] for i in range(20)]
    kept = reduce(corpus, posture="mr")
    assert len(kept) == 20
    assert {c.severity for c in kept} == {"WARNING"}, "kept, and demoted: the bar ran"


def test_the_bar_measures_the_body_it_is_looking_at():
    """Absorption appends the symptom list, so a root grows after `validate` counted its words.

    Measuring the stale count let a root that absorbed three symptoms post inline past the
    de-listing threshold while reporting a length it no longer had.
    """
    root = wordy(240, stage="MAKER", kind="design", severity="WARNING")[0]
    kept = reduce([root] + findings(2, offset=40), posture="high")
    design = [c for c in kept if c.kind == "design"]
    assert len(design) == 1
    assert design[0].inline is False, "the symptom list pushed it past the threshold"


def test_a_demoted_finding_carries_the_reason_across_the_boundary():
    """Without it the comment wears a quieter badge than its pass gave it and says nothing why."""
    kept = reduce(wordy(200, severity="CRITICAL", kind="bug"))
    assert kept[0].demoted_from == "CRITICAL"
    assert "ceiling of 150" in kept[0].writing


# --- the runaway guard's one exemption -------------------------------------------------------


def test_the_guard_never_trims_the_po_pass():
    """PO emits one comment per unmet criterion plus two roll-ups, so twenty is a correct pass.

    The deleted `STAGE_CAPS` said this with `"PO": None`. Losing it here cut exactly the two
    SUGGESTION-tier roll-ups that the reduce step then protects from the global ceiling by name.
    """
    po = findings(20, stage="PO", kind="bug", severity="SUGGESTION")
    assert len(reviewer._guard_runaway(po)) == 20


def test_the_guard_still_trims_the_pass_beside_it():
    """The exemption is for PO, not for whatever else happens to be in the same batch."""
    corpus = findings(20, stage="PO", severity="SUGGESTION") + findings(20, offset=500)
    kept = reviewer._guard_runaway(corpus)
    assert len(kept) == 20 + reviewer.RUNAWAY_GUARD


# --- the prompt side ------------------------------------------------------------------


def test_charter_resolves_to_the_plugin():
    assert prompts.charter_dir().name == "charter"
    assert (prompts.charter_dir() / "base.md").is_file()


def test_the_reducer_resolves_to_the_plugin_too():
    """Same fallback chain as the charter, for the same reason: one copy, two consumers."""
    assert R.scripts_dir().name == "scripts"
    assert R.reducer().POSTURES == ("mr", "high", "brutal")


def test_every_pass_is_asked_for_candidates_in_every_mode(brutal):
    """Detection does not vary by posture. A quieter reviewer loses the subtle findings
    irrecoverably; a quieter report loses nothing, because the reducer records what it removed."""
    body = prompts.get_base_prompt()
    assert "CANDIDATES" in body
    assert body.index("CANDIDATES") > body.index("STAGE BUDGETS"), "after the budgets it lifts"


def test_the_prompt_does_not_change_with_posture():
    assert prompts.get_base_prompt() == prompts.get_base_prompt()
    assert "BRUTAL MODE" not in prompts.get_base_prompt()
