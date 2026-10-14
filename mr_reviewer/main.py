"""
CLI entry point for MR Reviewer.
"""

import argparse
import logging

# Configure logging
import os
import re
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import review_cache, telemetry
from .config import jira_project_keys, load_config, validate_config
from .gitlab_client import CommentPosition, GitLabClient
from .prompts import brutal_mode
from .reduce import cap_comments, posture_for, reduce_for_mr, report_only_words
from .related_files import get_related_files_for_review
from .repo_context import cleanup_repo, clone_or_update_repo, remove_base_tree
from .reviewer import DEFAULT_MODEL, SEVERITY_RANK, LocalReviewer, ReviewComment, Reviewer

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _parse_projects_arg(values: list[str] | None) -> list[str]:
    """Flatten argparse --projects (supports repeated flag and comma-separated values)."""
    if not values:
        return []
    projects = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                projects.append(part)
    return projects


def extract_jira_tickets(mr_title: str, mr_description: str = "") -> list[str]:
    """
    Extract Jira ticket IDs from MR title and description.

    Matches patterns like PROJ-1234, ABC-567, XY-89, etc. When ``JIRA_PROJECT_KEYS`` (or
    ``jira.project_keys``) is set, only those keys match, so ``SHA-256`` or ``UTF-8`` in a
    description is not mistaken for a story.

    Returns:
        Deduplicated list of ticket IDs, title matches first.
    """
    keys = jira_project_keys()
    prefix = "(?:" + "|".join(map(re.escape, keys)) + ")" if keys else "[A-Z][A-Z0-9]+"
    pattern = rf"\b({prefix}-\d+)\b"
    seen = set()
    tickets = []
    for text in [mr_title, mr_description or ""]:
        for match in re.findall(pattern, text):
            if match not in seen:
                seen.add(match)
                tickets.append(match)
    return tickets


def _extract_diff_new_lines(diff: str) -> set[int]:
    """Extract valid new-side line numbers from a unified diff.

    GitLab only allows inline comments on lines that appear in the diff.
    This parses hunk headers and counts context/addition lines to build
    the set of valid new_line values.
    """
    valid_lines = set()
    new_line_num = 0

    for line in diff.split("\n"):
        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk_match:
            new_line_num = int(hunk_match.group(1))
            continue

        if new_line_num == 0:
            continue

        if line.startswith("+"):
            valid_lines.add(new_line_num)
            new_line_num += 1
        elif line.startswith("-"):
            # Deletion — doesn't increment new-side line number
            pass
        else:
            # Context line (or empty)
            valid_lines.add(new_line_num)
            new_line_num += 1

    return valid_lines


def _snap_to_nearest_diff_line(target: int, valid_lines: set[int]) -> int | None:
    """Find the closest valid diff line to the target, or None if no valid lines."""
    if not valid_lines:
        return None
    return min(valid_lines, key=lambda candidate: abs(candidate - target))


PROMPT_GUIDE_REPO_PATH = "docs/prompt-engineering.md"


def has_markdown_files(changes: list) -> bool:
    """Return True if any changed file is a markdown file.

    Markdown files MAY be LLM prompts. The reviewer LLM decides per-file whether
    each `.md` change is actually a prompt or just documentation, and applies the
    prompt-review guidance only to real prompts.
    """
    for change in changes:
        path = (change.new_path or "").lower()
        if path.endswith(".md") or path.endswith(".markdown"):
            return True
    return False


def detect_language_from_dir(repo_dir, scope: str = "") -> tuple[str, dict[str, int]]:
    """
    Detect the dominant language by walking a checked-out repo directory.

    Args:
        repo_dir: Path (or string) to the repo root
        scope: Optional subtree (relative to repo_dir) to restrict the walk

    Returns:
        Tuple of (dominant_language, counts_dict). counts_dict maps language
        to file count so callers can show the breakdown in the report header.
    """
    from pathlib import Path

    root = Path(repo_dir) / scope if scope else Path(repo_dir)
    counts = {"java": 0, "python": 0, "react": 0, "angular": 0, "_ts_plain": 0}

    # Directories we never want to traverse (vendored / build output / VCS)
    skip_dirs = {
        ".git",
        "node_modules",
        "build",
        "dist",
        "target",
        ".gradle",
        ".venv",
        "venv",
        "__pycache__",
        ".next",
        ".angular",
    }

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        name = path.name.lower()
        if name.endswith(".java"):
            counts["java"] += 1
        elif name.endswith(".py"):
            counts["python"] += 1
        elif name.endswith(".tsx") or name.endswith(".jsx"):
            counts["react"] += 1
        elif name.endswith(".ts"):
            if ".component.ts" in name or ".service.ts" in name or ".module.ts" in name:
                counts["angular"] += 1
            else:
                counts["_ts_plain"] += 1

    primary_counts = {k: v for k, v in counts.items() if not k.startswith("_")}
    dominant = max(primary_counts, key=lambda name: primary_counts[name])
    if primary_counts[dominant] > 0:
        return dominant, primary_counts

    if counts["_ts_plain"] > 0:
        return "angular", primary_counts

    return "generic", primary_counts


def detect_language(changes: list) -> str:
    """
    Detect the primary language from file changes.

    Args:
        changes: List of MRChange objects

    Returns:
        Detected language string
    """
    java_count = 0
    python_count = 0
    react_count = 0
    angular_count = 0
    ts_count = 0  # plain .ts without Angular markers

    for change in changes:
        path = change.new_path.lower()
        if path.endswith(".java"):
            java_count += 1
        elif path.endswith(".py"):
            python_count += 1
        elif path.endswith(".tsx") or path.endswith(".jsx"):
            react_count += 1
        elif path.endswith(".ts"):
            if ".component.ts" in path or ".service.ts" in path or ".module.ts" in path:
                angular_count += 1
            else:
                ts_count += 1

    # Pick the dominant language among the unambiguous categories.
    counts = {
        "java": java_count,
        "python": python_count,
        "react": react_count,
        "angular": angular_count,
    }
    dominant = max(counts, key=lambda name: counts[name])
    if counts[dominant] > 0:
        return dominant

    # Plain .ts without Angular markers — default to angular (workspace convention)
    if ts_count > 0:
        return "angular"

    return "generic"


def build_diff_content(changes: list, max_lines: int = 3000) -> str:
    """
    Build combined diff content from changes.

    Args:
        changes: List of MRChange objects
        max_lines: Maximum lines to include (0 = unlimited)

    Returns:
        Combined diff string
    """
    diffs = []
    total_lines = 0

    for change in changes:
        if change.deleted_file:
            continue  # Skip deleted files

        diff_lines = change.diff.count("\n")
        if max_lines > 0 and total_lines + diff_lines > max_lines:
            logger.warning(f"Diff truncated at {total_lines} lines (max: {max_lines})")
            break

        diffs.append(f"# File: {change.new_path}\n{change.diff}")
        total_lines += diff_lines

    return "\n\n".join(diffs)


def build_diff_summary(changes: list) -> str:
    """
    Build a summary of changes for very large MRs (50+ files).

    Lists changed files with line counts so Claude can decide what to Read.

    Args:
        changes: List of MRChange objects

    Returns:
        Summary string with file list and stats
    """
    lines = ["## Changed Files Summary\n"]
    lines.append(f"Total files changed: {len(changes)}\n")

    for change in changes:
        if change.deleted_file:
            lines.append(f"- **DELETED** `{change.old_path}`")
            continue
        if change.new_file:
            status = "NEW"
        elif change.renamed_file:
            status = f"RENAMED from `{change.old_path}`"
        else:
            status = "MODIFIED"

        diff_lines = change.diff.count("\n")
        added = sum(
            1
            for line in change.diff.split("\n")
            if line.startswith("+") and not line.startswith("+++")
        )
        removed = sum(
            1
            for line in change.diff.split("\n")
            if line.startswith("-") and not line.startswith("---")
        )
        lines.append(
            f"- **{status}** `{change.new_path}` (+{added}/-{removed}, {diff_lines} diff lines)"
        )

    return "\n".join(lines)


