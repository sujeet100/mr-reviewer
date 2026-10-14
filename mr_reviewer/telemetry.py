"""
Record review runs on the MR itself (opt-in), so a local review can still be counted.

A ``--dry-run`` review used to leave no trace anywhere: the review happened, the
author fixed the code, and no evidence of the tool's use survived it. An audit of
review history could therefore only measure *posted* reviews, and had to
report its coverage figures as a floor on real usage rather than a measurement of
it. Nothing retrospective can recover those runs — GitLab does not audit
repository reads, and a dry run creates no note, no event and no server-side
trace — so the only fix is to start recording.

With ``MR_REVIEWER_RECORD_RUNS=1`` a dry run posts one MR-level note and no
inline comments, because a local run's detail belongs to the person who ran it;
a posted review gets the same counts folded into its normal summary either way.
Recording is opt-in because a dry run that writes to a shared MR thread is a
surprise to anyone who did not ask for it; a team that wants to measure usage
sets the variable for everyone.

Two design points are load-bearing.

**The trailer is the data.** Rendered prose is for the MR author; the HTML comment
at the end is for measurement. It does not render, it is cheap to parse exactly,
and it records what ``ReviewComment.to_markdown()`` throws away — the ``stage``
and ``kind`` of every finding. That erasure is why the pass that produced a
finding is unrecoverable for the whole existing corpus; the trailer closes it
permanently.

**Legacy notes have no trailer, and that is what makes them identifiable.** Every
marker note posted before this module existed was a posted review, so
"no trailer" must keep meaning "posted" — see ``is_posted_review``. Getting this
wrong would either make ``review-all`` re-review its whole history or make
it skip every locally-reviewed MR forever.
"""

import logging
import os
import re
from collections.abc import Iterable

logger = logging.getLogger(__name__)

# The census marker. Unchanged since the first commit and matched with startswith
# by every downstream consumer, so a mode suffix may follow it but nothing may
# precede it.
MARKER = "## 🤖 Automated Code Review"

TRAILER_RE = re.compile(r"<!--\s*mr-reviewer\s+(?P<body>[^>]*?)\s*-->", re.S)

# Modes that mean "this review was posted to the MR in full". Only these suppress
# a later re-review; a local run must not.
POSTED_MODES = ("post", "post-cached")

_STAGE_KEYS = {
    "PO": "stage_po",
    "ARCHITECT": "stage_architect",
    "SENIOR": "stage_senior",
    "MAKER": "stage_maker",
    "PROMPT": "stage_prompt",
}


def _counts(comments: Iterable) -> dict:
    """Tally a finished review along every axis the reviewer actually tracks.

    Only real fields are recorded. In particular there is no ``nits`` count: the
    reviewer has no nit severity, and treating SUGGESTION-plus-craft as "nit" is
    an interpretation, so it is recorded under its own honest name and left to
    the reader to interpret. Nor is there an "implemented" count — whether a
    finding was acted on is only knowable later, from replies and subsequent
    commits, and is derived at analysis time rather than guessed at review time.
    """
    comments = list(comments)
    c = {
        "total": len(comments),
        "critical": 0,
        "warning": 0,
        "suggestion": 0,
        "redesign": 0,
        "mustfix": 0,
        "kind_bug": 0,
        "kind_craft": 0,
        "kind_design": 0,
        "suggestion_craft": 0,
        "files": len({x.file for x in comments if x.file}),
    }
    c.update(dict.fromkeys(_STAGE_KEYS.values(), 0))

    for x in comments:
        # Design findings render as a REDESIGN badge instead of a severity word, so
        # they are counted as their own class here for the same reason — a root
        # decision is a different kind of feedback, not a milder one.
        if x.kind == "design":
            c["redesign"] += 1
            if x.severity == "CRITICAL":
                c["mustfix"] += 1
        elif x.severity == "CRITICAL":
            c["critical"] += 1
        elif x.severity == "WARNING":
            c["warning"] += 1
        elif x.severity == "SUGGESTION":
            c["suggestion"] += 1

        if x.kind in ("bug", "craft", "design"):
            c[f"kind_{x.kind}"] += 1
        if x.severity == "SUGGESTION" and x.kind == "craft":
            c["suggestion_craft"] += 1

        key = _STAGE_KEYS.get(x.stage)
        if key:
            c[key] += 1

    return c


def build_trailer(
    mode: str,
    head_sha: str,
    comments: Iterable,
    run: int = 1,
    maker: bool = False,
    learn: bool = False,
    duration_s: int | None = None,
) -> str:
    """Render the machine-readable run record. Invisible when the note renders."""
    fields = [f"mode={mode}", f"head={(head_sha or '')[:12]}", f"run={run}"]
    fields += [f"{k}={v}" for k, v in _counts(comments).items()]
    fields += [f"maker={int(bool(maker))}", f"learn={int(bool(learn))}"]
    if duration_s is not None:
        fields.append(f"duration_s={int(duration_s)}")
    return "<!-- mr-reviewer " + " ".join(fields) + " -->"


