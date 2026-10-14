#!/usr/bin/env python3
"""Reduce the review agents' findings deterministically, so the orchestrator never does it by eye.

The ``/review-changes`` skill collects one JSON array per agent. Before this script existed the
merge — drop malformed entries, drop anything that quotes no code, dedupe, cap — was six prose
steps the orchestrating model performed in its own context. That is the one step of the review
that should never be approximate: a duplicate it misses is posted twice, a quote it fails to
check is an invented finding, and nothing tested it. Everything here is mechanical and has a
fixture; nothing here decides whether a finding is *right*. That judgement is not a script's to
make, and a previous attempt to make a model do it wholesale ("re-evaluate strictly, be
ruthless") over-pruned the craft findings the review exists to produce.

What it does, in order:

1. Parse each agent's output leniently (an array, possibly wrapped in prose or a fence).
2. Drop malformed findings — no file, no integer line, no title, no body, a line past the end of
   the file, or a file that does not exist — and say why.
3. Drop findings that quote nothing, or whose quoted code appears neither in the cited file nor
   in the diff. This is the "quote or drop" rule the charter states, checked rather than trusted.
   It is the only drop rule that touches content, and it drops exactly the class of finding
   developers most often push back on: code that is not there.
4. Dedupe exact repeats on (file, line, normalised title), keeping the sharpest copy.
5. Collapse one pattern reported at many lines into one finding that lists every location.
   Nothing is removed by this step; it is what stops the same lookup being posted eleven times.
6. Collapse two passes reporting one locus to the higher-altitude framing. This runs in every
   posture: three agents describing the same coupling from three altitudes is noise no matter how
   loud the posture is, and the altitude that tells the author what to do about it wins.
7. Fold two or more craft symptoms into the design finding on the same file, which is the one
   that says what to do about them. Bugs are never folded in.
8. Apply the posture's caps — craft per file, per stage, and a global ceiling that scales with the
   number of review units and never drops a CRITICAL. ``brutal`` has no caps.
9. Apply the writing bar: demote an over-long body one rank, de-list a very long one from the
   inline thread. Nothing is dropped for length, and this runs after the caps so it can never be
   the reason a demonstrated bug disappears.
10. Rank by severity, then by pass altitude.

Both consumers call this. The skill pipes each agent's raw output in through ``reduce_findings``;
the CLI's passes are parsed before it sees them, so it calls ``reduce_candidates`` through
``mr_reviewer/reduce.py``. The CLI used to have its own reduce step, which had never gained the
quote check — so the consumer that actually posted to a shared thread was the one running the
weaker filter.

    merge_findings.py --repo . /tmp/review-findings-*.json > /tmp/review-merged.json
    merge_findings.py --repo . --markdown --plan /tmp/review-plan.json findings/*.json
    merge_findings.py --self-test
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

SEVERITIES = ("CRITICAL", "WARNING", "SUGGESTION")
_SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}
# Higher altitude first: when two passes describe one spot, the framing that tells the author
# what to do about the *design* wins over the local symptom. Same order as the CLI's reducer.
_STAGE_PRIORITY = {
    "PO": 0,
    "MAKER": 1,
    "ARCHITECT": 2,
    "INTEGRATION": 3,
    "FUNCTION": 4,
    "SENIOR": 4,
    "PROMPT": 5,
}

# Exempt from same-locus collapse: PO anchors both its roll-ups at line 1 of the primary file, and
# a design decision and a bug can both be true of one line — collapsing them would lose the design
# finding every time, since `_rank` puts severity first. Named because the rule is tested in two
# directions and a third exempt pass would otherwise have to be added in two places.
LOCUS_EXEMPT_STAGES = ("PO", "MAKER")
KINDS = ("craft", "bug", "design", "test_gap")

# Posture sets how much of what was found gets reported. It never changes the evidence bar: the
# drop rules above run identically in all three, so a finding suppressed here was true and merely
# not worth the reader's attention at this threshold. Detection and presentation are deliberately
# separate — the agents always review deeply and emit candidates, and the numbers live here rather
# than in the prompts, so raising the threshold cannot make an agent less observant.
#
#   mr     — is this important enough to interrupt the author's MR thread with? What the CLI
#            posts. A shared thread has an etiquette a private report does not: every comment
#            costs someone else's attention, and a thread where a third of the comments are
#            optional gets skimmed rather than read.
#   high   — would a strong senior raise this while pairing, before the MR? Everything worth
#            saying, bounded by what the developer can work through in one sitting. The skill's
#            default, and the CLI's under `--dry-run`, where nothing is posted and nobody else
#            reads it.
#   brutal — every finding that clears the evidence bar, for "tear this apart".
#
# One table, both consumers. The CLI used to keep its own numbers in three places — a parse-time
# cap per pass, a per-file cap and a global cap — none of which the skill shared, so "the same
# methodology" produced measurably different output depending on which one ran. Posture is the
# only thing that differs between them now, and it differs by *who reads the result* rather than
# by which program produced it.
#
# The global ceiling scales with review units rather than diff size, because a large mechanical
# diff carries no design freedom while a small one often carries all of it. Sublinear and bounded:
# twice the units is not twice the useful comments, and past a point a longer list is just ignored.
#
# Every number here is a hypothesis awaiting the seeded-canary benchmark, not a measurement. The
# `mr` tier's ceiling gives 6 comments at 5 units, 10 at 25, 13 at 50 and its maximum of 14 from
# 55 up — the shape the architecture assessment proposed, and the self-test pins all four sizes
# rather than the round numbers alone. Nothing has yet shown that 10 earns more acted-on feedback
# than 8 or 14 would.
POSTURES = ("mr", "high", "brutal")

POSTURE_CAPS: dict[str, dict | None] = {
    "mr": {
        "craft_per_file": 2,
        "stage": {"ARCHITECT": 6, "MAKER": 2, "INTEGRATION": 6, "PROMPT": 4},
        "global": (2.8, 1.45, 6, 14),
    },
    "high": {
        "craft_per_file": 4,
        "stage": {"ARCHITECT": 12, "MAKER": 4, "INTEGRATION": 10, "PROMPT": 6},
        "global": (10.0, 4.0, 14, 40),
    },
    "brutal": None,
}

# Design findings held back from the global ceiling. A root design decision names a cost rather
# than a failure, so it is SUGGESTION- or WARNING-tier by nature and can never win a
# severity-ordered trim against a pile of bugs — yet it is the one finding that reshapes the
# change rather than patching it. Protected by count, for the same reason craft is.
DESIGN_SLOTS = 2


def _caps(posture: str) -> dict:
    """The posture's cap table. Only the capped tiers have one, which is what `brutal` being
    ``None`` means — so this is for callers that have already excluded it, chiefly the self-test."""
    caps = POSTURE_CAPS[posture]
    if caps is None:
        raise KeyError(f"posture {posture!r} has no caps")
    return caps


def global_cap(posture: str, units: int) -> int:
    """The global ceiling for this posture, scaled by how many review units the change has."""
    caps = POSTURE_CAPS.get(posture)
    if caps is None:
        return 10**6  # brutal: uncapped
    base, per, low, high = caps["global"]
    return int(max(low, min(high, round(base + per * math.sqrt(max(units, 1))))))


_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_INLINE = re.compile(r"`([^`\n]+)`")
_MIN_CHECKABLE = 3  # a quoted fragment shorter than this, whitespace removed, proves nothing

# writing.md asks for 40-100 words and calls anything past the ceiling below a defective comment
# rather than a thorough one. Every rule here that actually holds is one the reducer measures: a
# prose rule nobody counts is a preference, and this one was ignored at 330 words in 26 of the 36
# findings on the two MRs the architecture assessment read.
#
# **Nothing is dropped for length.** Losing a demonstrated bug because its author was wordy is the
# worse outcome, so the bar demotes and it de-lists, and it never rejects:
#
#   over the ceiling      -> one severity rank lower, and the count is shown
#   over REPORT_ONLY      -> kept in the report, never posted inline
#
# The ceiling is higher for a design or ARCHITECT finding because its alternative *is* the finding:
# "do this instead, and here is what the current shape costs" cannot be said in forty words, while
# a naming problem that takes two hundred is padded.
BODY_WORD_CEILING = 150
STRUCTURAL_WORD_CEILING = 180
REPORT_ONLY_WORDS = 250


def word_ceiling(f: dict) -> int:
    """This finding's word ceiling — the structural one when its alternative is part of it."""
    structural = f.get("kind") == "design" or f.get("stage") == "ARCHITECT"
    return STRUCTURAL_WORD_CEILING if structural else BODY_WORD_CEILING