def review_mr(
    project: str,
    mr_iid: int,
    gitlab_url: str,
    gitlab_token: str,
    anthropic_key: str | None = None,
    model: str = DEFAULT_MODEL,
    effort: str = "",
    language: str | None = None,
    dry_run: bool = False,
    max_diff_lines: int = 3000,
    use_cli: bool = False,
    include_file_context: bool = True,
    max_context_lines: int = 500,
    fetch_related_files: bool = True,
    use_local_clone: bool = False,
    jira_cloud_id: str = "",
    jira_enabled: bool = False,
    default_group: str = "",
    enable_verification: bool = False,
    post_cached: bool = False,
) -> int:
    """
    Review a merge request and post comments.

    Args:
        project: GitLab project path
        mr_iid: Merge request IID
        gitlab_url: GitLab instance URL
        gitlab_token: GitLab API token
        anthropic_key: Anthropic API key (not needed if use_cli=True)
        model: Claude model to use
        language: Force specific language (auto-detect if None)
        dry_run: If True, don't post comments
        max_diff_lines: Maximum diff lines to process
        use_cli: If True, use Claude Code CLI instead of API
        include_file_context: If True, fetch full file content for context
        max_context_lines: Maximum lines per file for context
        fetch_related_files: If True, fetch related domain entities for context
        use_local_clone: If True, clone repo and let Claude browse files directly
        jira_cloud_id: Atlassian cloud ID for Jira MCP integration
        jira_enabled: If True, fetch Jira story context via MCP during review
        post_cached: If True, post the review cached by a previous --dry-run
            instead of running a new review

    Returns:
        Exit code (0 for success)
    """
    # Initialize GitLab client
    gitlab = GitLabClient(gitlab_url, gitlab_token, default_group=default_group)

    # Resolve short project paths (e.g. "team/foo" → "my-group/team/foo")
    resolved = gitlab.resolve_project(project)
    if resolved != project:
        logger.info(f"Resolved project path '{project}' → '{resolved}'")
        project = resolved

    # Fetch MR details
    logger.info(f"Fetching MR !{mr_iid} from {project}...")
    mr = gitlab.get_mr_changes(project, mr_iid)

    logger.info(f"📋 MR !{mr_iid}: {mr.title}")
    logger.info(f"👤 Author: {mr.author} | 📁 Files: {len(mr.changes)}")
    logger.info(f"🔗 {mr.web_url}")

    if not mr.changes:
        logger.info("No changes to review")
        return 0

    # --- Replay a cached dry run: no clone, no LLM, posts exactly what was shown ---
    if post_cached:
        return _post_cached_review(gitlab, project, mr_iid, mr)

    # For merged MRs, source branch may be deleted; use target branch for file access
    context_branch = mr.source_branch
    if mr.state == "merged":
        context_branch = mr.target_branch
        logger.info(f"MR is merged — using target branch '{context_branch}' for file access")

    # Detect language if not specified
    if not language:
        language = detect_language(mr.changes)
        logger.info(f"Detected language: {language}")

    # Detect prompt files (filename contains 'prompt') — adds prompt-review guidance
    markdown_files_present = has_markdown_files(mr.changes)
    if markdown_files_present:
        logger.info(
            "📝 Markdown file(s) in diff — appending prompt-review guidance (LLM decides which are prompts)"
        )

    # Extract Jira tickets from MR title/description
    jira_tickets = []
    if jira_enabled and jira_cloud_id:
        jira_tickets = extract_jira_tickets(mr.title, mr.description or "")
        if jira_tickets:
            logger.info(f"🎫 Found Jira ticket(s): {', '.join(jira_tickets)}")

    # --- Local clone mode: clone repo, send full diff, let Claude browse files ---
    if use_local_clone:
        result_code, _ = _review_with_local_clone(
            gitlab=gitlab,
            mr=mr,
            project=project,
            gitlab_url=gitlab_url,
            gitlab_token=gitlab_token,
            model=model,
            effort=effort,
            language=language,
            dry_run=dry_run,
            context_branch=context_branch,
            jira_tickets=jira_tickets,
            jira_cloud_id=jira_cloud_id,
            jira_enabled=jira_enabled,
            has_markdown_files=markdown_files_present,
            enable_verification=enable_verification,
        )
        return result_code

    # --- Standard mode: embed context in prompt ---
    # Build diff content
    diff_content = build_diff_content(mr.changes, max_diff_lines)

    if not diff_content.strip():
        logger.info("No reviewable diff content")
        return 0

    # Initialize reviewer
    reviewer = Reviewer(
        api_key=anthropic_key,
        model=model,
        use_cli=use_cli,
        enable_verification=enable_verification,
        effort=effort,
    )

    # Fetch file context for better review quality
    # Two-pass approach for CLI mode: ask what's needed first, then fetch only that
    files_context = None
    if include_file_context:
        if use_cli:
            # Two-pass approach: Ask Claude what context it needs
            logger.info("Using two-pass approach to minimize context...")
            try:
                context_needs = reviewer.request_context_needs(
                    diff_content=diff_content,
                    mr_title=mr.title,
                    mr_description=mr.description or "",
                )
                # Extract file paths from the response
                if not isinstance(context_needs, dict):
                    context_needs = {"files_needed": [], "can_review_without_context": True}

                files_needed = context_needs.get("files_needed", [])

                requested_files = []
                for f in files_needed:
                    if isinstance(f, dict) and "path" in f:
                        requested_files.append(f["path"])
                    elif isinstance(f, str):
                        requested_files.append(f)

                if requested_files:
                    logger.info(f"Claude requested {len(requested_files)} file(s) for context")
                    for f in context_needs.get("files_needed", []):
                        logger.info(f"  - {f['path']}: {f.get('reason', 'no reason given')}")

                    # Fetch only the requested files
                    files_context = gitlab.get_files_context(
                        project=project,
                        file_paths=requested_files,
                        ref=context_branch,
                        max_lines_per_file=max_context_lines,
                    )
                    logger.info(f"Retrieved context for {len(files_context)} file(s)")
                else:
                    logger.info("Claude indicated no additional context needed")

            except Exception as e:
                logger.warning(
                    f"Two-pass context request failed, falling back to diff-only review: {e}"
                )
        else:
            # API mode: fetch all changed files upfront (original behavior)
            file_paths = [
                change.new_path
                for change in mr.changes
                if not change.deleted_file and not change.new_file
            ]
            if file_paths:
                logger.info(f"Fetching file context for {len(file_paths)} file(s)...")
                try:
                    files_context = gitlab.get_files_context(
                        project=project,
                        file_paths=file_paths,
                        ref=context_branch,
                        max_lines_per_file=max_context_lines,
                    )
                    logger.info(f"Retrieved context for {len(files_context)} file(s)")
                except Exception as e:
                    logger.warning(f"Could not fetch file context: {e}")

    # Fetch related files (domain entities, interfaces) for better context
    related_files = None
    if fetch_related_files and files_context and language == "java":
        logger.info("Fetching related domain files...")
        try:
            related_files = get_related_files_for_review(
                gitlab=gitlab,
                project=project,
                branch=context_branch,
                changed_files_content=files_context,
            )
            if related_files:
                logger.info(f"Found {len(related_files)} related file(s) for context")
        except Exception as e:
            logger.warning(f"Could not fetch related files: {e}")

    # Fetch the prompt-engineering guide from the repo if any prompt files are in the MR
    prompt_guide_content = None
    if markdown_files_present:
        try:
            guide_map = gitlab.get_files_context(
                project=project,
                file_paths=[PROMPT_GUIDE_REPO_PATH],
                ref=context_branch,
                max_lines_per_file=0,
            )
            prompt_guide_content = guide_map.get(PROMPT_GUIDE_REPO_PATH)
            if prompt_guide_content:
                logger.info(f"Loaded prompt-engineering guide from {PROMPT_GUIDE_REPO_PATH}")
            else:
                logger.info(
                    f"No prompt-engineering guide at {PROMPT_GUIDE_REPO_PATH}; using generic guidance"
                )
        except Exception as e:
            logger.warning(f"Could not fetch prompt-engineering guide: {e}")

    # Perform review
    comments = reviewer.review_diff(
        diff_content=diff_content,
        language=language,
        mr_title=mr.title,
        mr_description=mr.description or "",
        files_context=files_context,
        related_files=related_files,
        jira_tickets=jira_tickets,
        jira_cloud_id=jira_cloud_id,
        has_markdown_files=markdown_files_present,
        prompt_guide_content=prompt_guide_content,
    )

    if comments is None:
        logger.error("Review failed - not posting any comments to MR")
        return 1

    comments = _reduce_diff_mode(comments, mr, diff_content, dry_run, files_context, related_files)

    if len(comments) == 0:
        logger.info("No issues found in review")
        if dry_run:
            # A clean local run is still a run, and "reviewed, found nothing" is
            # the single most useful thing to know about an unreviewed-looking MR.
            telemetry.record_local_run(
                gitlab, project, mr_iid, [], mr.diff_refs.get("head_sha", "")
            )
        else:
            gitlab.post_note(
                project,
                mr_iid,
                "## 🤖 Automated Code Review\n\n✅ No significant issues found. The code looks good!",
            )
        return 0

    # Sort comments by severity
    comments.sort(key=lambda c: SEVERITY_RANK.get(c.severity, 3))

    # Post comments
    if dry_run:
        logger.info("=== DRY RUN - Comments that would be posted ===")
        for comment in comments:
            note = "" if comment.inline else "  (report only — too long to post on a line)"
            print(f"\n[{comment.file}:{comment.line}]{note}")
            print(comment.to_markdown())
        print("\n=== End of dry run ===")
        telemetry.record_local_run(
            gitlab, project, mr_iid, comments, mr.diff_refs.get("head_sha", "")
        )
    else:
        # One posting path for every mode. This branch used to carry its own copy of the inline
        # loop — the third in this module — and only the shared one learned about the writing
        # bar's `inline` flag, so the same over-long finding was folded into the summary note by
        # `review-all` and posted on a line of the diff by `review --no-local-clone`.
        _post_review_comments(gitlab, project, mr, comments, reviewer)

    return 0


