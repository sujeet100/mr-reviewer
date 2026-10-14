"""The CLI's bridge to the one reduce step, which lives in the plugin's `merge_findings.py`.

The plugin owns the reducer for the same reason it owns the charter: a plugin install copies only
the plugin subtree, so a skill cannot read anything above its own root, while this package can
reach down into the plugin beside it. One copy, two consumers — and until this module existed the
consumer that *posts* was the one running the weaker reduce step. The CLI had its own chain
(`_apply_stage_caps` at parse time, then `dedupe_and_reduce`, then `apply_global_cap`) which had
no quote check, no file check and no pattern collapse, so "the same methodology" produced
materially different output depending on which program ran it.

What the CLI brings that the skill does not is that its passes return *parsed* comments rather
than raw agent text — the parse happens inside `reviewer.LocalReviewer`, per call — so this
module converts `ReviewComment` objects into the reducer's candidate dicts and back. That is
lossy in one direction only: the reducer may lengthen a body (a collapsed pattern lists its other
locations, a design root lists its symptoms), and those edits are read back onto the comments.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Literal

from .prompts import plugin_subdir
from .reviewer import ReviewComment

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).parent

# The reporting tiers, spelled once on this side of the boundary. The reducer owns the numbers in
# `POSTURE_CAPS`; this names the closed set so a misspelling is a type error at the call site
# rather than a `ValueError` raised after the whole review has run.
Posture = Literal["mr", "high", "brutal"]


@lru_cache(maxsize=1)
def scripts_dir() -> Path:
    """Locate the plugin's scripts, through the same chain that locates the charter."""
    return plugin_subdir(
        "scripts", "merge_findings.py", _PACKAGE_DIR / "scripts", "MR_REVIEWER_SCRIPTS"
    )