def apply_writing_bar(findings: list[dict]) -> tuple[list[dict], int, int]:
    """Demote an over-long finding one rank, and de-list a very long one from the inline thread.

    Runs **after** the caps, deliberately. A demotion that ran before them could turn a wordy
    CRITICAL into a WARNING and let a stage cap trim it, which would make length a reason a
    demonstrated bug disappears — exactly what this bar must not do. Running last, it changes the
    badge, the ranking and whether the comment is posted inline, and nothing else.
    """
    demoted = delisted = 0
    out: list[dict] = []
    for f in findings:
        # Recount rather than trust `f["words"]`: `validate` set that at parse time, and two later
        # steps grow the body — `collapse_patterns` appends its locations and `absorb_symptoms`
        # appends the symptom list. Measuring the stale count let a root that absorbed three
        # symptoms post inline past the ceiling while reporting a length it no longer had.
        words, ceiling = _body_words(f["body"]), word_ceiling(f)
        f = dict(f, words=words)
        f["inline"] = words <= REPORT_ONLY_WORDS
        if not f["inline"]:
            delisted += 1
            f["writing"] = (
                f"{words} words — report only; a comment this long is not read on a line of a diff"
            )
        elif words > ceiling:
            rank = _SEVERITY_RANK.get(f["severity"], 0)
            if rank + 1 < len(SEVERITIES):
                f["demoted_from"] = f["severity"]
                f["severity"] = SEVERITIES[rank + 1]
                demoted += 1
            f["writing"] = f"{words} words against a ceiling of {ceiling}"
        out.append(f)
    return out, demoted, delisted


def _body_words(body: str) -> int:
    """Words in the prose, ignoring fenced code — a long quote is evidence, not verbosity."""
    return len(_FENCE.sub(" ", body).split())


def _norm_title(title: str, width: int = 50) -> str:
    return "".join(ch for ch in (title or "").lower() if ch.isalnum())[:width]


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _rank(f: dict) -> tuple:
    return (
        _SEVERITY_RANK.get(f.get("severity", ""), 9),
        _STAGE_PRIORITY.get(f.get("stage", ""), 9),
        -len(f.get("body") or ""),
    )


def parse_output(text: str) -> list | None:
    """The JSON array in an agent's output, tolerating prose or a fence around it. None if none."""
    text = text.strip()
    if not text:
        return []
    for candidate in (text, *(m.group(1) for m in _FENCE.finditer(text))):
        try:
            data = json.loads(candidate)
            return data if isinstance(data, list) else None
        except json.JSONDecodeError:
            pass
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, list) else None
        except json.JSONDecodeError:
            return None
    return None


def quoted_fragments(body: str) -> list[str]:
    """Code the body quotes: every line of every fence, plus inline backtick spans."""
    fragments: list[str] = []
    for block in _FENCE.findall(body or ""):
        fragments.extend(line for line in block.splitlines() if line.strip())
    without_fences = _FENCE.sub("", body or "")
    fragments.extend(_INLINE.findall(without_fences))
    return fragments


def evidence_check(body: str, file_text: str | None, diff_text: str) -> str | None:
    """Why the finding is not evidenced, or None when a quoted fragment is found in the code.

    Whitespace is ignored on both sides because agents re-indent what they quote. A fragment may
    match the diff rather than the file so that "you deleted this guard" can quote the deleted
    line. Fragments too short to be meaningful are not checked, and a finding whose only quotes
    are that short passes unchecked rather than being dropped — a naming finding may legitimately
    quote only the name.
    """
    fragments = quoted_fragments(body)
    if not fragments:
        return "unquoted: the body quotes no code"
    checkable = [_squash(f) for f in fragments if len(_squash(f)) >= _MIN_CHECKABLE]
    if not checkable:
        return None
    haystack = _squash(file_text or "") + "\n" + _squash(diff_text or "")
    if any(f in haystack for f in checkable):
        return None
    return "quote not found: none of the quoted code appears in the cited file or the diff"


