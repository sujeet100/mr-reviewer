"""
Repository cloning for local file access during reviews.

Provides shallow clones of MR branches so Claude can browse
the full codebase using Read/Glob/Grep tools.
"""

import hashlib
import logging
import shutil
import subprocess
import tarfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Default work directory for clones
DEFAULT_CLONE_DIR = Path.home() / ".cache" / "mr-reviewer" / "repos"


def _repo_cache_key(gitlab_url: str, project: str, branch: str) -> str:
    """Generate a stable cache key for a project+branch combination."""
    key = f"{gitlab_url}/{project}/{branch}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _remove_git_locks(repo_dir: Path) -> None:
    """Remove stale git lock files that prevent operations."""
    git_dir = repo_dir / ".git"
    if not git_dir.exists():
        return
    for lock_file in git_dir.glob("*.lock"):
        try:
            lock_file.unlink()
            logger.debug(f"Removed stale lock file: {lock_file}")
        except OSError:
            pass


def clone_or_update_repo(
    gitlab_url: str,
    project: str,
    branch: str,
    gitlab_token: str,
    work_dir: Path | None = None,
    depth: int = 50,
) -> Path:
    """
    Shallow clone or update a repo at the MR source branch.

    Each project+branch gets its own directory to avoid conflicts when
    reviewing multiple MRs from the same project in parallel.

    Args:
        gitlab_url: GitLab instance URL (e.g., https://gitlab.com)
        project: Project path (e.g., group/repo)
        branch: Branch to check out (MR source branch)
        gitlab_token: GitLab token for authentication
        work_dir: Directory to clone into (default: ~/.cache/mr-reviewer/repos/)
        depth: Clone depth (default 50 for speed)

    Returns:
        Path to the cloned repo directory
    """
    if work_dir is None:
        work_dir = DEFAULT_CLONE_DIR

    work_dir.mkdir(parents=True, exist_ok=True)

    cache_key = _repo_cache_key(gitlab_url, project, branch)
    repo_dir = work_dir / cache_key

    # Build authenticated clone URL
    base = gitlab_url.rstrip("/")
    clone_url = f"{base}/{project}.git".replace("https://", f"https://oauth2:{gitlab_token}@")

    if repo_dir.exists() and (repo_dir / ".git").exists():
        _remove_git_locks(repo_dir)
        logger.info(f"Updating existing clone at {repo_dir}")
        try:
            subprocess.run(
                ["git", "fetch", "origin", branch, f"--depth={depth}"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
            subprocess.run(
                ["git", "checkout", "-f", f"origin/{branch}"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            subprocess.run(
                ["git", "clean", "-fdx"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            logger.info(f"Checked out branch '{branch}' in {repo_dir}")
            return repo_dir
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to update clone, re-cloning: {e.stderr}")
            shutil.rmtree(repo_dir, ignore_errors=True)

    # Fresh clone — clean up any partial leftover
    if repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)

    logger.info(f"Cloning {project} (branch: {branch}, depth: {depth})...")
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                str(depth),
                "--branch",
                branch,
                "--single-branch",
                clone_url,
                str(repo_dir),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        logger.info(f"Cloned to {repo_dir}")
        return repo_dir
    except subprocess.CalledProcessError as e:
        logger.error(f"Clone failed: {e.stderr}")
        raise RuntimeError(f"Failed to clone {project}: {e.stderr}") from e


def cleanup_repo(repo_dir: Path) -> None:
    """Remove a cloned repo directory."""
    if repo_dir and repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)
        logger.info(f"Cleaned up {repo_dir}")


def cleanup_all_repos(work_dir: Path | None = None) -> None:
    """Remove all cached repo clones."""
    if work_dir is None:
        work_dir = DEFAULT_CLONE_DIR
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
        logger.info(f"Cleaned up all repos in {work_dir}")


def _git(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a git command in `cwd`, capturing output. Does not raise on non-zero exit."""
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def base_worktree_path(repo_dir: Path) -> Path:
    """Sibling directory holding the pre-change checkout for a given clone."""
    repo_dir = Path(repo_dir)
    return repo_dir.parent / f"{repo_dir.name}-base"


def create_base_tree(
    repo_dir: Path,
    target_branch: str,
    depth: int = 50,
) -> Path | None:
    """Extract the codebase WITHOUT the MR's changes, as a plain directory beside the clone.

    The blind design pass has to design the change independently, which means it must not be able
    to read the implementation it will later be compared against. Enforcing that by instruction
    does not work — a model that can open the file will open the file — so the isolation is
    physical: the pass runs with its cwd here, where the files the MR adds do not exist and the
    files it modifies are in their pre-change form.

    It is a ``git archive`` extract rather than a worktree, because a worktree carries the object
    database: a design agent that thinks to run ``git log`` reaches the very branch it is supposed
    to be blind to. An archive has no ``.git`` at all, so there is nothing to read.

    The clone is shallow and single-branch, so the target branch has to be fetched before a merge
    base can be computed. If the shallow histories do not overlap, the tip of the target branch is
    used instead: for "the codebase without this change" that is close enough, and it degrades
    gracefully where an exact merge base would simply fail.

    Returns the tree path, or None if it could not be created — the caller skips the pass rather
    than failing the review.
    """
    repo_dir = Path(repo_dir)
    wt_path = base_worktree_path(repo_dir)

    remove_base_tree(repo_dir)

    fetched = _git(["fetch", "origin", target_branch, f"--depth={depth}"], repo_dir)
    if fetched.returncode != 0:
        logger.warning(
            f"BLIND DESIGN: could not fetch target branch '{target_branch}': {fetched.stderr.strip()}"
        )
        return None

    merge_base = _git(["merge-base", "FETCH_HEAD", "HEAD"], repo_dir, timeout=30)
    if merge_base.returncode == 0 and merge_base.stdout.strip():
        base_sha = merge_base.stdout.strip()
        origin = "merge base"
    else:
        tip = _git(["rev-parse", "FETCH_HEAD"], repo_dir, timeout=30)
        if tip.returncode != 0 or not tip.stdout.strip():
            logger.warning("BLIND DESIGN: could not resolve a pre-change commit; skipping the pass")
            return None
        base_sha = tip.stdout.strip()
        origin = f"tip of {target_branch} (shallow histories do not overlap)"

    wt_path.mkdir(parents=True, exist_ok=True)
    tar_path = wt_path.parent / f"{wt_path.name}.tar"
    archived = _git(["archive", "--format=tar", f"--output={tar_path}", base_sha], repo_dir)
    if archived.returncode != 0:
        logger.warning(
            f"BLIND DESIGN: could not archive the pre-change commit: {archived.stderr.strip()}"
        )
        remove_base_tree(repo_dir)
        return None
    try:
        with tarfile.open(tar_path) as tar:
            tar.extractall(wt_path, filter="data")
    except (tarfile.TarError, OSError) as exc:
        logger.warning(f"BLIND DESIGN: could not extract the pre-change tree: {exc}")
        remove_base_tree(repo_dir)
        return None
    finally:
        tar_path.unlink(missing_ok=True)

    if (wt_path / ".git").exists():
        logger.warning("BLIND DESIGN: extracted tree still carries .git; refusing to run blind")
        remove_base_tree(repo_dir)
        return None

    logger.info(
        f"BLIND DESIGN: pre-change tree at {wt_path} ({base_sha[:8]}, {origin}, no git history)"
    )
    return wt_path


def remove_base_tree(repo_dir: Path) -> None:
    """Remove the pre-change tree. Safe to call when absent.

    Older runs left a git worktree here rather than an archive extract, so the registration is
    pruned as well — a stale entry blocks branch deletes and confuses the next `git status`.
    """
    repo_dir = Path(repo_dir)
    wt_path = base_worktree_path(repo_dir)
    (wt_path.parent / f"{wt_path.name}.tar").unlink(missing_ok=True)
    if wt_path.exists() and (wt_path / ".git").exists():
        _git(["worktree", "remove", "--force", str(wt_path)], repo_dir, timeout=60)
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)
    if (repo_dir / ".git").exists():
        _git(["worktree", "prune"], repo_dir, timeout=30)
