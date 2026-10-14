"""
Orchestrated multi-pass review team for clone mode.

Instead of reviewing a whole MR in one mega-prompt (which front-loads attention and
silently skips the back half of large MRs), the review is run as a small team of
focused ``claude -p`` passes and merged:

  - **PO** (whole MR, 1 call) — acceptance-criteria coverage; only when Jira context exists.
  - **ARCHITECT** (whole MR, 1 call) — cross-file/system shape. Gets the full diff inline
    for small MRs, or a changed-file *map* + repo tools for large ones (never batched, so it
    keeps the breadth a per-file worker would lose).
  - **MAKER** (0 or 3 calls) — independently designs the change and diffs its design against what
    was built, which is how a *wrong root decision* gets found: it does not look like a smell, it
    looks like clean code implementing the wrong thing, and it is invisible without an alternative
    to compare against. Gated twice — a free structural gate over the added diff lines, then one
    cheap triage call — so it costs nothing on the many MRs whose work had one obvious shape. Its
    design phase runs in a worktree at the pre-change commit, so the isolation is physical rather
    than an instruction the model could ignore.
  - **SENIOR** (batched, N calls) — function-level craft + correctness bugs. Files are packed
    into batches by diff-line budget so every changed file is reviewed (deterministic coverage)
    without one worker per file.

The passes' findings are merged by the plugin's `merge_findings.py`, through
``reduce.reduce_comments`` — the same reduce step the `/review-changes` skill uses, so the quote
check, the dedup, the pattern collapse and the volume caps are one implementation with one set of
numbers rather than two that drift. How much is reported is a posture the caller chooses from who
reads the result: `mr` when posting, `high` under ``--dry-run``.

All passes run as separate ``claude -p`` subprocesses (clone mode, subscription billing),
parallelised on a thread pool — no Agent SDK, no metered API.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .gitlab_client import MRChange
from .reduce import Posture, reduce_comments
from .repo_context import create_base_tree, remove_base_tree
from .reviewer import LocalReviewer, ReviewComment

logger = logging.getLogger(__name__)

# --- SENIOR batching (code-craft lens) ------------------------------------------
# Batches are COST-based, not file-count-based. A file's cost = its diff lines + a small
# fixed per-file overhead (opening the file, reading the changed method, a grep — cheap,
# because the reviewer reads targeted regions + greps, not whole files). This lets many
# tiny-change files share one batch while genuinely large changes still split.
BATCH_BUDGET = 800  # cost units per SENIOR batch
PER_FILE_OVERHEAD = 50  # fixed cost per file, on top of its diff lines
BIG_FILE_THRESHOLD = 250  # a file with >= this many diff lines gets its own batch
HARD_FILE_CAP = 20  # backstop only — prevents a pathological mega-batch; rarely binds

# --- ARCHITECT context ----------------------------------------------------------
ARCHITECT_INLINE_LINE_BUDGET = (
    1500  # <= this: give architect the full diff inline; else a map + tools
)

# --- PROMPT review --------------------------------------------------------------
PROMPT_REVIEW_MAX_FILES = 30  # cap files sent to the single consolidated prompt-review pass

# --- BLIND DESIGN (independent design pass, emitted as stage MAKER) --------------
# The most expensive pass in the team: two serial calls, one of which runs against a separate
# pre-change tree, and it needs a story to design from. It only earns that cost where the
# implementer actually had a choice, so a free structural gate over the added diff lines (below)
# runs first and most MRs fail it. The gate decides only *whether* to spend the calls; what to
# design comes from the story's open forks, because a scope derived from the diff is the
# implementation's own decomposition and the design step could never question it.

# Added-line signals that a real design decision was made somewhere in this MR. Deliberately
# over-inclusive: this gate is free and only decides whether to run, so recall matters here and
# precision does not. Anything absent from this list (field widenings, new DTOs and enums, tests,
# renames, config) had one obvious shape and is not worth re-deriving.
_MAKER_SIGNALS: tuple[tuple[str, re.Pattern], ...] = (
    (
        "abstraction introduced",
        re.compile(r"\b(abstract\s+class|interface\s+\w+|extends\s+\w+|implements\s+\w+|ABC\b)"),
    ),
    (
        "untyped bag at a boundary",
        re.compile(
            r"(Map<String,\s*Object>|List<Document>|\bObject\s+\w+\s*[;,)=]|\bDocument\s+\w+\s*[;,)=]|:\s*any\b|Dict\[str,\s*Any\])"
        ),
    ),
    (
        "runtime type interrogation",
        re.compile(r"\b(instanceof|isinstance\(|\.class::cast|typeof\s)"),
    ),
    (
        "bulk data transform",
        re.compile(
            r"\b(mongoTemplate|updateFirst|updateOne|updateMany|bulkOps|findAll\(\)|createQuery|saveAll)\b"
        ),
    ),
    (
        "quality gate suppressed",
        re.compile(
            r"(CPD-OFF|@SuppressWarnings|eslint-disable|NOSONAR|# noqa|@Disabled|pragma:\s*no cover)"
        ),
    ),
    (
        "caching or scheduling choice",
        re.compile(r"@(Cacheable|CachePut|Scheduled|Async|Retryable|Transactional)\b"),
    ),
    (
        "new integration or service seam",
        re.compile(r"@(Service|Component|Repository|RestController|FeignClient)\b"),
    ),
    # A null or placeholder the code manufactures for its own downstream to tolerate. This is the
    # signal that catches a sentinel smuggled through the type system — the shape whose real cost is
    # paid by every leaf method that then has to null-check, several files away from the cause.
    (
        "manufactured null or sentinel",
        re.compile(
            r"(singletonList\(null\)|asList\(null\)|\.orElse\(null\)|Optional\.empty\(\)\.get|return None\s*#|\bNone\s*,\s*#)"
        ),
    ),
)

# Test sources are excluded from the gate. MAKER's remit is the choice of mechanism in production
# code — its prompt hands test quality to the SENIOR pass — so re-deriving a test file's design
# spends two calls to produce findings this pass is told not to make.
_MAKER_TEST_PATH = re.compile(
    r"(^|/)(src/test/|tests?/|__tests__/)|(Test|Spec|_test|\.spec)\.[a-z]+$"
)

# Migration-shaped paths always warrant triage — the application-loop-versus-server-side-operation
# fork is the single most consequential decision this pass exists to catch.
_MAKER_PATH_SIGNAL = re.compile(r"(migration|changelog|backfill|migrate)", re.IGNORECASE)

# --- reduce ---------------------------------------------------------------------
# There is no reduce code here any more. Dedup, the quote and file checks, pattern collapse,
# same-locus collapse, symptom absorption and every cap live in the plugin's `merge_findings.py`,
# reached through `reduce.reduce_comments` — one implementation, one set of numbers, both
# consumers. What used to be here (a per-file SENIOR cap, a global cap with its own scaling, a
# second stage-priority table) was a parallel reduce step that had never gained the quote check,
# so the consumer that *posts* was the one running the weaker filter.
MAX_CONCURRENCY = 6  # parallel claude -p passes

# Not craft-reviewable at all (deleted/rename handled separately): lock/generated/binary.
_SKIP_EXACT = {
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
    "go.sum",
    "gradle.lockfile",
}
_SKIP_SUFFIXES = (
    ".lock",
    ".min.js",
    ".map",
    ".snap",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".ico",
    ".pdf",
)

# Code files get the function-level craft lens (SENIOR). Markdown gets the prompt-review lens.
# Everything else (config, data, plain text) is not craft-reviewable and is skipped.
_CODE_SUFFIXES = (
    ".py",
    ".java",
    ".kt",
    ".kts",
    ".scala",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rb",
    ".cs",
    ".rs",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".php",
    ".swift",
    ".m",
    ".mm",
)
_PROMPT_SUFFIXES = (".md", ".markdown")


def _diff_lines(change: MRChange) -> int:
    return change.diff.count("\n")


def _classify(change) -> str:
    """Classify a changed file: 'code' | 'prompt' | 'skip'.

    - 'code'   -> function-level craft lens (SENIOR batches)
    - 'prompt' -> prompt-review lens (one consolidated pass)
    - 'skip'   -> not craft-reviewable (deleted, pure rename, lock/generated/binary, config/data)
    """
    if getattr(change, "deleted_file", False):
        return "skip"
    path = getattr(change, "new_path", "") or ""
    if not path:
        return "skip"
    low = path.lower()
    base = low.rsplit("/", 1)[-1]
    if base in _SKIP_EXACT or low.endswith(_SKIP_SUFFIXES):
        return "skip"
    if getattr(change, "renamed_file", False) and _diff_lines(change) < 5:
        return "skip"  # pure rename / move
    if low.endswith(_CODE_SUFFIXES):
        return "code"
    if low.endswith(_PROMPT_SUFFIXES):
        return "prompt"
    return "skip"


def _changes_to_diff(changes) -> str:
    """Render a list of MRChange objects into the diff format the prompt expects."""
    return "\n\n".join(f"# File: {c.new_path}\n{c.diff}" for c in changes)


def plan_senior_batches(
    changes,
    budget: int = BATCH_BUDGET,
    per_file_overhead: int = PER_FILE_OVERHEAD,
    big_file_threshold: int = BIG_FILE_THRESHOLD,
    hard_file_cap: int = HARD_FILE_CAP,
) -> list[list]:
    """Pack CODE files into cost-based SENIOR batches.

    A file's cost = diff_lines + per_file_overhead. Files (sorted by path for locality) fill a
    batch until cumulative cost exceeds `budget`; a file with a large diff (>= big_file_threshold)
    goes solo. `hard_file_cap` is a backstop against a pathological many-tiny-files batch and
    rarely binds. Only code files enter — prompts/docs/config are handled elsewhere.
    Deterministic: same input always yields the same batches (matters for the eval harness).
    """
    code = sorted((c for c in changes if _classify(c) == "code"), key=lambda c: c.new_path)
    batches: list[list] = []
    current: list = []
    current_cost = 0

    def flush():
        nonlocal current, current_cost
        if current:
            batches.append(current)
            current = []
            current_cost = 0

    for c in code:
        dl = _diff_lines(c)
        if dl >= big_file_threshold:
            flush()
            batches.append([c])
            continue
        cost = dl + per_file_overhead
        if current and (current_cost + cost > budget or len(current) >= hard_file_cap):
            flush()
        current.append(c)
        current_cost += cost
    flush()
    return batches


def _build_file_map(changes) -> str:
    """A compact changed-file map for the ARCHITECT pass on large MRs (no full diff)."""
    lines = ["## Changed Files (map — Read the ones that matter via tools)", ""]
    for c in changes:
        if getattr(c, "deleted_file", False):
            lines.append(f"- DELETED `{c.old_path}`")
            continue
        if getattr(c, "new_file", False):
            status = "NEW"
        elif getattr(c, "renamed_file", False):
            status = "RENAMED"
        else:
            status = "MODIFIED"
        added = sum(
            1 for line in c.diff.split("\n") if line.startswith("+") and not line.startswith("+++")
        )
        removed = sum(
            1 for line in c.diff.split("\n") if line.startswith("-") and not line.startswith("---")
        )
        lines.append(
            f"- {status} `{c.new_path}` (+{added}/-{removed}, {_diff_lines(c)} diff lines)"
        )
    return "\n".join(lines)


def _added_lines(change) -> str:
    """The added side of a change's diff — the only side that carries a new design decision."""
    return "\n".join(
        ln
        for ln in (change.diff or "").splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    )