_LABEL_STAGE = (
    ("craft", "FUNCTION"),
    ("function", "FUNCTION"),
    ("architect", "ARCHITECT"),
    ("integration", "INTEGRATION"),
    ("po", "PO"),
    ("maker", "MAKER"),
)


def _stage_from_label(label: str) -> str:
    """`craft-3` -> FUNCTION, `architect` -> ARCHITECT: the fallback when an agent omits `stage`."""
    low = label.lower()
    for prefix, stage in _LABEL_STAGE:
        if low.startswith(prefix):
            return stage
    return label.upper()


def validate(
    raw: dict, source: str, repo: Path | None, diff_text: str, file_cache: dict[str, str | None]
) -> tuple[dict | None, str | None]:
    """A cleaned finding, or (None, reason) when it must be dropped.

    ``repo`` is None when the caller has no checkout — the CLI's deprecated diff mode, which
    embeds a truncated diff in the prompt instead of cloning. There the file check and the line
    bound cannot run and the quote is checked against the diff alone, because dropping every
    finding for a file that is merely not on disk would silence the pass rather than filter it.
    """
    if not isinstance(raw, dict):
        return None, "malformed: not an object"
    file = raw.get("file")
    if not isinstance(file, str) or not file.strip():
        return None, "malformed: no file"
    cited_line = raw.get("line")
    if cited_line is None:
        return None, "malformed: line is not an integer"
    try:
        line = int(cited_line)
    except (TypeError, ValueError):
        return None, "malformed: line is not an integer"
    title = (raw.get("title") or "").strip()
    body = (raw.get("body") or "").strip()
    if not title:
        return None, "malformed: no title"
    if not body:
        return None, "malformed: no body"
    severity = (raw.get("severity") or "").upper()
    if severity not in SEVERITIES:
        return None, f"malformed: severity {raw.get('severity')!r}"
    stage = (raw.get("stage") or _stage_from_label(source)).upper()
    kind = (raw.get("kind") or ("craft" if stage == "FUNCTION" else "bug")).lower()
    if kind not in KINDS:
        kind = "bug"

    if line < 1:
        return None, f"malformed: line {line} is not a line number"
    text = None
    if repo is not None:
        if file not in file_cache:
            path = repo / file
            file_cache[file] = (
                path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
            )
        text = file_cache[file]
        if text is None:
            return None, f"file not in repository: {file}"
        if line > max(1, text.count("\n") + 1):
            return None, f"malformed: line {line} is beyond the end of {file}"
    reason = evidence_check(body, text, diff_text)
    if reason:
        return None, reason

    return {
        "stage": stage,
        "kind": kind,
        "file": file,
        "line": line,
        "severity": severity,
        "title": title,
        "body": body,
        "source": source,
        "words": _body_words(body),
    }, None