def _review_with_local_clone(
    gitlab,
    mr,
    project: str,
    gitlab_url: str,
    gitlab_token: str,
    model: str,
    effort: str,
    language: str,
    dry_run: bool,
    context_branch: str | None = None,
    jira_tickets: list[str] | None = None,
    jira_cloud_id: str = "",
    jira_enabled: bool = False,
    has_markdown_files: bool = False,
    enable_verification: bool = False,
) -> tuple[int, int]:
    """
    Review an MR by cloning the repo and letting Claude browse files.

    This gives Claude the same file access as a local terminal review.
    The diff is sent as a map of what changed; Claude reads full files itself.

    Returns:
        Tuple of (exit_code, comment_count)
    """
    repo_dir = None
    try:
        # Clone/update the repo at MR source branch (or target if merged)
        branch = context_branch or mr.source_branch
        logger.info(f"Cloning repo for local review (branch: {branch})...")
        repo_dir = clone_or_update_repo(
            gitlab_url=gitlab_url,
            project=project,
            branch=branch,
            gitlab_token=gitlab_token,
        )

        # Create reviewer with file access
        reviewer = Reviewer(
            model=model,
            use_cli=True,
            repo_dir=str(repo_dir),
            jira_enabled=jira_enabled,
            enable_verification=enable_verification,
            effort=effort,
        )

        if not isinstance(reviewer, LocalReviewer):
            raise RuntimeError("clone mode requires a LocalReviewer with file access")

        # Posture is chosen by who will read the result, not by how the review was produced:
        # `mr` when the findings land in a shared thread, `high` for a dry run the person who
        # asked for it reads alone. Every drop rule in the reduce step runs identically at both.
        posture = posture_for(dry_run, brutal_mode())
        learning = bool(os.environ.get("MR_REVIEWER_LEARN"))
        logger.info(f"Reporting posture: {posture}")

        # Default: orchestrated review team (PO + Architect + batched Senior workers, merged).
        # This guarantees every changed file is reviewed (no single-pass attention taper) and
        # keeps cross-file breadth via the whole-MR Architect pass. Set MR_REVIEWER_SINGLE_PASS=1
        # to fall back to the legacy single mega-prompt call.
        if os.environ.get("MR_REVIEWER_SINGLE_PASS"):
            logger.info("MR_REVIEWER_SINGLE_PASS set — using legacy single-pass review")
            diff_content = build_diff_content(mr.changes, max_lines=0)
            if len(mr.changes) >= 50:
                diff_content = f"{build_diff_summary(mr.changes)}\n\n---\n\n{diff_content}"
                logger.info(f"Large MR ({len(mr.changes)} files) - included file summary")
            comments = reviewer.review_diff(
                diff_content=diff_content,
                language=language,
                mr_title=mr.title,
                mr_description=mr.description or "",
                jira_tickets=jira_tickets,
                jira_cloud_id=jira_cloud_id,
                has_markdown_files=has_markdown_files,
            )
            # The single-pass path has no reduce step of its own, so it gets the shared one here.
            # It is one call rather than a team, so there is nothing cross-pass to merge — but the
            # quote check, the file check and the caps are exactly as load-bearing for one pass as
            # for eight, and until this was wired in they simply did not run on this path.
            if comments:
                comments = reduce_for_mr(
                    comments, mr, repo_dir, dry_run=dry_run, diff_text=diff_content
                )
        else:
            from .orchestrator import orchestrate_review

            comments = orchestrate_review(
                reviewer=reviewer,
                changes=mr.changes,
                language=language,
                mr_title=mr.title,
                mr_description=mr.description or "",
                jira_tickets=jira_tickets,
                jira_cloud_id=jira_cloud_id,
                has_markdown_files=has_markdown_files,
                # MAKER is opt-in while it is being calibrated: it is the longest chain in the team
                # (two serial calls) and adds design-level comments to a review that is already at
                # its volume cap. Passing an empty target branch is what disables it.
                target_branch=mr.target_branch if os.environ.get("MR_REVIEWER_MAKER") else "",
                # Learning mode wants every instance of a pattern for the study document, which is
                # what `brutal` means to the reducer. The thread is bounded afterwards, by the caps
                # alone — see the learning branch below.
                posture="brutal" if learning else posture,
            )

        if comments is None:
            logger.error("Review failed - not posting any comments to MR")
            return 1, 0

        if len(comments) == 0:
            logger.info("No issues found in review")
            if dry_run:
                telemetry.record_local_run(
                    gitlab,
                    project,
                    mr.iid,
                    [],
                    mr.diff_refs.get("head_sha", ""),
                    maker=bool(os.environ.get("MR_REVIEWER_MAKER")),
                    learn=bool(os.environ.get("MR_REVIEWER_LEARN")),
                )
            else:
                gitlab.post_note(
                    project,
                    mr.iid,
                    "## 🤖 Automated Code Review\n\n✅ No significant issues found. The code looks good!",
                )
            return 0, 0

        # Sort comments by severity
        comments.sort(key=lambda c: SEVERITY_RANK.get(c.severity, 3))

        if learning:
            comments = _teach_and_cap(reviewer, comments, mr, project, language, dry_run)

        if dry_run:
            _report_dry_run(gitlab, reviewer, comments, mr, project)
        else:
            _post_review_comments(gitlab, project, mr, comments, reviewer)

        return 0, len(comments)

    finally:
        # Clean up clone to prevent cache bloat (each branch gets its own dir). The MAKER pass's
        # pre-change tree is removed by the orchestrator, but a killed run can leave it behind,
        # so unregister it here too before the clone that owns it disappears.
        if repo_dir:
            remove_base_tree(repo_dir)
            cleanup_repo(repo_dir)


def _teach_and_cap(reviewer, comments, mr, project: str, language: str, dry_run: bool) -> list:
    """Learning mode's post-processing: teach every finding, write the study document, then cap.

    Two consumers with two volumes, which is the whole reason this is a stage of its own. The
    document gets everything, because a concept's second and third instance are the lesson rather
    than noise. The MR thread gets a normal-sized review, because a junior's MR buried under forty
    threads teaches nothing — and learning mode changes what a comment *says*, not how many post.
    """
    from .orchestrator import generate_learning_note, teach_in_comments

    # Teach first, so the addenda are on the comments before either consumer sees them.
    concepts = teach_in_comments(reviewer, comments, language, mr.title)
    note = generate_learning_note(
        reviewer, comments, mr.changes, language, mr.title, concepts=concepts
    )
    if note:
        path = _write_learning_note(project, mr.iid, note)
        print(note)
        logger.info(f"📚 Learning note written to {path}")
    else:
        logger.warning("📚 Learning note could not be generated — continuing with comments")

    # Caps only. Re-running the whole reduce step here re-validated findings against a haystack
    # this call does not have and re-measured bodies `teach_in_comments` had just lengthened, so a
    # finding lost a severity rank to the teaching. Under --brutal there are no caps, so an
    # explicit request for everything still wins over this bound.
    capped = cap_comments(comments, mr, dry_run=dry_run)
    logger.info(f"📚 Posting {len(capped)} taught comment(s) to the MR thread")
    return capped


def _report_dry_run(gitlab, reviewer, comments, mr, project: str) -> None:
    """Print the review, cache it for `--post-cached`, and record that the run happened.

    The summary is generated here rather than at posting time so replaying the cache needs no LLM
    call. The run record is posted (when ``MR_REVIEWER_RECORD_RUNS`` is set) even though nothing else
    is, so a team measuring usage can count local reviews too.
    """
    summary = reviewer.generate_summary(comments, mr.title)

    logger.info("=== DRY RUN - Comments that would be posted ===")
    for comment in comments:
        note = "" if comment.inline else "  (report only — too long to post on a line)"
        print(f"\n[{comment.file}:{comment.line}]{note}")
        print(comment.to_markdown())
    print("\n[summary comment]")
    print(summary)
    print("\n=== End of dry run ===")

    _cache_dry_run(project, mr, summary, comments)
    telemetry.record_local_run(
        gitlab,
        project,
        mr.iid,
        comments,
        mr.diff_refs.get("head_sha", ""),
        maker=bool(os.environ.get("MR_REVIEWER_MAKER")),
        learn=bool(os.environ.get("MR_REVIEWER_LEARN")),
    )