def parse_trailer(body: str) -> dict | None:
    """Read a run record back out of a note body.

    Returns None when the note carries no trailer — which for a marker note means
    it predates this module and was therefore a posted review.
    """
    if not body:
        return None
    m = TRAILER_RE.search(body)
    if not m:
        return None
    out: dict = {}
    for token in m.group("body").split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        out[key] = int(value) if value.lstrip("-").isdigit() else value
    return out or None


def is_posted_review(body: str) -> bool:
    """True when a marker note represents a review that was posted in full.

    This is the gate ``has_bot_reviewed`` needs. A local-run note must not count,
    or a dry run would silently suppress the real review that was meant to follow
    it. A marker note with no trailer predates this module and does count.
    """
    if not body or not body.lstrip().startswith(MARKER):
        return False
    trailer = parse_trailer(body)
    if trailer is None:
        return True
    return str(trailer.get("mode", "")).startswith(POSTED_MODES)


def build_local_run_note(
    comments: Iterable,
    head_sha: str,
    run: int = 1,
    maker: bool = False,
    learn: bool = False,
    duration_s: int | None = None,
) -> str:
    """The note a local (dry-run) review leaves on the MR.

    Deliberately short. It says the review happened and what it found in
    aggregate, and points at where the detail lives, because the detail is on the
    reviewer's screen and duplicating it here would make a dry run
    indistinguishable from a real one.
    """
    comments = list(comments)
    c = _counts(comments)

    if not comments:
        headline = "No significant issues found."
    else:
        parts = []
        for singular, plural, key in (
            ("critical", "critical", "critical"),
            ("warning", "warnings", "warning"),
            ("suggestion", "suggestions", "suggestion"),
            ("redesign", "redesign", "redesign"),
        ):
            if c[key]:
                parts.append(f"{c[key]} {singular if c[key] == 1 else plural}")
        headline = (
            f"**{', '.join(parts)}** — {c['total']} finding"
            f"{'s' if c['total'] != 1 else ''} across {c['files']} file"
            f"{'s' if c['files'] != 1 else ''}."
        )

    lines = [
        f"{MARKER} — local run",
        "",
        headline,
        "",
        f"_Reviewed locally; findings were not posted inline. Run {run} on this MR._",
    ]

    lens_rows = [
        (label, c[key])
        for label, key in (
            ("Product Owner", "stage_po"),
            ("Architect", "stage_architect"),
            ("Senior Developer", "stage_senior"),
            ("Alternative implementation", "stage_maker"),
            ("Prompt Review", "stage_prompt"),
        )
        if c[key]
    ]
    if lens_rows:
        lines += [
            "",
            "<details><summary>By lens</summary>",
            "",
            "| Lens | Findings |",
            "| --- | --- |",
        ]
        lines += [f"| {label} | {n} |" for label, n in lens_rows]
        lines += ["", "</details>"]

    lines += [
        "",
        build_trailer(
            "dry-run",
            head_sha,
            comments,
            run=run,
            maker=maker,
            learn=learn,
            duration_s=duration_s,
        ),
    ]
    return "\n".join(lines)


def next_run_number(gitlab, project: str, mr_iid: int) -> int:
    """How many times this MR has been reviewed, counting this run.

    History is kept rather than collapsed — a repeat review is a real event and
    editing one note in place would erase when each happened — so the note needs
    to say which run it is.
    """
    try:
        notes = gitlab.get_mr_notes(project, mr_iid)
    except Exception as e:
        logger.debug(f"Could not count previous runs: {e}")
        return 1
    seen = 0
    for note in notes:
        if parse_trailer(note.get("body", "")):
            seen += 1
    return seen + 1


def record_local_run(
    gitlab,
    project: str,
    mr_iid: int,
    comments: Iterable,
    head_sha: str,
    maker: bool = False,
    learn: bool = False,
    duration_s: int | None = None,
) -> bool:
    """Post the local-run note when ``MR_REVIEWER_RECORD_RUNS`` is set. Never raises.

    Failing to record a run must not fail the review that already happened, so
    this warns and returns False. It is loud rather than silent because a team
    that turned recording on is relying on it: if it breaks, the person running
    it should know the run went uncounted.
    """
    if not os.environ.get("MR_REVIEWER_RECORD_RUNS"):
        return False
    try:
        run = next_run_number(gitlab, project, mr_iid)
        body = build_local_run_note(
            comments, head_sha, run=run, maker=maker, learn=learn, duration_s=duration_s
        )
        gitlab.post_note(project, mr_iid, body)
        logger.info(f"📊 Recorded this local run on the MR (run {run})")
        return True
    except Exception as e:
        logger.warning(f"Could not record this run on the MR — it will not be counted: {e}")
        return False