def dedupe_exact(findings: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    best: dict[tuple, dict] = {}
    dropped: list[tuple[dict, str]] = []
    for f in findings:
        key = (f["file"], f["line"], _norm_title(f["title"]))
        cur = best.get(key)
        if cur is None:
            best[key] = f
        elif _rank(f) < _rank(cur):
            dropped.append((cur, f"duplicate of {f['source']}'s finding at the same line"))
            best[key] = f
        else:
            dropped.append((f, f"duplicate of {cur['source']}'s finding at the same line"))
    return list(best.values()), dropped


def collapse_patterns(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """One title reported at many lines becomes one finding listing every location.

    Within one file any repeat collapses; across files it takes three occurrences, because two
    files sharing a title may be two different problems and three is a pattern. The primary
    keeps its own line so it stays anchored; the rest are listed, not deleted.
    """
    groups: dict[tuple, list[dict]] = {}
    for f in findings:
        groups.setdefault((f["stage"], _norm_title(f["title"], 40)), []).append(f)
    kept: list[dict] = []
    collapsed: list[dict] = []
    for group in groups.values():
        distinct = {(f["file"], f["line"]) for f in group}
        files = {f["file"] for f in group}
        if len(distinct) < 2 or (len(files) > 1 and len(distinct) < 3):
            kept.extend(group)
            continue
        group.sort(key=_rank)
        primary, rest = group[0], group[1:]
        locations = [f"{f['file']}:{f['line']}" for f in rest]
        primary = dict(primary)
        primary["body"] = primary["body"] + "\n\n**Also at:** " + ", ".join(locations)
        primary["locations"] = [f"{primary['file']}:{primary['line']}", *locations]
        kept.append(primary)
        collapsed.append(
            {
                "primary": f"{primary['file']}:{primary['line']}",
                "title": primary["title"],
                "absorbed": locations,
            }
        )
    return kept, collapsed


def collapse_locus(findings: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Two *different* passes on one (file, line) keep the highest-ranked framing, in every posture.

    Highest-ranked means `_rank`: severity first, then altitude. Severity leads deliberately — a
    CRITICAL bug on a line must not be replaced by a WARNING about that line's shape, however much
    higher the altitude — so altitude only settles the tie, which is the common case because two
    passes describing one locus usually agree on how bad it is.

    PO and MAKER are exempt — a design decision and a bug can both be true of one line. This used
    to be calm-only, which is why a single coupling could arrive three times from the function,
    architect and integration passes: each was independently right, and nothing merged them. Volume
    is a posture question; three framings of one locus is not.

    **Two findings from the same pass never collapse into each other.** A pass reporting twice on
    one line has found two things, not one thing twice — a name that hides its direction and a
    boundary check that is missing are both true of the same signature — and the case this rule
    exists for, one repeated title at many lines, is `collapse_patterns`' job. Keying on the line
    alone dropped the second of the two.
    """
    kept: list[dict] = []
    dropped: list[tuple[dict, str]] = []
    by_locus: dict[tuple, list[dict]] = {}
    for f in findings:
        if f["stage"] in LOCUS_EXEMPT_STAGES:
            kept.append(f)
        else:
            by_locus.setdefault((f["file"], f["line"]), []).append(f)
    for group in by_locus.values():
        winner = min(group, key=_rank)["stage"]
        # Every finding from the winning pass survives, so a pass that reported twice on one line
        # keeps both; the other passes' framings of that line go. Deciding per *stage* rather than
        # per finding is what makes this independent of the order the passes happened to finish in.
        for f in group:
            if f["stage"] == winner:
                kept.append(f)
            else:
                dropped.append((f, f"same line as a {winner} finding (one framing per locus)"))
    return kept, dropped


def apply_caps(
    findings: list[dict], posture: str, units: int
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """The posture's caps. A CRITICAL is never dropped by any of them — a cap is a bound on how
    much optional advice reaches a shared thread, not a reason to hide a demonstrated bug."""
    if posture not in POSTURE_CAPS:
        raise ValueError(f"unknown posture {posture!r}; expected one of {POSTURES}")
    caps = POSTURE_CAPS[posture]
    if caps is None:
        return findings, []  # brutal: the drop rules above are the only filter
    per_file, stage_caps = caps["craft_per_file"], caps["stage"]
    ceiling = global_cap(posture, units)
    dropped: list[tuple[dict, str]] = []
    kept: list[dict] = []
    craft_by_file: dict[str, int] = {}
    stage_counts: dict[str, int] = {}
    for f in sorted(findings, key=_rank):
        critical = f["severity"] == "CRITICAL"
        if f["stage"] in ("FUNCTION", "SENIOR"):
            n = craft_by_file.get(f["file"], 0)
            if n >= per_file and not critical:
                dropped.append((f, f"{posture} cap: {per_file} craft findings per file"))
                continue
            craft_by_file[f["file"]] = n + 1
        cap = stage_caps.get(f["stage"])
        if cap is not None:
            n = stage_counts.get(f["stage"], 0)
            if n >= cap and not critical:
                dropped.append((f, f"{posture} cap: {cap} {f['stage']} findings"))
                continue
            stage_counts[f["stage"]] = n + 1
        kept.append(f)
    # Global ceiling: never drops CRITICAL/WARNING, PO output, or the first DESIGN_SLOTS design
    # findings; trims the lowest-ranked SUGGESTIONs. PO is protected because its roll-ups *are*
    # the acceptance-criteria report, and a bug-heavy MR would otherwise delete the whole pass on
    # exactly the changes where AC coverage matters most.
    design_slots = [f for f in sorted(kept, key=_rank) if f["kind"] == "design"][:DESIGN_SLOTS]
    design_slot_ids = {id(f) for f in design_slots}
    protected = [
        f
        for f in kept
        if f["severity"] != "SUGGESTION" or f["stage"] == "PO" or id(f) in design_slot_ids
    ]
    protected_ids = {id(f) for f in protected}
    optional = [f for f in kept if id(f) not in protected_ids]
    room = max(0, ceiling - len(protected))
    for f in optional[room:]:
        dropped.append(
            (f, f"{posture} cap: global ceiling of {ceiling} for {units} review unit(s)")
        )
    return protected + optional[:room], dropped


# The passes that can own a *root*: a decision, rather than one of its visible consequences. Both
# are ranked above the local passes in `_STAGE_PRIORITY`, and the priority order is what decides
# which of two roots wins when they land in one unit.
_ROOT_STAGES = ("MAKER", "ARCHITECT")

# A finding to its enclosing symbol, or None when nothing contains it. `_unit_lookup` builds one
# from the mapper's plan; `absorb_symptoms` takes it as a parameter rather than reaching for the
# plan itself, so the locus rule can be tested without a plan file.
UnitLookup = Callable[[dict], str | None]


def _unit_lookup(plan: dict | None) -> UnitLookup:
    """finding -> its enclosing symbol from the mapper's plan, or None when nothing contains it.

    ``None`` is meaningful rather than a failure: a finding on a class declaration or a module
    header sits outside every function unit, and a root there legitimately owns its whole file.
    Falls back to None for every finding when no plan is given, which is the CLI today — it does
    not run the mapper yet, so its absorption stays file-wide until it does.
    """
    lookup = _symbol_lookup(plan)

    def unit_of(f: dict) -> str | None:
        return lookup(f["file"], f["line"])

    return unit_of


MIN_SYMPTOMS = 2  # one symptom is more actionable on its own line than buried in a longer comment


def _is_root(f: dict) -> bool:
    return f["kind"] == "design" or f["stage"] in _ROOT_STAGES


def absorb_symptoms(findings: list[dict], unit_of: UnitLookup) -> tuple[list[dict], int]:
    """Fold local symptoms into the finding that names the decision causing them.

    A wrong root decision shows up as several small findings — an untyped local here, a runtime
    type check there, a helper extracted from a loop that should not exist. Posted separately they
    read as nitpicks and are correctly trimmed by every volume bound in the pipeline; posted as one
    comment that names them, they justify reshaping the code. Only the design and ARCHITECT passes
    can see the root, so their comment is where the list belongs.

    Three rules, and each of them is a thing that went wrong on a real MR:

    **The locus is the enclosing unit, not the file.** ``unit_of`` maps a finding to its enclosing
    symbol from the mapper's plan; a root that resolves to no unit (a class declaration, a module
    header) still absorbs across its file, which is the only behaviour available before the plan
    reached this consumer. Without it, a design root in one method absorbed a naming problem in an
    unrelated method of the same file.

    **A root absorbs only what is below it.** `_STAGE_PRIORITY` decides, so a design finding can
    absorb an ARCHITECT one restating it — which on one real MR it could not, and the author got the
    same coupling described from three altitudes.

    **A bug is never absorbed. It gains a pointer instead.** A data-loss bug must not be buried
    inside a design comment the author may well disagree with, so it stays standalone and gets one
    line saying where its root is. On another MR the "distance unit modelled three times" finding and
    the "silently reads unknown units as metres" bug were the same decision and its consequence;
    both needed to post, and neither needed to be the other's footnote.
    """
    live = [f for f in findings if f["stage"] != "PO"]
    roots = [f for f in live if _is_root(f)]
    if not roots:
        return findings, 0

    def _altitude(f: dict) -> tuple:
        """Sort key that puts altitude ahead of severity, which is the reverse of `_rank`.

        Deliberate, and the one place in this file where it is. `_rank` leads on severity because
        it answers "which of these matters most"; here the question is "which of these names the
        decision", and a CRITICAL restatement from a lower pass is still a restatement. Keying on
        `_rank` let a CRITICAL ARCHITECT finding out-rank the MAKER design root and collect the
        symptoms, which is the opposite of what this function is for.
        """
        return (_STAGE_PRIORITY.get(f["stage"], 9), _rank(f))

    def root_for(f: dict) -> dict | None:
        """The highest-altitude root above this finding whose unit contains it."""
        unit = unit_of(f)
        candidates = [
            r
            for r in roots
            if r["file"] == f["file"]
            and id(r) != id(f)
            and _STAGE_PRIORITY.get(r["stage"], 9) < _STAGE_PRIORITY.get(f["stage"], 9)
            and (unit_of(r) is None or unit_of(r) == unit)
        ]
        return min(candidates, key=_altitude) if candidates else None

    absorbable: dict[int, list[dict]] = {}
    pointers: dict[int, dict] = {}
    for f in live:
        root = root_for(f)
        if root is None:
            continue
        if f["kind"] == "bug" or f["severity"] == "CRITICAL":
            pointers[id(f)] = root
        else:
            absorbable.setdefault(id(root), []).append(f)

    folded = {id(f) for group in absorbable.values() if len(group) >= MIN_SYMPTOMS for f in group}
    root_note = {rid: g for rid, g in absorbable.items() if len(g) >= MIN_SYMPTOMS}

    out: list[dict] = []
    for f in findings:
        if id(f) in folded:
            continue
        # Both lookups read the ORIGINAL finding's id, before either branch rebinds `f` to a copy.
        # Reading the second one after the fold missed every time, so a finding that both collected
        # symptoms and sat under a root of its own silently lost the pointer this function promises.
        group = root_note.get(id(f))
        root = pointers.get(id(f))
        if group:
            listed = "\n".join(f"- {x['title']} (`{x['file']}:{x['line']}`)" for x in group)
            f = dict(
                f,
                body=f["body"] + "\n\n**Also flagged as symptoms of the same decision:**\n"
                f"{listed}",
                symptoms=[f"{x['file']}:{x['line']}" for x in group],
            )
        if root:
            f = dict(
                f,
                body=f["body"] + f"\n\nRoot: see `{root['file']}:{root['line']}` — {root['title']}",
                root=f"{root['file']}:{root['line']}",
            )
        out.append(f)
    return out, len(folded)


def reduce_candidates(
    candidates: list[tuple[str, dict]],
    repo: Path | None,
    diff_text: str = "",
    posture: str = "high",
    units: int = 1,
    preserve_all: bool = False,
    plan: dict | None = None,
    parsed: dict[str, dict] | None = None,
) -> dict:
    """Validate, dedupe, collapse, absorb and cap already-parsed candidates.

    Split out from ``reduce_findings`` so the CLI can share it. The CLI's passes return parsed
    comment objects rather than raw text — the parse happens inside the reviewer, per call — and
    the alternative was for the CLI to keep its own reduce step, which is precisely the divergence
    this refactor removes: the consumer that actually *posts* was the one without the quote check,
    the file check and the pattern collapse.

    ``preserve_all`` turns off the two lossy steps — symptom absorption and the caps — while
    leaving every drop rule and dedup on. It is implied by the ``brutal`` posture and requested
    explicitly by learning mode, which wants the same thing for a different reason: a lesson needs
    a pattern's second and third instance, and a brutal review needs every symptom the author could
    learn to recognise. So it is named for what it does rather than for who asks.

    Absorption is the part worth understanding. The symptoms it folds away are the recognisable
    surface of a decision — an untyped local, a runtime type check, a helper extracted from a loop
    — and someone who asked to see everything is asking for exactly those.
    """
    file_cache: dict[str, str | None] = {}
    accepted: list[dict] = []
    dropped: list[tuple[dict, str]] = []
    for label, raw in candidates:
        finding, reason = validate(raw, label, repo, diff_text, file_cache)
        if finding is None:
            dropped.append((raw if isinstance(raw, dict) else {"raw": raw}, reason or "dropped"))
        else:
            accepted.append(finding)

    # Checked here rather than in `apply_caps`, which is the only other place that reads the
    # posture and is skipped entirely under `preserve_all` — so an unrecognised tier used to run
    # the whole pipeline and come back with the misspelling echoed beside a ceiling of a million.
    if posture not in POSTURE_CAPS:
        raise ValueError(f"unknown posture {posture!r}; expected one of {POSTURES}")

    # brutal implies preserve_all: a posture with no ceilings that still folded symptoms into
    # their root would be lifting the numbers while keeping the one lossy step that is not a number.
    preserve_all = preserve_all or posture == "brutal"

    findings, d = dedupe_exact(accepted)
    dropped.extend(d)
    findings, collapsed = collapse_patterns(findings)
    findings, d = collapse_locus(findings)
    dropped.extend(d)
    absorbed = 0
    if not preserve_all:
        findings, absorbed = absorb_symptoms(findings, _unit_lookup(plan))
        findings, d = apply_caps(findings, posture, units)
        dropped.extend(d)
    findings, demoted, delisted = apply_writing_bar(findings)
    findings.sort(key=_rank)

    return _summarise(
        findings,
        dropped,
        collapsed,
        parsed or {},
        posture,
        units,
        absorbed=absorbed,
        demoted=demoted,
        report_only=delisted,
    )


def _summarise(
    findings: list[dict],
    dropped: list[tuple[dict, str]],
    collapsed: list[dict],
    parsed: dict[str, dict],
    posture: str,
    units: int,
    *,
    absorbed: int,
    demoted: int,
    report_only: int,
) -> dict:
    """The result object. Split out so `reduce_candidates` reads as the pipeline it is.

    The six steps in that function are what anyone opens it to check, and they were twelve lines
    between a validation loop and twenty lines of tallying. Nothing here decides anything; it
    counts what the steps did.
    """

    def tally(key: str) -> dict[str, int]:
        counted: dict[str, int] = {}
        for f in findings:
            counted[f.get(key, "?")] = counted.get(f.get(key, "?"), 0) + 1
        return dict(sorted(counted.items()))

    return {
        "kept": findings,
        "dropped": [{"finding": f, "reason": r} for f, r in dropped],
        "collapsed": collapsed,
        "sources": parsed,
        "posture": posture,
        "units": units,
        "global_cap": global_cap(posture, units),
        "counts": {
            "kept": len(findings),
            "dropped": len(dropped),
            "collapsed": len(collapsed),
            "absorbed": absorbed,
            "demoted": demoted,
            "report_only": report_only,
            "overlong": sum(1 for f in findings if f.get("words", 0) > word_ceiling(f)),
            "by_severity": tally("severity"),
            "by_stage": tally("stage"),
            "by_kind": tally("kind"),
        },
    }


def reduce_findings(
    sources: dict[str, str],
    repo: Path | None,
    diff_text: str = "",
    posture: str = "high",
    units: int = 1,
    preserve_all: bool = False,
    plan: dict | None = None,
) -> dict:
    """The whole pipeline from raw agent output. `sources` maps an agent label to its output text."""
    candidates: list[tuple[str, dict]] = []
    parsed: dict[str, dict] = {}
    for label, text in sources.items():
        data = parse_output(text)
        if data is None:
            parsed[label] = {"parsed": 0, "unparseable": True}
            continue
        parsed[label] = {"parsed": len(data), "unparseable": False}
        candidates.extend((label, raw) for raw in data)
    return reduce_candidates(
        candidates,
        repo,
        diff_text,
        posture,
        units,
        preserve_all=preserve_all,
        plan=plan,
        parsed=parsed,
    )


def _symbol_lookup(plan: dict | None) -> Callable[[str, int], str | None]:
    """(file, line) -> enclosing function name, from the mapper's plan JSON, when given."""
    units = list((plan or {}).get("units") or []) + list((plan or {}).get("trivial") or [])
    for batch in (plan or {}).get("batches") or []:
        units.extend(batch.get("units") or [])

    def lookup(file: str, line: int) -> str | None:
        hits = [
            u
            for u in units
            if u.get("file") == file and u.get("start_line", 0) <= line <= u.get("end_line", 0)
        ]
        if not hits:
            return None
        symbol = min(hits, key=lambda u: u["end_line"] - u["start_line"]).get("symbol")
        return str(symbol) if symbol is not None else None

    return lookup


def _excerpt(repo: Path | None, file: str, line: int, around: int = 1) -> list[str]:
    """The anchor line and its neighbours from the working tree, numbered, so the reader rarely
    has to open the file. Empty when the file is not readable here."""
    if repo is None:
        return []
    path = repo / file
    if not path.is_file():
        return []
    source = path.read_text(encoding="utf-8", errors="replace").splitlines()
    lo, hi = max(1, line - around), min(len(source), line + around)
    return [f"{n:>5} | {source[n - 1]}" for n in range(lo, hi + 1)]


def render_markdown(result: dict, plan: dict | None = None, repo: Path | None = None) -> str:
    """The full list grouped by file, then the reducer's own drops. The orchestrator writes the
    orientation, the top three and the not-covered section around this.

    Each finding's `path:line` sits alone at the start of a line because IDE terminals turn that
    shape into a link, and a two-line excerpt follows so the reader rarely needs to click."""
    lookup = _symbol_lookup(plan)
    counts = result["counts"]
    sev = counts["by_severity"]
    out = [
        f"{counts['kept']} finding(s): {sev.get('CRITICAL', 0)} critical, {sev.get('WARNING', 0)} warning, "
        f"{sev.get('SUGGESTION', 0)} suggestion — posture {result['posture']}; "
        f"{counts['dropped']} dropped by the reducer, {counts['collapsed']} pattern(s) collapsed"
        + (
            f"; {counts['overlong']} over {BODY_WORD_CEILING} words"
            if counts.get("overlong")
            else ""
        ),
        "",
    ]
    by_file: dict[str, list[dict]] = {}
    for f in result["kept"]:
        by_file.setdefault(f["file"], []).append(f)
    for file in sorted(by_file):
        out.append(f"## {file}")
        out.append("")
        for f in sorted(by_file[file], key=lambda f: (f["line"], _rank(f))):
            symbol = lookup(file, f["line"])
            label = "REDESIGN" if f["kind"] == "design" else f["severity"]
            # The writing bar's note, where it acted. Without it a finding shows a badge one rank
            # below the one its pass gave it and nothing on the page says why, which reads as the
            # reviewer having been unsure rather than wordy.
            bar = f" · {f['writing']}" if f.get("writing") else ""
            if f.get("demoted_from"):
                bar = f" · demoted from {f['demoted_from']}, {f['writing']}"
            out.append(f"**{label}** · {f['stage']} — {f['title']}{bar}")
            out.append(f"{file}:{f['line']}" + (f" · {symbol}" if symbol else ""))
            excerpt = _excerpt(repo, file, f["line"])
            if excerpt:
                out.append("```")
                out.extend(excerpt)
                out.append("```")
            out.append("")
            out.append(f["body"])
            out.append("")
    if result["dropped"]:
        out.append("## Dropped by the reducer")
        out.append("")
        for d in result["dropped"]:
            f = d["finding"]
            where = f"{f.get('file', '?')}:{f.get('line', '?')}" if isinstance(f, dict) else "?"
            out.append(
                f"- {where} — {f.get('title', '(no title)') if isinstance(f, dict) else f} — {d['reason']}"
            )
        out.append("")
    return "\n".join(out)


# --- self-test ------------------------------------------------------------------------

_SAMPLE_FILE = """\
public class Svc {
    public int total(List<Item> items) {
        int sum = 0;
        for (Item i : items) sum += i.price();
        return sum;
    }
    public void save(Order o) {
        repo.save(o);
    }
}
"""


def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, got, want) -> None:
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    def finding(**over) -> dict:
        base = {
            "stage": "FUNCTION",
            "kind": "craft",
            "file": "Svc.java",
            "line": 3,
            "severity": "WARNING",
            "title": "Accumulator loop hides a sum",
            "body": "```java\nfor (Item i : items) sum += i.price();\n```\nUse a stream.",
        }
        base.update(over)
        return base

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "Svc.java").write_text(_SAMPLE_FILE)

        check("parse: plain array", parse_output('[{"a": 1}]'), [{"a": 1}])
        check("parse: fenced", parse_output('Here:\n```json\n[{"a": 1}]\n```\n'), [{"a": 1}])
        check("parse: prose around", parse_output('Findings follow. [{"a": 1}] Done.'), [{"a": 1}])
        check("parse: empty", parse_output("   "), [])
        check("parse: garbage", parse_output("no json here"), None)

        ok = reduce_findings({"craft-1": json.dumps([finding()])}, repo)
        check("valid finding kept", ok["counts"]["kept"], 1)

        unquoted = reduce_findings({"a": json.dumps([finding(body="This loop is unclear.")])}, repo)
        check("unquoted dropped", [d["reason"][:8] for d in unquoted["dropped"]], ["unquoted"])

        invented = reduce_findings(
            {"a": json.dumps([finding(body="```java\nreturn cache.get(key);\n```")])}, repo
        )
        check("invented quote dropped", invented["counts"]["kept"], 0)
        check(
            "invented quote reason",
            invented["dropped"][0]["reason"].startswith("quote not found"),
            True,
        )

        # A quote of a deleted line is evidenced by the diff, not the file.
        deleted = reduce_findings(
            {"a": json.dumps([finding(body="You removed `if (o == null) return;` here.")])},
            repo,
            diff_text="-        if (o == null) return;\n",
        )
        check("deleted line evidenced via diff", deleted["counts"]["kept"], 1)

        short = reduce_findings(
            {"a": json.dumps([finding(body="`sum` says nothing; call it `subtotal`.")])}, repo
        )
        check("short quotes pass unchecked", short["counts"]["kept"], 1)

        missing = reduce_findings({"a": json.dumps([finding(file="Nope.java")])}, repo)
        check(
            "missing file dropped",
            missing["dropped"][0]["reason"].startswith("file not in repository"),
            True,
        )
        beyond = reduce_findings({"a": json.dumps([finding(line=999)])}, repo)
        check("line beyond file dropped", "beyond the end" in beyond["dropped"][0]["reason"], True)
        bad_sev = reduce_findings({"a": json.dumps([finding(severity="HIGH")])}, repo)
        check(
            "bad severity dropped",
            bad_sev["dropped"][0]["reason"].startswith("malformed: severity"),
            True,
        )

        longer = finding(
            body="```java\nfor (Item i : items) sum += i.price();\n```\nAn accumulator loop hides "
            "the sum; a stream states it. This is the longer body of the two."
        )
        dup = reduce_findings({"a": json.dumps([finding()]), "b": json.dumps([longer])}, repo)
        check("exact duplicate collapses to one", dup["counts"]["kept"], 1)
        check("duplicate keeps the longer body", "longer body" in dup["kept"][0]["body"], True)

        pattern = reduce_findings(
            {"a": json.dumps([finding(line=3), finding(line=4, body="```java\nreturn sum;\n```")])},
            repo,
        )
        check("same pattern in one file collapses", pattern["counts"]["kept"], 1)
        check(
            "collapsed lists locations",
            pattern["kept"][0]["locations"],
            ["Svc.java:3", "Svc.java:4"],
        )
        check("collapse is not a drop", pattern["counts"]["dropped"], 0)

        (repo / "Other.java").write_text("class Other { void f() { int sum = 0; } }\n")
        two_files = reduce_findings(
            {
                "a": json.dumps(
                    [
                        finding(line=3),
                        finding(file="Other.java", line=1, body="```java\nint sum = 0;\n```"),
                    ]
                )
            },
            repo,
        )
        check("two files, two occurrences stay separate", two_files["counts"]["kept"], 2)

        two_stages = {
            "craft": json.dumps([finding()]),
            "arch": json.dumps(
                [finding(stage="ARCHITECT", kind="bug", title="Service owns arithmetic")]
            ),
        }
        for posture in POSTURES:
            one = reduce_findings(dict(two_stages), repo, posture=posture)
            check(
                f"{posture} keeps one framing per locus",
                [f["stage"] for f in one["kept"]],
                ["ARCHITECT"],
            )

        many = [
            finding(line=3 + n, severity="SUGGESTION", title=f"craft note {n}") for n in range(5)
        ]
        capped = reduce_findings({"a": json.dumps(many)}, repo)
        check("high caps craft per file", capped["counts"]["kept"], _caps("high")["craft_per_file"])
        brutal = reduce_findings({"a": json.dumps(many)}, repo, posture="brutal")
        check("brutal keeps all craft", brutal["counts"]["kept"], 5)

        check(
            "ceiling scales sublinearly with units",
            [global_cap("high", n) for n in (1, 9, 25, 400)],
            [14, 22, 30, 40],
        )
        check("brutal is uncapped", global_cap("brutal", 1) > 1000, True)
        sizes = (1, 5, 25, 400)
        check(
            "the mr tier is quieter at every size",
            [global_cap("mr", n) < global_cap("high", n) for n in sizes],
            [True] * len(sizes),
        )
        check(
            "the mr ceiling reproduces the 6/10/14 table",
            [global_cap("mr", n) for n in (5, 25, 200)],
            [6, 10, 14],
        )
        check(
            "and reaches its ceiling rather than drifting past it",
            [global_cap("mr", n) for n in (50, 55, 10_000)],
            [13, 14, 14],
        )

        root = finding(
            line=2,
            stage="MAKER",
            kind="design",
            severity="WARNING",
            title="Service reconstructs the price",
            body="```java\nint sum = 0;\n```\nWrong tier.",
        )
        two = [
            finding(line=3, title="sum says nothing"),
            finding(line=4, title="loop hides the total"),
        ]
        absorbed = reduce_findings({"maker": json.dumps([root]), "craft-1": json.dumps(two)}, repo)
        check("two craft symptoms fold into the design root", absorbed["counts"]["absorbed"], 2)
        check(
            "the root names them",
            "symptoms of the same decision"
            in [f for f in absorbed["kept"] if f["kind"] == "design"][0]["body"],
            True,
        )
        one = reduce_findings({"maker": json.dumps([root]), "craft-1": json.dumps(two[:1])}, repo)
        check("one symptom stays on its own line", one["counts"]["absorbed"], 0)
        bug = [dict(f, kind="bug", severity="CRITICAL") for f in two]
        kept_bugs = reduce_findings({"maker": json.dumps([root]), "craft-1": json.dumps(bug)}, repo)
        check("a bug is never absorbed", kept_bugs["counts"]["absorbed"], 0)
        preserved = reduce_findings(
            {"maker": json.dumps([root]), "craft-1": json.dumps(two)}, repo, preserve_all=True
        )
        check("preserve_all keeps the symptoms", preserved["counts"]["absorbed"], 0)
        loud = reduce_findings(
            {"maker": json.dumps([root]), "craft-1": json.dumps(two)}, repo, posture="brutal"
        )
        check("brutal implies preserve_all", loud["counts"]["absorbed"], 0)

        no_repo = reduce_findings(
            {"a": json.dumps([finding(file="Gone.java", line=9)])},
            None,
            diff_text="+        for (Item i : items) sum += i.price();\n",
        )
        check("no checkout: the diff is the evidence", no_repo["counts"]["kept"], 1)

        (repo / "Big.java").write_text("".join(f"        int sum{n} = {n};\n" for n in range(30)))
        crit = [
            finding(
                file="Big.java",
                line=1 + n,
                severity="CRITICAL",
                kind="bug",
                stage="INTEGRATION",
                title=f"bug {n}",
                body=f"```java\nint sum{n} = {n};\n```\nLost write.",
            )
            for n in range(25)
        ]
        ceiling = reduce_findings({"a": json.dumps(crit)}, repo)
        check("caps never drop CRITICAL", ceiling["counts"]["kept"], 25)
        warn = [dict(c, severity="WARNING") for c in crit]
        stage_capped = reduce_findings({"a": json.dumps(warn)}, repo)
        check(
            "stage cap bounds WARNINGs",
            stage_capped["counts"]["kept"],
            _caps("high")["stage"]["INTEGRATION"],
        )

        wordy = finding(severity="CRITICAL", body="```java\nint sum = 0;\n```\n" + "proof " * 200)
        long_run = reduce_findings({"a": json.dumps([wordy])}, repo)
        check("an over-long body is kept", long_run["counts"]["kept"], 1)
        check("and counted", long_run["counts"]["overlong"], 1)
        check("code fences do not count as words", long_run["kept"][0]["words"], 200)
        check("it is demoted one rank", long_run["kept"][0]["severity"], "WARNING")
        check("and says why", "ceiling of 150" in long_run["kept"][0]["writing"], True)
        check("but still posted inline", long_run["kept"][0]["inline"], True)
        check(
            "a short body is not flagged",
            reduce_findings({"a": json.dumps([finding()])}, repo)["counts"]["overlong"],
            0,
        )
        structural = finding(
            stage="ARCHITECT",
            kind="design",
            severity="WARNING",
            body="```java\nint sum = 0;\n```\n" + "proof " * 170,
        )
        wider = reduce_findings({"a": json.dumps([structural])}, repo)
        check("a design finding gets the wider ceiling", wider["kept"][0]["severity"], "WARNING")
        essay = finding(body="```java\nint sum = 0;\n```\n" + "proof " * 300)
        off_thread = reduce_findings({"a": json.dumps([essay])}, repo)
        check("a 300-word body is kept", off_thread["counts"]["kept"], 1)
        check("but not posted inline", off_thread["kept"][0]["inline"], False)
        check("and not demoted as well", off_thread["kept"][0].get("demoted_from"), None)

        unparseable = reduce_findings({"a": "the agent wrote prose only"}, repo)
        check("unparseable source recorded", unparseable["sources"]["a"]["unparseable"], True)

        md = render_markdown(
            ok,
            {
                "units": [
                    {"file": "Svc.java", "start_line": 2, "end_line": 6, "symbol": "Svc.total"}
                ]
            },
        )
        check("markdown names the function", "Svc.total" in md, True)

    for f in failures:
        print(f"FAIL {f}", file=sys.stderr)
    print(f"{'FAILED' if failures else 'OK'}: {len(failures)} failure(s)")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "files", nargs="*", help="one agent output per file; the stem labels the source"
    )
    ap.add_argument(
        "--repo", type=Path, default=Path("."), help="repository the findings cite (default: cwd)"
    )
    ap.add_argument(
        "--diff", type=Path, help="the reviewed diff, so a quote of a deleted line still counts"
    )
    ap.add_argument(
        "--plan",
        type=Path,
        help="the mapper's plan JSON. Names the enclosing function in the rendered "
        "list, and scopes symptom absorption to the unit rather than the file",
    )
    ap.add_argument(
        "--posture",
        choices=POSTURES,
        default="high",
        help="how much of what was found gets reported: mr (worth interrupting a "
        "shared MR thread with — what the CLI posts), high (what a senior would "
        "mention while pairing — the default) or brutal (everything that clears "
        "the evidence bar). The evidence bar is identical in all three.",
    )
    ap.add_argument("--brutal", action="store_true", help="alias for --posture brutal")
    ap.add_argument(
        "--units",
        type=int,
        default=0,
        help="review-unit count for the global ceiling; taken from --plan when given",
    )
    ap.add_argument(
        "--markdown", action="store_true", help="render the grouped list instead of JSON"
    )
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    if not args.files:
        ap.error("no findings files given")

    sources: dict[str, str] = {}
    for name in args.files:
        path = Path(name)
        label = path.stem.replace("review-findings-", "")
        sources[label] = (
            path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        )
    diff_text = (
        args.diff.read_text(encoding="utf-8", errors="replace")
        if args.diff and args.diff.is_file()
        else ""
    )
    plan = json.loads(args.plan.read_text()) if args.plan and args.plan.is_file() else None

    posture = "brutal" if args.brutal else args.posture
    units = args.units or int(((plan or {}).get("counts") or {}).get("units") or 0) or len(sources)
    result = reduce_findings(sources, args.repo, diff_text, posture=posture, units=units, plan=plan)
    if args.markdown:
        print(render_markdown(result, plan, args.repo))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