def _post_cached_review(gitlab, project, mr_iid, mr) -> int:
    """
    Post the review a previous --dry-run cached for this MR.

    Refuses if the MR has moved on since the review was taken: comment positions
    are computed against the diff refs, so replaying onto a new head would either
    be rejected by the API or snap comments silently onto shifted lines.

    Returns:
        Exit code (0 for success).
    """
    cached = review_cache.load_review(project, mr_iid)
    if cached is None:
        logger.error(
            f"No cached review for {project}!{mr_iid}. Run the review with --dry-run "
            f"first, then re-run with --post-cached."
        )
        return 1

    current_head = mr.diff_refs.get("head_sha", "")
    if cached.head_sha != current_head:
        logger.error(
            f"Cached review is stale — it was taken against {cached.head_sha[:8] or '(unknown)'} "
            f"but the MR is now at {current_head[:8]}. New commits have landed; "
            f"re-run the review with --dry-run."
        )
        return 1

    if not cached.comments:
        logger.info("Cached review found no issues — posting the clean-bill note")
        gitlab.post_note(project, mr.iid, cached.summary)
        return 0

    # The cache holds what a `--dry-run` produced, and a dry run reports at `high` — the tier for
    # a report its requester reads alone. Posting it unchanged would put roughly three times the
    # intended volume into a shared thread, on the very path CLAUDE.md tells people to prefer.
    # Capping only: the findings cleared every drop rule when they were taken, and re-validating
    # them now against a tree that has since moved would drop good ones for the wrong reason.
    comments = cap_comments(cached.comments, mr, dry_run=False)
    logger.info(
        f"📤 Posting {len(comments)} of {len(cached.comments)} cached comment(s) at the "
        f"{posture_for(False, brutal_mode())} posture "
        f"(reviewed {cached.age_description}, head {current_head[:8]})"
    )
    _post_review_comments(gitlab, project, mr, comments, summary=cached.summary)
    return 0


def _write_learning_note(project: str, mr_iid: int, note: str) -> Path:
    """Write the study document into the current directory, and return the path.

    Deliberately not under ~/.cache like the review cache: a cache is disposable and this is something
    the reader is meant to keep, open in an editor, and probably share. The filename is obviously
    generated, and the pattern is gitignored so it cannot be committed by accident.
    """
    from . import review_cache

    path = Path.cwd() / f"mr-learning-{review_cache._slug(project)}-{mr_iid}.md"
    path.write_text(note, encoding="utf-8")
    return path


def _cache_dry_run(project, mr, summary, comments):
    """Persist a dry-run review so it can be posted later without re-reviewing."""
    try:
        review_cache.save_review(
            project=project,
            mr_iid=mr.iid,
            head_sha=mr.diff_refs.get("head_sha", ""),
            mr_title=mr.title,
            summary=summary,
            comments=comments,
        )
    except OSError as e:
        # A dry run's job is to show the review; failing to cache it is not fatal.
        logger.warning(f"Could not cache review: {e}")
        return

    logger.info(
        f"💾 Cached {len(comments)} comment(s) — post them unchanged with: "
        f"mr-reviewer review --url {mr.web_url} --post-cached"
    )


def _reduce_diff_mode(
    comments: list,
    mr,
    diff_content: str,
    dry_run: bool,
    files_context: dict | None = None,
    related_files: dict | None = None,
) -> list:
    """Reduce the deprecated diff mode's findings, which have no checkout to check against.

    Both diff-mode entry points call this. They each held a copy of it, comment included, and the
    live-file count inside it appeared at four sites in this module — so a change to how diff mode
    is reduced had to be made twice and would be made once.

    The evidence haystack is the diff **plus the file bodies the prompt was given**. With no
    checkout the file check cannot run at all, so the diff alone would drop any finding quoting an
    unchanged line of a file the prompt supplied as context — and if that took every finding, this
    path would post "No significant issues found" on an MR that had some. Clone mode is still the
    stronger check; this is the best available on a path that never clones.
    """
    if not comments:
        return comments
    evidence = "\n".join(
        [diff_content, *(files_context or {}).values(), *(related_files or {}).values()]
    )
    return reduce_for_mr(comments, mr, None, dry_run=dry_run, diff_text=evidence)


def _post_review_comments(gitlab, project, mr, comments, reviewer=None, summary=None):
    """
    Post review comments and summary to GitLab.

    Args:
        reviewer: Used only to generate the summary; unneeded when `summary` is given.
        summary: Pre-generated summary text (from a cached review). Generated via
            `reviewer` when omitted.
    """
    diff_refs = mr.diff_refs
    posted = 0

    # The reduce step's writing bar de-lists a finding whose body runs past 250 words. It is still
    # a real finding and it still reaches the author — appended to the summary note below, where a
    # long body is readable — but a comment that long is not read on a line of a diff, so posting
    # it inline would spend attention and buy none. Nothing is discarded for length.
    inline = [c for c in comments if c.inline]
    report_only = [c for c in comments if not c.inline]
    if report_only:
        logger.info(
            f"{len(report_only)} finding(s) too long to post inline — folded into the summary"
        )

    for comment in inline:
        try:
            matching_change = None
            for change in mr.changes:
                if change.new_path == comment.file:
                    matching_change = change
                    break

            if not matching_change:
                logger.warning(f"File not found in MR changes, posting as note: {comment.file}")
                gitlab.post_note(
                    project,
                    mr.iid,
                    f"**{comment.file}:{comment.line}**\n\n{comment.to_markdown()}",
                )
                posted += 1
                continue

            # Validate line is in the diff; snap to nearest if not
            valid_lines = _extract_diff_new_lines(matching_change.diff)
            target_line = comment.line
            if valid_lines and target_line not in valid_lines:
                snapped = _snap_to_nearest_diff_line(target_line, valid_lines)
                logger.debug(
                    f"Line {target_line} not in diff for {comment.file}, snapped to {snapped}"
                )
                target_line = snapped

            if target_line is None:
                gitlab.post_note(
                    project,
                    mr.iid,
                    f"**{comment.file}:{comment.line}**\n\n{comment.to_markdown()}",
                )
                posted += 1
                continue

            position = CommentPosition(
                base_sha=diff_refs.get("base_sha", ""),
                start_sha=diff_refs.get("start_sha", ""),
                head_sha=diff_refs.get("head_sha", ""),
                old_path=matching_change.old_path,
                new_path=matching_change.new_path,
                new_line=target_line,
            )

            gitlab.post_discussion(
                project,
                mr.iid,
                comment.to_markdown(),
                position=position,
            )
            posted += 1
            logger.info(f"Posted comment on {comment.file}:{comment.line}")

        except Exception as e:
            logger.warning(f"Failed to post inline comment: {e}")
            try:
                gitlab.post_note(
                    project,
                    mr.iid,
                    f"**{comment.file}:{comment.line}**\n\n{comment.to_markdown()}",
                )
                posted += 1
            except Exception as e2:
                logger.error(f"Failed to post note: {e2}")

    # Post summary
    if summary is None:
        summary = reviewer.generate_summary(comments, mr.title)
    gitlab.post_note(project, mr.iid, summary + _report_only_section(report_only))
    logger.info(f"Posted {posted} comments and summary")


def _report_only_section(comments: Sequence[ReviewComment]) -> str:
    """The findings the writing bar kept off the diff, rendered for the summary note.

    `report_only` is the reducer's own name for this set (`REPORT_ONLY_WORDS`, and the
    `report_only` count it returns), so it is the name used here too: a second word for one
    concept means the reader has to match them up.

    Collapsed, because the reason they are here is length — expanded, they would dominate the note
    they were moved into. The heading names the reason and takes the threshold from the reducer
    that owns it, so a comment cannot end up telling the author a number that is no longer true.
    """
    if not comments:
        return ""
    bodies = "\n\n---\n\n".join(f"**`{c.file}:{c.line}`** — {c.to_markdown()}" for c in comments)
    return (
        f"\n\n<details>\n<summary>{len(comments)} finding(s) too long to post on a line "
        f"(over {report_only_words()} words)</summary>\n\n{bodies}\n\n</details>"
    )


@dataclass
class MRReviewResult:
    """Result of reviewing a single MR."""

    mr_iid: int
    project: str
    title: str = ""
    web_url: str = ""
    status: str = "pending"  # "pending", "in_progress", "reviewed", "skipped", "error"
    error: str | None = None
    comment_count: int = 0
    duration_secs: float = 0.0