def maker_candidates(changes) -> tuple[list, list[str]]:
    """Free structural gate: which code files show signs of a real design decision.

    Returns (candidate changes, signal names that fired). An empty candidate list means MAKER is
    skipped entirely and costs nothing — the common case, because widening a field, adding a DTO,
    adding tests, and renaming things all had one obvious shape.
    """
    candidates, fired = [], []
    for c in changes:
        if _classify(c) != "code":
            continue
        if _MAKER_TEST_PATH.search(getattr(c, "new_path", "") or ""):
            continue
        added = _added_lines(c)
        hits = [name for name, pattern in _MAKER_SIGNALS if pattern.search(added)]
        if _MAKER_PATH_SIGNAL.search(getattr(c, "new_path", "") or ""):
            hits.append("migration-shaped path")
        if hits:
            candidates.append(c)
            fired.extend(hits)
    return candidates, sorted(set(fired))


def run_maker(
    reviewer: LocalReviewer,
    changes,
    language: str,
    mr_title: str,
    mr_description: str,
    jira_tickets: list[str] | None,
    jira_cloud_id: str,
    base_dir: Path,
) -> list[ReviewComment]:
    """Design the change from the story, blind, then compare. Returns findings, or [].

    Two calls, serial by necessity: the design has to exist before it can be compared. They run as
    a single unit of work on the orchestrator's pool so the other passes proceed alongside them.

    The requirement comes from the story and only from the story. An earlier version scoped the
    pass with a triage step that read the diff and named the mechanisms worth designing; that made
    it affordable, but the mechanisms were the implementation's own decomposition in requirement
    language, so the design step could ask whether each one should exist while never asking whether
    the change was carved up the right way. Scoping now comes from the forks the acceptance
    criteria leave open, which is why the pass needs a story and skips itself without one.

    Only the design call runs in ``base_dir`` (the pre-change tree). The comparison needs to see
    the implementation; only the design must not.
    """
    from .prompts import get_blind_compare_prompt, get_blind_design_prompt

    if not (jira_tickets and jira_cloud_id):
        logger.info(
            "[BLIND DESIGN] no Jira story on this MR — skipping (the pass designs from the story)"
        )
        return []

    candidates, signals = maker_candidates(changes)
    if not candidates:
        logger.info("[BLIND DESIGN] no design-decision signals in the diff — skipping")
        return []
    logger.info(f"[BLIND DESIGN] gate passed on {len(candidates)} file(s): {', '.join(signals)}")

    sketch = reviewer.run_text_prompt(
        get_blind_design_prompt(
            language=language,
            jira_tickets=jira_tickets,
            jira_cloud_id=jira_cloud_id,
        ),
        cwd=str(base_dir),
        label="BLIND DESIGN/design",
    )
    if not sketch:
        return []
    logger.info(f"[BLIND DESIGN] design sketch: {len(sketch)} chars")

    comments = reviewer.review_with_prompt(
        get_blind_compare_prompt(
            language=language,
            diff_content=_changes_to_diff(candidates),
            mr_title=mr_title,
            mr_description=mr_description,
            design_sketch=sketch,
        ),
        label="BLIND DESIGN/compare",
    )
    if not comments:
        return []
    for c in comments:
        c.stage, c.kind = "MAKER", "design"
    return comments