@lru_cache(maxsize=1)
def reducer() -> ModuleType:
    """The `merge_findings` module, loaded from the plugin by path.

    Loaded by path rather than imported, because the plugin is not a package and must not become
    one: its files are copied into a plugin cache directory that is nowhere on `sys.path`, and the
    skill runs them as scripts. Cached, so the load happens once per process.
    """
    path = scripts_dir() / "merge_findings.py"
    spec = importlib.util.spec_from_file_location("mr_reviewer._merge_findings", path)
    if spec is None or spec.loader is None:
        # scripts_dir() already proved the file exists, so this is a broken or unreadable file
        # rather than a missing one — and an installed CLI with no reducer has no reduce step at
        # all, which must fail loudly here instead of surfacing as an empty review later.
        raise ImportError(f"cannot load the reducer from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def report_only_words() -> int:
    """The reducer's own de-listing threshold, for anything that has to name it to a human."""
    words: int = reducer().REPORT_ONLY_WORDS
    return words


# What the CLI's stages are called in the reducer's priority table. The CLI emits `SENIOR` for its
# craft pass where the skill emits `FUNCTION`; both are in `_STAGE_PRIORITY` at the same rank and
# both are treated as craft by the per-file cap, so no translation is needed. This map exists
# only to state that the overlap is deliberate rather than accidental.
CRAFT_STAGES = ("SENIOR", "FUNCTION")


def _to_candidate(c: ReviewComment) -> dict:
    """A `ReviewComment` as the reducer's raw input.

    `kind` is defaulted the way the reducer would default it, rather than left empty: the CLI's
    parser leaves `kind` blank when a pass omits it, and an empty string is not one of the
    reducer's `KINDS`, so it would silently become `bug` and lose the craft protections.
    """
    kind = c.kind or ("craft" if c.stage in CRAFT_STAGES else "bug")
    return {
        "file": c.file,
        "line": c.line,
        "severity": c.severity,
        "title": c.title,
        "body": c.body,
        "stage": c.stage,
        "kind": kind,
    }


def _to_comment(f: dict) -> ReviewComment:
    """A reduced finding as a `ReviewComment`, carrying what the reducer added to it.

    `writing` and `demoted_from` cross back deliberately. They are the only record of *why* a
    finding wears a badge one rank below the one its pass gave it, and dropping them here left the
    posted comment looking like an unsure reviewer rather than a wordy one.
    """
    return ReviewComment(
        file=f["file"],
        line=f["line"],
        severity=f["severity"],
        title=f["title"],
        body=f["body"],
        stage=f["stage"],
        kind=f["kind"],
        inline=f.get("inline", True),
        writing=f.get("writing", ""),
        demoted_from=f.get("demoted_from", ""),
    )


def posture_for(dry_run: bool, brutal: bool = False) -> Posture:
    """Which posture this run reports at.

    An explicit `--brutal` wins over everything, as it must: it is a request for everything
    meaningful. Otherwise a dry run reports at `high` and a posting run at `mr` — the split is by
    who reads the result, not by how the review was produced. A dry run is read by the person who
    asked for it and nobody else, so the etiquette that keeps a shared thread short does not apply;
    a posted comment costs the author's attention whether or not they agree with it.
    """
    if brutal:
        return "brutal"
    return "high" if dry_run else "mr"


def live_file_count(mr) -> int:
    """Changed files that still exist, which is what the CLI scales its ceiling by.

    A rougher proxy than the skill's count of changed *functions* — the mapper is not wired into
    this consumer yet — and it was written out at four call sites, so tuning it meant finding them
    all and a missed one silently scaled the ceiling from the wrong number.
    """
    return len([c for c in mr.changes if not getattr(c, "deleted_file", False)])


def reduce_for_mr(
    comments: list[ReviewComment],
    mr,
    repo_dir: str | Path | None,
    *,
    dry_run: bool,
    diff_text: str = "",
    preserve_all: bool = False,
) -> list[ReviewComment]:
    """Reduce one MR's findings, deciding the posture and the unit count from the run itself.

    The entry point every CLI path should use. Its callers each used to assemble both arguments —
    `posture_for(dry_run, brutal_mode())` at three sites and the live-file count at four, with the
    same five-line comment copied twice — which made a policy change a hunt rather than an edit.
    """
    from .prompts import brutal_mode

    return reduce_comments(
        comments,
        repo_dir,
        posture=posture_for(dry_run, brutal_mode()),
        units=live_file_count(mr),
        diff_text=diff_text,
        preserve_all=preserve_all,
    )


def cap_comments(comments: list[ReviewComment], mr, *, dry_run: bool) -> list[ReviewComment]:
    """Apply only the posture's caps to findings that have already been reduced once.

    Learning mode needs this. It asks the orchestrator for the uncapped set so the study document
    can show a pattern's second and third instance, then bounds what reaches the MR thread — and
    running the *whole* pipeline again to do that had three consequences, all invisible:

    - it re-validated, without the diff the first run had, so a finding whose only quote was a
      deleted line cleared the evidence bar once and was dropped the second time;
    - it re-ran the writing bar over bodies `teach_in_comments` had just lengthened by 60 to 120
      words, so a finding was demoted a severity rank for the teaching rather than its own prose;
    - it re-collapsed and re-absorbed findings that had already been collapsed and absorbed.

    Capping is the only part of the reduce step that depends on posture, so it is the only part
    that should run twice.
    """
    from .prompts import brutal_mode

    if not comments:
        return comments
    posture, units = posture_for(dry_run, brutal_mode()), live_file_count(mr)
    candidates = [dict(_to_candidate(c), _index=i) for i, c in enumerate(comments)]
    kept, dropped = reducer().apply_caps(candidates, posture, units)
    surviving = {c["_index"] for c in kept}
    logger.info(f"Cap [{posture}]: {len(comments)} finding(s) -> {len(surviving)} for the thread")
    for candidate, reason in dropped:
        logger.info(f"  capped {candidate['file']}:{candidate['line']} — {reason}")
    return [c for i, c in enumerate(comments) if i in surviving]


def reduce_comments(
    comments: list[ReviewComment],
    repo_dir: str | Path | None,
    *,
    posture: Posture,
    units: int,
    diff_text: str = "",
    preserve_all: bool = False,
) -> list[ReviewComment]:
    """Run the shared reduce step over the CLI's findings and return what survives.

    ``repo_dir`` is None in the deprecated diff mode, which has no checkout; the reducer then
    checks quotes against ``diff_text`` alone rather than dropping everything for a file it cannot
    open. ``units`` is the CLI's count of changed files, which is a rougher proxy than the skill's
    count of changed functions — the mapper is not yet wired into this consumer — and it scales
    only the global ceiling.

    ``posture`` and ``units`` are required and keyword-only. They were defaulted, and to
    *different* values at different layers — `mr` here, `high` in the reducer — so one policy
    had two answers depending on the layer a caller entered at. `posture_for` is now the only
    thing that decides, and a caller that forgets says so at the call site rather than quietly
    picking a tier.

    The mapper's plan, which would scope symptom absorption to the enclosing function rather than
    the whole file, is not a parameter yet: nothing can fill it until the CLI runs the mapper, and
    a parameter no caller passes is one more thing to keep working with no way to tell if it does.
    """
    if not comments:
        return comments
    mf = reducer()
    result = mf.reduce_candidates(
        [(c.stage, _to_candidate(c)) for c in comments],
        Path(repo_dir) if repo_dir else None,
        diff_text=diff_text,
        posture=posture,
        units=units,
        preserve_all=preserve_all,
    )
    kept = [_to_comment(f) for f in result["kept"]]
    counts = result["counts"]
    logger.info(
        f"Reduce [{posture}]: {len(comments)} candidate(s) -> {counts['kept']} kept "
        f"({counts['dropped']} dropped, {counts['collapsed']} pattern(s) collapsed, "
        f"{counts['absorbed']} symptom(s) absorbed, ceiling {result['global_cap']} "
        f"for {units} unit(s))"
    )
    for entry in result["dropped"]:
        f = entry["finding"]
        where = f"{f.get('file')}:{f.get('line')}" if isinstance(f, dict) else "?"
        logger.info(f"  dropped {where} — {entry['reason']}")
    for f in result["kept"]:
        if f.get("writing"):
            logger.info(f"  writing bar {f['file']}:{f['line']} — {f['writing']}")
    if counts["demoted"] or counts["report_only"]:
        logger.info(
            f"  writing bar: {counts['demoted']} demoted one rank, {counts['report_only']} kept "
            "in the report but not posted inline"
        )
    return kept