class ReviewTracker:
    """Thread-safe progress tracker for parallel MR reviews."""

    STATUS_EMOJI = {
        "pending": "⏳",
        "in_progress": "🔥",
        "reviewed": "✅",
        "skipped": "⏭️ ",
        "error": "❌",
    }

    def __init__(self, mrs: list):
        self._lock = threading.Lock()
        self._results: dict[int, MRReviewResult] = {}
        self._total = len(mrs)
        self._completed = 0
        self._start_time = time.monotonic()

        # Initialize all as pending
        for mr in mrs:
            self._results[mr.iid] = MRReviewResult(
                mr_iid=mr.iid,
                project=mr.project_path,
                title=mr.title,
                web_url=mr.web_url,
                status="pending",
            )

    def start(self, mr_iid: int) -> None:
        """Mark an MR as in-progress."""
        with self._lock:
            if mr_iid in self._results:
                self._results[mr_iid].status = "in_progress"

    def complete(self, result: MRReviewResult) -> None:
        """Record a completed review and print progress."""
        with self._lock:
            self._results[result.mr_iid] = result
            self._completed += 1
            self._print_progress(result)

    def _print_progress(self, result: MRReviewResult) -> None:
        """Print a progress line for a completed review."""
        emoji = self.STATUS_EMOJI.get(result.status, "❓")
        elapsed = f"{result.duration_secs:.0f}s" if result.duration_secs else ""

        detail = ""
        if result.status == "reviewed" and result.comment_count > 0:
            detail = f" ({result.comment_count} comments)"
        elif result.status == "error" and result.error:
            detail = f" - {result.error[:60]}"

        logger.info(
            f"[{self._completed}/{self._total}] {emoji} MR !{result.mr_iid} "
            f"{result.status}{detail} {elapsed}"
        )

    def print_queue(self) -> None:
        """Print the initial queue of MRs to review."""
        logger.info(f"\n{'─' * 70}")
        logger.info(f"📋 REVIEW QUEUE ({self._total} MR(s))")
        logger.info(f"{'─' * 70}")
        with self._lock:
            for r in self._results.values():
                logger.info(f"  ⏳ !{r.mr_iid} | {r.project} | {r.title[:50]}")
        logger.info(f"{'─' * 70}\n")

    def print_summary(self) -> None:
        """Print a final status table."""
        total_time = time.monotonic() - self._start_time

        with self._lock:
            reviewed = [r for r in self._results.values() if r.status == "reviewed"]
            skipped = [r for r in self._results.values() if r.status == "skipped"]
            errors = [r for r in self._results.values() if r.status == "error"]

        logger.info(f"\n{'═' * 70}")
        logger.info(
            f"📊 REVIEW SUMMARY — {len(reviewed)} reviewed, "
            f"{len(skipped)} skipped, {len(errors)} errors "
            f"({total_time:.0f}s total)"
        )
        logger.info(f"{'═' * 70}")

        with self._lock:
            for r in self._results.values():
                emoji = self.STATUS_EMOJI.get(r.status, "❓")
                line = f"  {emoji} !{r.mr_iid:<5} {r.status:<10}"
                if r.status == "reviewed":
                    line += f" ({r.comment_count} comments, {r.duration_secs:.0f}s)"
                elif r.status == "error" and r.error:
                    line += f" - {r.error[:40]}"
                logger.info(line)
                logger.info(f"    📋 {r.title[:60]}")
                if r.web_url:
                    logger.info(f"    🔗 {r.web_url}")

        logger.info(f"{'═' * 70}")

    @property
    def error_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._results.values() if r.status == "error")


def _review_single_mr(
    mr_summary,
    gitlab_url: str,
    gitlab_token: str,
    anthropic_key: str | None,
    model: str,
    effort: str,
    dry_run: bool,
    max_diff_lines: int,
    skip_reviewed: bool,
    use_cli: bool,
    include_file_context: bool,
    max_context_lines: int,
    fetch_related_files: bool,
    use_local_clone: bool = False,
    jira_cloud_id: str = "",
    jira_enabled: bool = False,
    default_group: str = "",
    tracker: ReviewTracker | None = None,
    enable_verification: bool = False,
) -> MRReviewResult:
    """
    Review a single MR. Designed to be called from a thread pool.

    Each invocation creates its own GitLab client and Reviewer to be thread-safe.
    """
    project = mr_summary.project_path
    mr_iid = mr_summary.iid
    start_time = time.monotonic()

    def _make_result(
        status: str, error: str | None = None, comment_count: int = 0
    ) -> MRReviewResult:
        return MRReviewResult(
            mr_iid=mr_iid,
            project=project,
            title=mr_summary.title,
            web_url=mr_summary.web_url,
            status=status,
            error=error,
            comment_count=comment_count,
            duration_secs=time.monotonic() - start_time,
        )

    def _log_mr_info():
        logger.info(f"[MR !{mr_iid}] 👤 {mr_summary.author} | 📂 {project}")
        logger.info(f"[MR !{mr_iid}] 🔗 {mr_summary.web_url}")

    if tracker:
        tracker.start(mr_iid)

    # Each thread gets its own clients for thread safety
    gitlab = GitLabClient(gitlab_url, gitlab_token, default_group=default_group)

    # Check if already reviewed
    if skip_reviewed:
        try:
            if gitlab.has_bot_reviewed(project, mr_iid):
                logger.info(f"[MR !{mr_iid}] ⏭️  Skipped (already reviewed): {mr_summary.title}")
                _log_mr_info()
                return _make_result("skipped")
        except Exception as e:
            logger.warning(f"[MR !{mr_iid}] Could not check review status: {e}")

    try:
        # Fetch full MR details
        mr = gitlab.get_mr_changes(project, mr_iid)

        if not mr.changes:
            logger.info(f"[MR !{mr_iid}] ⏭️  Skipped (no changes): {mr_summary.title}")
            _log_mr_info()
            return _make_result("skipped")

        logger.info(f"[MR !{mr_iid}] 🔥 Reviewing: {mr_summary.title}")
        _log_mr_info()

        # For merged MRs, source branch may be deleted; use target branch for file access
        context_branch = mr.source_branch
        if mr.state == "merged":
            context_branch = mr.target_branch
            logger.info(
                f"[MR !{mr_iid}] MR is merged — using target branch '{context_branch}' for file access"
            )

        # Detect language
        language = detect_language(mr.changes)
        logger.debug(f"[MR !{mr_iid}] Detected language: {language}, Files: {len(mr.changes)}")

        # Detect prompt files
        markdown_files_present = has_markdown_files(mr.changes)
        if markdown_files_present:
            logger.info(
                f"[MR !{mr_iid}] 📝 Markdown file(s) in diff — appending prompt-review guidance"
            )

        # Extract Jira tickets
        jira_tickets = []
        if jira_enabled and jira_cloud_id:
            jira_tickets = extract_jira_tickets(mr.title, mr.description or "")
            if jira_tickets:
                logger.info(f"[MR !{mr_iid}] 🎫 Found Jira ticket(s): {', '.join(jira_tickets)}")

        # --- Local clone mode ---
        if use_local_clone:
            result_code, comment_count = _review_with_local_clone(
                gitlab=gitlab,
                mr=mr,
                project=project,
                gitlab_url=gitlab_url,
                gitlab_token=gitlab_token,
                model=model,
                effort=effort,
                language=language,
                dry_run=dry_run,
                context_branch=context_branch,
                jira_tickets=jira_tickets,
                jira_cloud_id=jira_cloud_id,
                jira_enabled=jira_enabled,
                has_markdown_files=markdown_files_present,
                enable_verification=enable_verification,
            )
            status = "reviewed" if result_code == 0 else "error"
            return _make_result(status, comment_count=comment_count)

        # --- Standard mode ---
        diff_content = build_diff_content(mr.changes, max_diff_lines)

        if not diff_content.strip():
            logger.info(f"[MR !{mr_iid}] ⏭️  Skipped (empty diff): {mr_summary.title}")
            return _make_result("skipped")

        reviewer = Reviewer(
            api_key=anthropic_key,
            model=model,
            use_cli=use_cli,
            enable_verification=enable_verification,
            effort=effort,
        )

        # Fetch file context
        files_context = None
        if include_file_context:
            if use_cli:
                try:
                    context_needs = reviewer.request_context_needs(
                        diff_content=diff_content,
                        mr_title=mr.title,
                        mr_description=mr.description or "",
                    )
                    files_needed = context_needs.get("files_needed", [])
                    requested_files = []
                    for f in files_needed:
                        if isinstance(f, dict) and "path" in f:
                            requested_files.append(f["path"])
                        elif isinstance(f, str):
                            requested_files.append(f)
                    if requested_files:
                        files_context = gitlab.get_files_context(
                            project=project,
                            file_paths=requested_files,
                            ref=context_branch,
                            max_lines_per_file=max_context_lines,
                        )
                except Exception as e:
                    logger.warning(f"[MR !{mr_iid}] Two-pass context failed: {e}")
            else:
                file_paths = [
                    change.new_path
                    for change in mr.changes
                    if not change.deleted_file and not change.new_file
                ]
                if file_paths:
                    try:
                        files_context = gitlab.get_files_context(
                            project=project,
                            file_paths=file_paths,
                            ref=context_branch,
                            max_lines_per_file=max_context_lines,
                        )
                    except Exception as e:
                        logger.warning(f"[MR !{mr_iid}] Could not fetch file context: {e}")

        # Fetch related files
        related_files = None
        if fetch_related_files and files_context and language == "java":
            try:
                related_files = get_related_files_for_review(
                    gitlab=gitlab,
                    project=project,
                    branch=context_branch,
                    changed_files_content=files_context,
                )
            except Exception as e:
                logger.warning(f"[MR !{mr_iid}] Could not fetch related files: {e}")

        # Fetch repo prompt-engineering guide when prompt files are present
        prompt_guide_content = None
        if markdown_files_present:
            try:
                guide_map = gitlab.get_files_context(
                    project=project,
                    file_paths=[PROMPT_GUIDE_REPO_PATH],
                    ref=context_branch,
                    max_lines_per_file=0,
                )
                prompt_guide_content = guide_map.get(PROMPT_GUIDE_REPO_PATH)
            except Exception as e:
                logger.warning(f"[MR !{mr_iid}] Could not fetch prompt-engineering guide: {e}")

        # Perform review
        comments = reviewer.review_diff(
            diff_content=diff_content,
            language=language,
            mr_title=mr.title,
            mr_description=mr.description or "",
            files_context=files_context,
            related_files=related_files,
            jira_tickets=jira_tickets,
            jira_cloud_id=jira_cloud_id,
            has_markdown_files=markdown_files_present,
            prompt_guide_content=prompt_guide_content,
        )

        if comments is None:
            return _make_result("error", "Review parsing failed")

        comments = _reduce_diff_mode(
            comments, mr, diff_content, dry_run, files_context, related_files
        )

        if dry_run:
            for comment in comments:
                print(
                    f"  [{comment.stage}/{comment.severity}] {comment.file}:{comment.line} - {comment.title}"
                )
            telemetry.record_local_run(
                gitlab, project, mr_iid, comments, mr.diff_refs.get("head_sha", "")
            )
        else:
            if len(comments) == 0:
                gitlab.post_note(
                    project,
                    mr_iid,
                    "## 🤖 Automated Code Review\n\n✅ No significant issues found. The code looks good!",
                )
            else:
                comments.sort(key=lambda c: SEVERITY_RANK.get(c.severity, 3))
                _post_review_comments(gitlab, project, mr, comments, reviewer)

        return _make_result("reviewed", comment_count=len(comments))

    except Exception as e:
        logger.error(f"[MR !{mr_iid}] ❌ Error: {e}")
        return _make_result("error", str(e))