def teach_in_comments(
    reviewer: LocalReviewer,
    comments: list[ReviewComment],
    language: str,
    mr_title: str,
) -> list[str]:
    """Append a teaching addendum to the comments that have a concept worth teaching.

    Mutates the comment bodies in place and returns the concept names that were taught, so the study
    document can be built from the same grouping the comments used.

    Teaching in the comment beats teaching in a document alone because the concept arrives on the line
    that demonstrates it, in the reader's own change, while they are looking at it. The document still
    earns its place — it groups, ranks, and points onward — but the comment is where recognition
    happens.

    Failure here is non-fatal: the findings are already correct without the teaching, so a bad parse
    leaves them untouched rather than dropping the review.
    """
    from .prompts import get_learning_comment_prompt

    if not comments:
        return []

    findings_block = "\n\n".join(
        f"**[{i}] {c.stage}/{c.kind or 'n-a'} — {c.severity} — {c.file}:{c.line}**\n"
        f"{c.title}\n\n{c.body}"
        for i, c in enumerate(comments, 1)
    )
    text = reviewer.run_text_prompt(
        get_learning_comment_prompt(language, mr_title, findings_block),
        label="LEARN/comments",
        timeout_secs=1800,
    )
    if not text:
        logger.warning("📚 Comment teaching produced nothing — posting findings unannotated")
        return []
    try:
        addenda = reviewer._parse_json_response(text)
    except Exception as e:
        logger.warning(f"📚 Comment teaching unparseable ({e}) — posting findings unannotated")
        return []
    if not isinstance(addenda, list):
        return []

    by_locus = {(c.file, c.line): c for c in comments}
    concepts: list[str] = []
    applied = 0
    for a in addenda:
        if not isinstance(a, dict):
            continue
        teaching = (a.get("teaching") or "").strip()
        if not teaching:
            continue
        cited_file, cited_line = a.get("file"), a.get("line")
        if not isinstance(cited_file, str) or cited_line is None:
            continue
        try:
            target = by_locus.get((cited_file, int(cited_line)))
        except (TypeError, ValueError):
            continue
        if target is None:
            logger.debug(f"📚 Addendum for unknown locus {a.get('file')}:{a.get('line')} — dropped")
            continue
        target.body = f"{target.body}\n\n{teaching}"
        applied += 1
        concept = (a.get("concept") or "").strip()
        if concept and concept not in concepts:
            concepts.append(concept)

    logger.info(
        f"📚 Taught {applied} of {len(comments)} finding(s) inline "
        f"across {len(concepts)} concept(s): {', '.join(concepts) or 'none named'}"
    )
    return concepts


