"""
Persistence for a completed review so a dry run can be posted later.

A dry run currently discards the whole review — several minutes of LLM passes —
and because the review is not deterministic, re-running to post produces a
*different* set of comments than the one that was inspected. Caching the
finished review makes "inspect, then post" post exactly what was inspected, and
lets the posting step run with no LLM calls and no clone.

The cached `head_sha` is what makes replay safe: comment positions are computed
against the MR's diff refs, so a cache taken against an older head must not be
posted (callers compare and refuse).
"""

import json
import logging
import re
from dataclasses import MISSING, asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from .reviewer import ReviewComment

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "mr-reviewer" / "reviews"

# Bumped when the on-disk shape changes; a mismatch is treated as a cache miss
# rather than an error, so a stale file never blocks a run.
CACHE_VERSION = 1


@dataclass
class CachedReview:
    """A finished review, replayable onto the MR it was produced from."""

    project: str
    mr_iid: int
    head_sha: str
    mr_title: str
    summary: str
    comments: list[ReviewComment]
    created_at: str

    @property
    def age_description(self) -> str:
        """Human-readable age, for telling the user how old a replay is."""
        try:
            created = datetime.fromisoformat(self.created_at)
        except ValueError:
            return "unknown age"
        delta = datetime.now(timezone.utc) - created
        minutes = int(delta.total_seconds() // 60)
        if minutes < 1:
            return "just now"
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"


def _slug(project: str) -> str:
    """Filesystem-safe form of a project path (``a/b/c`` → ``a_b_c``)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", project)


def cache_path(project: str, mr_iid: int, cache_dir: Path | None = None) -> Path:
    """
    Path of the cache file for one MR.

    Keyed on project+iid only — not head_sha — so a later run can find the cache
    without knowing which commit it was taken against. The sha lives inside the
    file, where the staleness check reads it. One review per MR: a new dry run
    replaces the previous one.
    """
    return (cache_dir or DEFAULT_CACHE_DIR) / f"{_slug(project)}-{mr_iid}.json"


def save_review(
    project: str,
    mr_iid: int,
    head_sha: str,
    mr_title: str,
    summary: str,
    comments: list[ReviewComment],
    cache_dir: Path | None = None,
) -> Path:
    """
    Write a finished review to the cache, replacing any previous one for this MR.

    Returns:
        Path written.
    """
    path = cache_path(project, mr_iid, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": CACHE_VERSION,
        "project": project,
        "mr_iid": mr_iid,
        "head_sha": head_sha,
        "mr_title": mr_title,
        "summary": summary,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "comments": [asdict(comment) for comment in comments],
    }

    # Write-then-rename so an interrupted write cannot leave a half-parsed cache.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)
    return path


def load_review(
    project: str,
    mr_iid: int,
    cache_dir: Path | None = None,
) -> CachedReview | None:
    """
    Read the cached review for an MR.

    Returns:
        The cached review, or None if absent, unreadable, or written by a
        different cache version. Never raises — a bad cache is a miss.
    """
    path = cache_path(project, mr_iid, cache_dir)
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Ignoring unreadable review cache {path}: {e}")
        return None

    if payload.get("version") != CACHE_VERSION:
        logger.warning(
            f"Ignoring review cache {path}: version {payload.get('version')} "
            f"!= expected {CACHE_VERSION}"
        )
        return None

    # Tolerate a ReviewComment that has gained or lost fields since the cache was
    # written — keep what still matches, let defaults cover the rest.
    known = {f.name for f in fields(ReviewComment)}
    required = {
        f.name
        for f in fields(ReviewComment)
        if f.default is MISSING and f.default_factory is MISSING
    }
    comments = []
    for raw in payload.get("comments", []):
        if not required.issubset(raw.keys()):
            logger.warning(f"Ignoring review cache {path}: comment missing required fields")
            return None
        comments.append(ReviewComment(**{k: v for k, v in raw.items() if k in known}))

    try:
        return CachedReview(
            project=payload["project"],
            mr_iid=payload["mr_iid"],
            head_sha=payload.get("head_sha", ""),
            mr_title=payload.get("mr_title", ""),
            summary=payload.get("summary", ""),
            comments=comments,
            created_at=payload.get("created_at", ""),
        )
    except KeyError as e:
        logger.warning(f"Ignoring review cache {path}: missing {e}")
        return None