def review_all_pending(
    gitlab_url: str,
    gitlab_token: str,
    anthropic_key: str | None = None,
    model: str = DEFAULT_MODEL,
    effort: str = "",
    dry_run: bool = False,
    max_diff_lines: int = 3000,
    skip_reviewed: bool = True,
    use_cli: bool = False,
    include_file_context: bool = True,
    max_context_lines: int = 500,
    fetch_related_files: bool = True,
    max_parallel: int = 4,
    use_local_clone: bool = False,
    jira_cloud_id: str = "",
    jira_enabled: bool = False,
    reviewer_username: str | None = None,
    projects: list[str] | None = None,
    default_group: str = "",
    enable_verification: bool = False,
) -> int:
    """
    Review all MRs assigned to the current user that haven't been reviewed yet.

    Args:
        gitlab_url: GitLab instance URL
        gitlab_token: GitLab API token
        anthropic_key: Anthropic API key (not needed if use_cli=True)
        model: Claude model to use
        dry_run: If True, don't post comments
        max_diff_lines: Maximum diff lines to process
        skip_reviewed: Skip MRs that have already been reviewed by bot
        use_cli: If True, use Claude Code CLI instead of API
        include_file_context: If True, fetch full file content for context
        max_context_lines: Maximum lines per file for context
        fetch_related_files: If True, fetch related domain entities for context
        max_parallel: Maximum number of MRs to review in parallel (1 = sequential)
        use_local_clone: If True, clone repos and let Claude browse files directly
        jira_cloud_id: Atlassian cloud ID for Jira MCP integration
        jira_enabled: If True, fetch Jira story context via MCP during review

    Returns:
        Exit code (0 for success)
    """
    gitlab = GitLabClient(gitlab_url, gitlab_token, default_group=default_group)

    # Resolve any short project paths against the default group for logging parity
    if projects:
        resolved_projects = []
        for p in projects:
            r = gitlab.resolve_project(p)
            if r != p:
                logger.info(f"Resolved project path '{p}' → '{r}'")
            resolved_projects.append(r)
        projects = resolved_projects

    if projects:
        if reviewer_username:
            logger.info(
                f"👤 Fetching open MRs in {len(projects)} project(s) "
                f"assigned to reviewer: {reviewer_username}"
            )
        else:
            logger.info(f"📦 Fetching all open MRs in {len(projects)} project(s)")
    elif reviewer_username:
        logger.info(f"👤 Fetching MRs for reviewer: {reviewer_username}")
    else:
        user = gitlab.get_current_user()
        logger.info(f"👤 Fetching MRs for reviewer: {user['username']} (token owner)")

    mrs = gitlab.get_mrs_for_review(
        state="opened",
        reviewer_username=reviewer_username,
        projects=projects,
    )

    if not mrs:
        logger.info("No open MRs found for the given filters")
        return 0

    logger.info(f"📬 Found {len(mrs)} open MR(s) to review")

    # Clamp parallel count
    max_parallel = max(1, min(max_parallel, len(mrs)))
    if max_parallel > 1:
        logger.info(f"⚡ Reviewing up to {max_parallel} MR(s) in parallel")

    # Progress tracker
    tracker = ReviewTracker(mrs)
    tracker.print_queue()

    # Common kwargs for each review
    review_kwargs: dict[str, Any] = {
        "gitlab_url": gitlab_url,
        "gitlab_token": gitlab_token,
        "anthropic_key": anthropic_key,
        "model": model,
        "effort": effort,
        "dry_run": dry_run,
        "max_diff_lines": max_diff_lines,
        "skip_reviewed": skip_reviewed,
        "use_cli": use_cli,
        "include_file_context": include_file_context,
        "max_context_lines": max_context_lines,
        "fetch_related_files": fetch_related_files,
        "use_local_clone": use_local_clone,
        "jira_cloud_id": jira_cloud_id,
        "jira_enabled": jira_enabled,
        "default_group": default_group,
        "tracker": tracker,
        "enable_verification": enable_verification,
    }

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = {
            executor.submit(_review_single_mr, mr_summary, **review_kwargs): mr_summary
            for mr_summary in mrs
        }

        for future in as_completed(futures):
            mr_summary = futures[future]
            try:
                result = future.result()
                tracker.complete(result)
            except Exception as e:
                logger.error(f"[MR !{mr_summary.iid}] Unexpected error: {e}")
                tracker.complete(
                    MRReviewResult(
                        mr_iid=mr_summary.iid,
                        project=mr_summary.project_path,
                        title=mr_summary.title,
                        web_url=mr_summary.web_url,
                        status="error",
                        error=str(e),
                    )
                )

    tracker.print_summary()

    return 0 if tracker.error_count == 0 else 1