def generate_learning_note(
    reviewer: LocalReviewer,
    comments: list[ReviewComment],
    changes,
    language: str,
    mr_title: str,
    concepts: list[str] | None = None,
) -> str | None:
    """Turn a finished review into a study document. Returns markdown, or None on failure.

    A post-processing stage rather than another pass: the findings already exist, so re-deriving them
    would double the cost, and a prompt asked to both hunt and teach reliably does neither well.

    Note what this deliberately does not do — change the review. The inline comments stay terse and
    professional, because a lead running this on someone else's MR should not turn that person's public
    review thread into a tutorial. The lesson is a separate artifact for the person learning.
    """
    from .prompts import get_learning_prompt

    if not comments:
        logger.info("📚 Learning mode: no findings to teach from")
        return None

    findings_block = "\n\n".join(
        f"**[{c.stage}/{c.kind or 'n-a'}] {c.severity} — {c.file}:{c.line}**\n{c.title}\n\n{c.body}"
        for c in comments
    )
    live = [c for c in changes if not getattr(c, "deleted_file", False)]
    prompt = get_learning_prompt(
        language=language,
        mr_title=mr_title,
        findings_block=findings_block,
        diff_content=_changes_to_diff(live),
        concepts=concepts,
    )
    note = reviewer.run_text_prompt(prompt, label="LEARN", timeout_secs=1800)
    if note:
        logger.info(f"📚 Learning note: {len(note)} chars from {len(comments)} finding(s)")
    return note