def audit_repo(
    project: str,
    gitlab_url: str,
    gitlab_token: str,
    branch: str = "main",
    model: str = DEFAULT_MODEL,
    effort: str = "",
    scope: str = "",
    output_path: str | None = None,
    language: str | None = None,
    default_group: str = "",
    timeout_secs: int = 1800,
) -> int:
    """
    Audit an entire repository and write a markdown report.

    Clones (or updates) the repo at the given branch, detects the dominant
    language, runs the audit prompt through LocalReviewer, and writes the
    resulting markdown report to a file (or stdout if output_path is None).
    """
    from datetime import datetime

    from .repo_context import clone_or_update_repo

    gitlab = GitLabClient(gitlab_url, gitlab_token, default_group=default_group)

    resolved = gitlab.resolve_project(project)
    if resolved != project:
        logger.info(f"Resolved project path '{project}' → '{resolved}'")
        project = resolved

    logger.info(f"Cloning {project} (branch: {branch}) for audit...")
    repo_dir = clone_or_update_repo(
        gitlab_url=gitlab_url,
        project=project,
        branch=branch,
        gitlab_token=gitlab_token,
    )

    detected_lang, counts = detect_language_from_dir(repo_dir, scope=scope)
    effective_language = language or detected_lang
    if language and language != detected_lang:
        logger.info(f"Language override: using '{language}' (detected '{detected_lang}')")
    else:
        logger.info(f"Detected language: {detected_lang} (counts: {counts})")

    repo_name = project.split("/")[-1]

    # Map full model names to CLI shortcuts for the Reviewer factory
    cli_model = None
    if "opus" in model.lower():
        cli_model = "opus"
    elif "sonnet" in model.lower():
        cli_model = "sonnet"
    elif "haiku" in model.lower():
        cli_model = "haiku"

    auditor = LocalReviewer(repo_dir=str(repo_dir), model=cli_model)

    try:
        report = auditor.audit_repo(
            language=effective_language,
            repo_name=repo_name,
            branch=branch,
            scope=scope,
            timeout_secs=timeout_secs,
        )
    except Exception as e:
        logger.error(f"Audit failed: {e}")
        return 1

    if not report.strip():
        logger.error("Audit returned empty output")
        return 1

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = project.replace("/", "_")
        output_path = f"audit-{slug}-{ts}.md"

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info(f"📝 Audit report written to {output_path}")
    except OSError as e:
        logger.error(f"Failed to write report to {output_path}: {e}")
        # Fall back to stdout so the work isn't lost
        print(report)
        return 1

    return 0


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Automated GitLab MR code review with Claude",
        prog="mr-reviewer",
    )

    # Global options
    parser.add_argument(
        "--use-api",
        action="store_true",
        help="Use Anthropic API instead of Claude Code CLI (requires ANTHROPIC_API_KEY)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Review single MR command
    review_parser = subparsers.add_parser("review", help="Review a single merge request")
    review_parser.add_argument(
        "--project",
        "-p",
        help="GitLab project path (e.g., group/repo)",
    )
    review_parser.add_argument(
        "--mr",
        "-m",
        type=int,
        help="Merge request IID",
    )
    review_parser.add_argument(
        "--url",
        "-u",
        help="Full MR URL (alternative to --project and --mr)",
    )
    review_parser.add_argument(
        "--language",
        "-l",
        choices=["java", "angular", "typescript", "react", "python", "generic"],
        help="Force specific language (auto-detect by default)",
    )
    review_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Review locally: print the findings instead of posting them inline. Still "
        "posts one summary note recording that the run happened and what it found, "
        "so a local review is not invisible. There is no way to suppress that note.",
    )
    review_parser.add_argument(
        "--post-cached",
        action="store_true",
        help="Post the review a previous --dry-run cached for this MR, instead of "
        "reviewing again. No clone and no LLM calls; refuses if new commits have "
        "landed since the review was taken.",
    )
    review_parser.add_argument(
        "--use-api",
        action="store_true",
        help="Use Anthropic API instead of Claude Code CLI (requires ANTHROPIC_API_KEY)",
    )
    review_parser.add_argument(
        "--config",
        "-c",
        help="Path to config file",
    )
    review_parser.add_argument(
        "--no-context",
        action="store_true",
        help="Don't fetch full file content for context (faster but less accurate)",
    )
    review_parser.add_argument(
        "--max-context-lines",
        type=int,
        default=500,
        help="Maximum lines per file for context (default: 500)",
    )
    review_parser.add_argument(
        "--no-related-files",
        action="store_true",
        help="Don't fetch related domain files (entities, interfaces) for context",
    )
    review_parser.add_argument(
        "--no-local-clone",
        action="store_true",
        help="[DEPRECATED] Use diff mode instead of cloning. Diff mode is deprecated — it truncates "
        "large MRs, cannot browse the codebase, and yields weaker structural/craft findings, and "
        "it will be removed. Clone mode is the supported review path.",
    )
    review_parser.add_argument(
        "--enable-verification",
        action="store_true",
        help="Run the verification pass that filters weak comments (off by default — currently over-prunes high-quality comments)",
    )
    review_parser.add_argument(
        "--enable-maker",
        action="store_true",
        help="Run the blind design pass: design the change from its Jira story in a pre-change tree with no git history, then diff that design against what was built. Needs a story with acceptance criteria and skips itself without one. Off by default — two serial Claude calls, the longest chain in the review, and still being calibrated. Also enabled by MR_REVIEWER_MAKER=1.",
    )
    review_parser.add_argument(
        "--learn",
        action="store_true",
        help="Learning mode: run the review with volume caps lifted, then produce a study document that teaches the design concepts this MR illustrates, using its own code as the examples. Writes mr-learning-<project>-<iid>.md and posts nothing. Also enabled by MR_REVIEWER_LEARN=1.",
    )
    review_parser.add_argument(
        "--brutal",
        action="store_true",
        help="Brutal mode: report every finding that clears the evidence bar, with all volume caps off (per-stage, per-file, global, and the symptom absorption that folds craft findings into a design root). The evidence bar itself is unchanged — findings still have to quote the lines that prove them. Expect several times the usual comment count, so prefer it with --dry-run or on a review you are reading yourself. Also enabled by MR_REVIEWER_BRUTAL=1.",
    )
    review_parser.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Reasoning effort for every review pass (low|medium|high|xhigh|max). Omit it and each pass uses its own calibrated level from reviewer.EFFORT_BY_STAGE — high for ARCHITECT and BLIND DESIGN/compare, which derive a consequence from several facts; medium for the craft pass, which is by far the most numerous and so the largest effort cost in the run. Also set by MR_REVIEWER_EFFORT.",
    )

    # Review all pending MRs command
    review_all_parser = subparsers.add_parser(
        "review-all", help="Review all open MRs assigned to you as reviewer"
    )
    review_all_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Review locally: print the findings instead of posting them inline. Still "
        "posts one summary note per MR recording the run and its counts.",
    )
    review_all_parser.add_argument(
        "--include-reviewed",
        action="store_true",
        help="Re-review MRs that have already been reviewed by the bot",
    )
    review_all_parser.add_argument(
        "--use-api",
        action="store_true",
        help="Use Anthropic API instead of Claude Code CLI (requires ANTHROPIC_API_KEY)",
    )
    review_all_parser.add_argument(
        "--config",
        "-c",
        help="Path to config file",
    )
    review_all_parser.add_argument(
        "--no-context",
        action="store_true",
        help="Don't fetch full file content for context (faster but less accurate)",
    )
    review_all_parser.add_argument(
        "--max-context-lines",
        type=int,
        default=500,
        help="Maximum lines per file for context (default: 500)",
    )
    review_all_parser.add_argument(
        "--no-related-files",
        action="store_true",
        help="Don't fetch related domain files (entities, interfaces) for context",
    )
    review_all_parser.add_argument(
        "--parallel",
        "-j",
        type=int,
        default=None,
        help="Number of MRs to review in parallel (default: from config, usually 4)",
    )
    review_all_parser.add_argument(
        "--no-local-clone",
        action="store_true",
        help="[DEPRECATED] Use diff mode instead of cloning. Diff mode is deprecated — it truncates "
        "large MRs, cannot browse the codebase, and yields weaker structural/craft findings, and "
        "it will be removed. Clone mode is the supported review path.",
    )
    review_all_parser.add_argument(
        "--reviewer",
        help="GitLab username of the reviewer to filter by (defaults to token owner)",
    )
    review_all_parser.add_argument(
        "--projects",
        action="append",
        default=None,
        help="Project path(s) to scan (e.g. group/repo). Repeat the flag or use comma-separated values. "
        "When set, scans all open MRs in those projects regardless of assignee (unless --reviewer is also given).",
    )
    review_all_parser.add_argument(
        "--enable-verification",
        action="store_true",
        help="Run the verification pass that filters weak comments (off by default — currently over-prunes high-quality comments)",
    )
    review_all_parser.add_argument(
        "--enable-maker",
        action="store_true",
        help="Run the blind design pass: design the change from its Jira story in a pre-change tree with no git history, then diff that design against what was built. Needs a story with acceptance criteria and skips itself without one. Off by default — two serial Claude calls, the longest chain in the review, and still being calibrated. Also enabled by MR_REVIEWER_MAKER=1.",
    )
    review_all_parser.add_argument(
        "--learn",
        action="store_true",
        help="Learning mode: run the review with volume caps lifted, then produce a study document that teaches the design concepts this MR illustrates, using its own code as the examples. Writes mr-learning-<project>-<iid>.md and posts nothing. Also enabled by MR_REVIEWER_LEARN=1.",
    )
    review_all_parser.add_argument(
        "--brutal",
        action="store_true",
        help="Brutal mode: report every finding that clears the evidence bar, with all volume caps off (per-stage, per-file, global, and the symptom absorption that folds craft findings into a design root). The evidence bar itself is unchanged — findings still have to quote the lines that prove them. Expect several times the usual comment count, so prefer it with --dry-run or on a review you are reading yourself. Also enabled by MR_REVIEWER_BRUTAL=1.",
    )
    review_all_parser.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Reasoning effort for every review pass (low|medium|high|xhigh|max). Omit it and each pass uses its own calibrated level from reviewer.EFFORT_BY_STAGE — high for ARCHITECT and BLIND DESIGN/compare, which derive a consequence from several facts; medium for the craft pass, which is by far the most numerous and so the largest effort cost in the run. Also set by MR_REVIEWER_EFFORT.",
    )

    # List pending MRs command
    # Audit entire repo command
    audit_parser = subparsers.add_parser(
        "audit",
        help="Audit an entire repository for structural issues; writes a markdown report",
    )
    audit_parser.add_argument(
        "project",
        help="GitLab project path (e.g. group/repo, or a short path that resolves against default_group)",
    )
    audit_parser.add_argument(
        "--branch",
        "-b",
        default="main",
        help="Branch to audit (default: main)",
    )
    audit_parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output markdown file (default: audit-<project>-<timestamp>.md in cwd)",
    )
    audit_parser.add_argument(
        "--scope",
        "-s",
        default="",
        help="Restrict the audit to a subtree (path relative to repo root, e.g. src/main/java/com/foo)",
    )
    audit_parser.add_argument(
        "--language",
        "-l",
        choices=["java", "angular", "typescript", "react", "python", "generic"],
        help="Force a specific language (auto-detected by default)",
    )
    audit_parser.add_argument(
        "--config",
        "-c",
        help="Path to config file",
    )
    audit_parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Subprocess timeout in seconds (default: 1800 = 30 min)",
    )

    list_parser = subparsers.add_parser(
        "list", help="List open MRs assigned to a reviewer or in specific projects"
    )
    list_parser.add_argument(
        "--config",
        "-c",
        help="Path to config file",
    )
    list_parser.add_argument(
        "--reviewer",
        help="GitLab username of the reviewer to filter by (defaults to token owner)",
    )
    list_parser.add_argument(
        "--projects",
        action="append",
        default=None,
        help="Project path(s) to scan. Repeat the flag or use comma-separated values.",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Determine if using API backend (CLI is the default)
    use_api = getattr(args, "use_api", False)
    use_cli = not use_api

    if args.command == "review":
        if getattr(args, "enable_maker", False):
            os.environ["MR_REVIEWER_MAKER"] = "1"
        if getattr(args, "learn", False):
            os.environ["MR_REVIEWER_LEARN"] = "1"
        if getattr(args, "brutal", False):
            os.environ["MR_REVIEWER_BRUTAL"] = "1"
        if getattr(args, "effort", None):
            os.environ["MR_REVIEWER_EFFORT"] = args.effort

        # Load config
        config = load_config(args.config)

        # Validate config (require API key only if using API backend)
        errors = validate_config(config, require_anthropic_key=use_api)
        if errors:
            for error in errors:
                logger.error(error)
            return 1

        # Determine project and MR IID
        if args.url:
            try:
                gitlab_url, project, mr_iid = GitLabClient.parse_mr_url(args.url)
            except ValueError as e:
                logger.error(str(e))
                return 1
        elif args.project and args.mr:
            project = args.project
            mr_iid = args.mr
            gitlab_url = config.gitlab.url
        else:
            logger.error("Must specify either --url or both --project and --mr")
            return 1

        if args.post_cached and args.dry_run:
            logger.error("--post-cached and --dry-run are mutually exclusive")
            return 1

        # Local clone is default for CLI mode; --no-local-clone or --use-api disables it
        use_local_clone = use_cli and not args.no_local_clone
        if not use_local_clone:
            logger.warning(
                "⚠️  Diff mode (non-clone) is DEPRECATED and slated for removal — clone mode is the "
                "supported review path. Avoid --no-local-clone / --use-api for reviews."
            )

        return review_mr(
            project=project,
            mr_iid=mr_iid,
            gitlab_url=gitlab_url,
            gitlab_token=config.gitlab.token,
            anthropic_key=config.anthropic.api_key,
            model=config.anthropic.model,
            effort=config.anthropic.effort,
            language=args.language,
            dry_run=args.dry_run,
            max_diff_lines=config.review.max_diff_lines,
            use_cli=use_cli,
            include_file_context=not args.no_context,
            max_context_lines=args.max_context_lines,
            fetch_related_files=not args.no_related_files,
            use_local_clone=use_local_clone,
            jira_cloud_id=config.jira.cloud_id if config.jira.enabled else "",
            jira_enabled=config.jira.enabled,
            default_group=config.review.default_group,
            enable_verification=args.enable_verification,
            post_cached=args.post_cached,
        )

    elif args.command == "review-all":
        if getattr(args, "enable_maker", False):
            os.environ["MR_REVIEWER_MAKER"] = "1"
        if getattr(args, "learn", False):
            os.environ["MR_REVIEWER_LEARN"] = "1"
        if getattr(args, "brutal", False):
            os.environ["MR_REVIEWER_BRUTAL"] = "1"
        if getattr(args, "effort", None):
            os.environ["MR_REVIEWER_EFFORT"] = args.effort

        # Load config
        config = load_config(args.config)

        # Validate config (require API key only if using API backend)
        errors = validate_config(config, require_anthropic_key=use_api)
        if errors:
            for error in errors:
                logger.error(error)
            return 1

        max_parallel = args.parallel if args.parallel is not None else config.review.max_parallel

        # Local clone is default for CLI mode
        use_local_clone = use_cli and not args.no_local_clone
        if not use_local_clone:
            logger.warning(
                "⚠️  Diff mode (non-clone) is DEPRECATED and slated for removal — clone mode is the "
                "supported review path. Avoid --no-local-clone / --use-api for reviews."
            )

        # Reviewer: CLI flag > config > None (defaults to token owner)
        reviewer_username = args.reviewer or config.review.reviewer_username or None

        # Projects: CLI flag > config > None
        cli_projects = _parse_projects_arg(args.projects)
        projects = cli_projects or config.review.projects or None

        return review_all_pending(
            gitlab_url=config.gitlab.url,
            gitlab_token=config.gitlab.token,
            anthropic_key=config.anthropic.api_key,
            model=config.anthropic.model,
            effort=config.anthropic.effort,
            dry_run=args.dry_run,
            max_diff_lines=config.review.max_diff_lines,
            skip_reviewed=not args.include_reviewed,
            use_cli=use_cli,
            include_file_context=not args.no_context,
            max_context_lines=args.max_context_lines,
            fetch_related_files=not args.no_related_files,
            max_parallel=max_parallel,
            use_local_clone=use_local_clone,
            jira_cloud_id=config.jira.cloud_id if config.jira.enabled else "",
            jira_enabled=config.jira.enabled,
            reviewer_username=reviewer_username,
            projects=projects,
            default_group=config.review.default_group,
            enable_verification=args.enable_verification,
        )

    elif args.command == "audit":
        config = load_config(args.config)
        if not config.gitlab.token:
            logger.error("GitLab token not configured. Set GITLAB_TOKEN environment variable.")
            return 1
        return audit_repo(
            project=args.project,
            gitlab_url=config.gitlab.url,
            gitlab_token=config.gitlab.token,
            branch=args.branch,
            model=config.anthropic.model,
            effort=config.anthropic.effort,
            scope=args.scope,
            output_path=args.output,
            language=args.language,
            default_group=config.review.default_group,
            timeout_secs=args.timeout,
        )

    elif args.command == "list":
        # Load config
        config = load_config(args.config)

        # Validate GitLab config only
        if not config.gitlab.token:
            logger.error("GitLab token not configured. Set GITLAB_TOKEN environment variable.")
            return 1

        gitlab = GitLabClient(
            config.gitlab.url,
            config.gitlab.token,
            default_group=config.review.default_group,
        )

        reviewer_username = args.reviewer or config.review.reviewer_username or None
        cli_projects = _parse_projects_arg(args.projects)
        projects = cli_projects or config.review.projects or None
        if projects:
            resolved = []
            for p in projects:
                r = gitlab.resolve_project(p)
                if r != p:
                    logger.info(f"Resolved project path '{p}' → '{r}'")
                resolved.append(r)
            projects = resolved

        if projects and reviewer_username:
            logger.info(
                f"📦 Open MRs in {len(projects)} project(s), reviewer: {reviewer_username}\n"
            )
        elif projects:
            logger.info(f"📦 Open MRs in {len(projects)} project(s)\n")
        elif reviewer_username:
            logger.info(f"👤 Open MRs for reviewer: {reviewer_username}\n")
        else:
            user = gitlab.get_current_user()
            logger.info(f"👤 Open MRs assigned to: {user['username']} (token owner)\n")

        mrs = gitlab.get_mrs_for_review(
            state="opened",
            reviewer_username=reviewer_username,
            projects=projects,
        )

        if not mrs:
            logger.info("No open MRs found for the given filters")
            return 0

        for mr in mrs:
            reviewed = gitlab.has_bot_reviewed(mr.project_path, mr.iid)
            status = "✅ reviewed" if reviewed else "⏳ pending"
            print(f"[{status}] !{mr.iid} - {mr.title}")
            print(f"    📂 Project: {mr.project_path}")
            print(f"    👤 Author:  {mr.author}")
            print(f"    🔗 URL:  {mr.web_url}")
            print()

        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