def _run_pass(reviewer: LocalReviewer, kwargs: dict) -> list[ReviewComment] | None:
    return reviewer.review_diff(**kwargs)


def orchestrate_review(
    reviewer: LocalReviewer,
    changes,
    language: str,
    mr_title: str,
    mr_description: str = "",
    jira_tickets: list[str] | None = None,
    jira_cloud_id: str = "",
    has_markdown_files: bool = False,
    max_concurrency: int = MAX_CONCURRENCY,
    target_branch: str = "",
    posture: Posture = "mr",
) -> list[ReviewComment] | None:
    """Run the review as a team of focused passes and return merged comments.

    Returns the merged/deduped comment list, or ``None`` if every pass failed (so the caller
    can treat it as a review failure rather than "no issues").

    ``posture`` decides how much of what was found is reported, and only that — every drop rule
    in the reduce step runs identically at every tier, so a finding the posture suppresses was
    still true. The caller picks it from who will read the result (`reduce.posture_for`), never
    from how expensive the run was: making the agents quieter would lose subtle findings
    irrecoverably, while making the *reduce step* quieter leaves every suppressed finding in the
    merged output with the cap that removed it.
    """
    jira_tickets = jira_tickets or []
    live_changes = [c for c in changes if not getattr(c, "deleted_file", False)]
    prompt_files = [c for c in changes if _classify(c) == "prompt"]
    full_diff = _changes_to_diff(live_changes)
    total_lines = full_diff.count("\n")

    tasks: list[tuple[str, dict]] = []

    # PO — acceptance-criteria coverage, only when Jira context is available.
    # Code/PO/Architect passes review code; prompt-review is its own pass, so has_markdown_files=False here.
    po_enabled = bool(jira_tickets and jira_cloud_id and reviewer.jira_enabled)
    if po_enabled:
        tasks.append(
            (
                "PO",
                {
                    "diff_content": full_diff,
                    "language": language,
                    "mr_title": mr_title,
                    "mr_description": mr_description,
                    "jira_tickets": jira_tickets,
                    "jira_cloud_id": jira_cloud_id,
                    "has_markdown_files": False,
                    "focus_stage": "PO",
                    "focus_note": "You see the whole MR diff below. Read files via tools as needed.",
                },
            )
        )

    # ARCHITECT — whole-MR, cross-file. Full diff inline when small; a map + tools when large.
    if total_lines <= ARCHITECT_INLINE_LINE_BUDGET:
        arch_diff = full_diff
        arch_note = "You see the full MR diff below. Review cross-file/system shape; Read files via tools as needed."
    else:
        arch_diff = _build_file_map(changes)
        arch_note = (
            "This is a LARGE MR — below is a MAP of changed files, NOT the full diff. Use Read/Glob/Grep "
            "to open the files that matter for system/class-shape review. Focus on cross-file concerns: "
            "duplication, layering, domain modelling, and whether the change's abstraction is right."
        )
    tasks.append(
        (
            "ARCHITECT",
            {
                "diff_content": arch_diff,
                "language": language,
                "mr_title": mr_title,
                "mr_description": mr_description,
                "has_markdown_files": False,
                "focus_stage": "ARCHITECT",
                "focus_note": arch_note,
            },
        )
    )

    # SENIOR — cost-based batches over CODE files (craft + bugs); every code file is reviewed.
    batches = plan_senior_batches(changes)
    for i, batch in enumerate(batches):
        files = ", ".join(c.new_path for c in batch)
        note = (
            f"This is SENIOR batch {i + 1}/{len(batches)}. Review ONLY these changed files for "
            f"function-level craft and correctness bugs. For each, read the changed method/class and "
            f"grep for related definitions/usages/duplication — don't read whole large files: {files}"
        )
        tasks.append(
            (
                f"SENIOR[{i + 1}/{len(batches)}]",
                {
                    "diff_content": _changes_to_diff(batch),
                    "language": language,
                    "mr_title": mr_title,
                    "mr_description": mr_description,
                    "has_markdown_files": False,
                    "focus_stage": "SENIOR",
                    "focus_note": note,
                },
            )
        )

    # PROMPT — one consolidated pass over changed LLM prompt/markdown files (prompt quality,
    # NOT code craft). Keeps prompts from inflating the SENIOR batch count.
    if prompt_files:
        pf = prompt_files[:PROMPT_REVIEW_MAX_FILES]
        files = ", ".join(c.new_path for c in pf)
        tasks.append(
            (
                "PROMPT",
                {
                    "diff_content": _changes_to_diff(pf),
                    "language": language,
                    "mr_title": mr_title,
                    "mr_description": mr_description,
                    "has_markdown_files": True,
                    "focus_stage": "PROMPT",
                    "focus_note": (
                        "These are the changed markdown/LLM-prompt files. The LLM decides per file whether each "
                        "is actually a prompt; for real prompts, review PROMPT QUALITY (clear instructions, good "
                        "examples, structure, consistency across regions) using the prompt-review guidance — NOT "
                        f"code craft. Read each file via tools. Skip pure docs that aren't prompts. Files: {files}"
                    ),
                },
            )
        )

    # MAKER — independent design, then compare. Runs only when the free structural gate finds a
    # design decision worth re-deriving AND a pre-change checkout can be created; either miss simply
    # drops the pass rather than failing the review.
    maker_base_dir = None
    gate_candidates, _ = maker_candidates(changes)
    if gate_candidates and target_branch:
        maker_base_dir = create_base_tree(Path(reviewer.repo_dir), target_branch)
    elif gate_candidates:
        logger.info(
            f"[MAKER] {len(gate_candidates)} file(s) show design-decision signals, but the pass is "
            "off — enable it with --enable-maker or MR_REVIEWER_MAKER=1"
        )

    logger.info(
        f"🤝 Orchestrated review: {len(tasks)} focused pass(es) — "
        f"PO={'yes' if po_enabled else 'no'}, MAKER={'yes' if maker_base_dir else 'no'}, "
        f"ARCHITECT=1, SENIOR batches={len(batches)}, PROMPT={'1' if prompt_files else '0'} "
        f"({len(live_changes)} files, {total_lines} diff lines)"
    )

    results: list[ReviewComment] = []
    any_success = False
    workers = max(1, min(max_concurrency, len(tasks) + (1 if maker_base_dir else 0)))
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures: dict[Future[Any], str] = {}
            if maker_base_dir:
                # Submitted FIRST, deliberately. It is one unit of work but two serial calls, so it
                # is the longest chain in the team and therefore the critical path; queueing it behind
                # the single-call passes would add its whole duration to the review's wall clock.
                futures[
                    ex.submit(
                        run_maker,
                        reviewer,
                        changes,
                        language,
                        mr_title,
                        mr_description,
                        jira_tickets,
                        jira_cloud_id,
                        maker_base_dir,
                    )
                ] = "MAKER"
            futures.update({ex.submit(_run_pass, reviewer, kw): label for label, kw in tasks})
            for fut in as_completed(futures):
                label = futures[fut]
                try:
                    cs = fut.result()
                except Exception as e:
                    logger.warning(f"[{label}] pass failed: {e}")
                    continue
                if cs is None:
                    logger.warning(f"[{label}] pass returned no parseable output")
                    continue
                any_success = True
                logger.info(f"[{label}] {len(cs)} finding(s)")
                results.extend(cs)
    finally:
        if maker_base_dir:
            remove_base_tree(Path(reviewer.repo_dir))

    if not any_success:
        return None
    # There is one merge path. `brutal` used to arrive twice — once as this function's
    # `learning_mode` flag and once as the posture — and the two produced identical findings,
    # because `reduce_candidates` already reads `brutal` as "preserve everything". A flag argument
    # with nothing behind it is two call sites to keep in step for no behavioural difference.
    #
    # What the caller does with the result still differs: learning mode asks for `brutal` so the
    # study document gets a pattern's second and third instance, then bounds what reaches the
    # thread with the caps alone (`reduce.cap_comments`). The document wants every instance
    # because recurrence is the lesson; the thread wants a normal-sized review.
    merged = reduce_comments(
        results, reviewer.repo_dir, diff_text=full_diff, posture=posture, units=len(live_changes)
    )
    logger.info(f"🤝 Orchestrated review merged to {len(merged)} comment(s) at posture {posture}")
    return merged
