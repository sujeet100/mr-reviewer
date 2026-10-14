"""
GitLab API client for fetching MR details and posting review comments.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

import requests

logger = logging.getLogger(__name__)

# GitLab's maximum page size; also the signal that a short page is the last one.
_PER_PAGE = 100


@dataclass
class MRChange:
    """Represents a file change in a merge request."""

    old_path: str
    new_path: str
    diff: str
    new_file: bool
    renamed_file: bool
    deleted_file: bool


@dataclass
class MergeRequestSummary:
    """Lightweight MR info without full diff (for listing)."""

    iid: int
    project_id: int
    project_path: str
    title: str
    state: str
    web_url: str
    author: str
    created_at: str
    updated_at: str


@dataclass
class MergeRequest:
    """Represents a GitLab merge request."""

    iid: int
    title: str
    description: str
    source_branch: str
    target_branch: str
    state: str
    web_url: str
    author: str
    diff_refs: dict
    changes: list[MRChange]


@dataclass
class CommentPosition:
    """Position for an inline comment on a merge request."""

    base_sha: str
    start_sha: str
    head_sha: str
    old_path: str
    new_path: str
    position_type: str = "text"
    old_line: int | None = None
    new_line: int | None = None


class GitLabClient:
    """Client for interacting with the GitLab API."""

    def __init__(self, base_url: str, token: str, default_group: str = ""):
        """
        Initialize the GitLab client.

        Args:
            base_url: GitLab instance URL (e.g., https://gitlab.com)
            token: Personal Access Token with 'api' scope
            default_group: Parent group auto-prepended to short project paths
                (e.g. ``"my-group"`` turns ``"team/foo"`` into
                ``"my-group/team/foo"``). Paths that already start with this
                prefix are left unchanged. Empty string disables the behavior.
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.default_group = default_group.strip("/")
        self.session = requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token, "Content-Type": "application/json"})

    def _api_url(self, path: str) -> str:
        """Build full API URL."""
        return f"{self.base_url}/api/v4{path}"

    def resolve_project(self, project: str) -> str:
        """Prepend ``default_group`` to ``project`` if it's not already prefixed."""
        if not self.default_group:
            return project
        prefix = f"{self.default_group}/"
        if project == self.default_group or project.startswith(prefix):
            return project
        return f"{prefix}{project}"

    def _encode_project(self, project: str) -> str:
        """Resolve against the default group and URL-encode."""
        return quote_plus(self.resolve_project(project))

    def get_mr_changes(self, project: str, mr_iid: int) -> MergeRequest:
        """
        Fetch merge request details including file changes.

        Args:
            project: Project path (e.g., "group/repo")
            mr_iid: Merge request internal ID

        Returns:
            MergeRequest object with all changes
        """
        encoded_project = self._encode_project(project)

        # Get MR details
        mr_url = self._api_url(f"/projects/{encoded_project}/merge_requests/{mr_iid}")
        mr_response = self.session.get(mr_url)
        mr_response.raise_for_status()
        mr_data = mr_response.json()

        # Get MR changes (diffs)
        changes_url = self._api_url(f"/projects/{encoded_project}/merge_requests/{mr_iid}/changes")
        changes_response = self.session.get(changes_url)
        changes_response.raise_for_status()
        changes_data = changes_response.json()

        # Parse changes
        changes = []
        for change in changes_data.get("changes", []):
            changes.append(
                MRChange(
                    old_path=change.get("old_path", ""),
                    new_path=change.get("new_path", ""),
                    diff=change.get("diff", ""),
                    new_file=change.get("new_file", False),
                    renamed_file=change.get("renamed_file", False),
                    deleted_file=change.get("deleted_file", False),
                )
            )

        return MergeRequest(
            iid=mr_data["iid"],
            title=mr_data["title"],
            description=mr_data.get("description", ""),
            source_branch=mr_data["source_branch"],
            target_branch=mr_data["target_branch"],
            state=mr_data["state"],
            web_url=mr_data["web_url"],
            author=mr_data.get("author", {}).get("username", "unknown"),
            diff_refs=mr_data.get("diff_refs", {}),
            changes=changes,
        )

    def post_discussion(
        self, project: str, mr_iid: int, body: str, position: CommentPosition | None = None
    ) -> dict:
        """
        Post a discussion comment on a merge request.

        Args:
            project: Project path
            mr_iid: Merge request internal ID
            body: Comment body (markdown supported)
            position: Optional position for inline comment

        Returns:
            API response data
        """
        encoded_project = self._encode_project(project)
        url = self._api_url(f"/projects/{encoded_project}/merge_requests/{mr_iid}/discussions")

        data: dict[str, Any] = {"body": body}

        if position:
            data["position"] = {
                "base_sha": position.base_sha,
                "start_sha": position.start_sha,
                "head_sha": position.head_sha,
                "position_type": position.position_type,
                "old_path": position.old_path,
                "new_path": position.new_path,
            }
            if position.new_line is not None:
                data["position"]["new_line"] = position.new_line
            if position.old_line is not None:
                data["position"]["old_line"] = position.old_line

        response = self.session.post(url, json=data)
        response.raise_for_status()
        discussion: dict = response.json()
        return discussion

    def post_note(self, project: str, mr_iid: int, body: str) -> dict:
        """
        Post a general note/comment on a merge request (not inline).

        Falls back to a positionless discussion if — and only if — /notes is
        forbidden. Fine-grained tokens can be allowed to write /discussions while
        403ing on /notes, and a positionless discussion renders as the same
        MR-level comment. Any other error is raised as usual.

        Args:
            project: Project path
            mr_iid: Merge request internal ID
            body: Comment body (markdown supported)

        Returns:
            API response data — a note object, or a discussion object (with a
            "notes" list) when the fallback was used.
        """
        encoded_project = self._encode_project(project)
        url = self._api_url(f"/projects/{encoded_project}/merge_requests/{mr_iid}/notes")

        response = self.session.post(url, json={"body": body})
        if response.status_code == 403:
            logger.warning("/notes forbidden (403) — retrying as a positionless discussion")
            return self.post_discussion(project, mr_iid, body)
        response.raise_for_status()
        note: dict = response.json()
        return note

    def get_current_user(self) -> dict:
        """
        Get the current authenticated user's information.

        Returns:
            User data dict with id, username, name, etc.
        """
        url = self._api_url("/user")
        response = self.session.get(url)
        response.raise_for_status()
        user: dict = response.json()
        return user

    def get_project_path(self, project_id: int) -> str:
        """
        Get project path from project ID.

        Args:
            project_id: GitLab project ID

        Returns:
            Project path (e.g., "group/repo")
        """
        url = self._api_url(f"/projects/{project_id}")
        response = self.session.get(url)
        response.raise_for_status()
        project: dict = response.json()
        path: str = project["path_with_namespace"]
        return path

    def get_user_by_username(self, username: str) -> dict | None:
        """
        Look up a GitLab user by username.

        Returns:
            User dict (with id, username, name, ...) or None if not found.
        """
        url = self._api_url("/users")
        response = self.session.get(url, params={"username": username})
        response.raise_for_status()
        users = response.json()
        return users[0] if users else None

    def _mr_summary_from_api(self, mr_data: dict) -> MergeRequestSummary:
        """Build an MRSummary from a GitLab MR API response, resolving project path."""
        project_id = mr_data["project_id"]
        try:
            project_path = self.get_project_path(project_id)
        except Exception:
            project_path = str(project_id)

        return MergeRequestSummary(
            iid=mr_data["iid"],
            project_id=project_id,
            project_path=project_path,
            title=mr_data["title"],
            state=mr_data["state"],
            web_url=mr_data["web_url"],
            author=mr_data["author"]["username"],
            created_at=mr_data["created_at"],
            updated_at=mr_data["updated_at"],
        )

    def get_mrs_for_review(
        self,
        state: str = "opened",
        scope: str = "all",
        reviewer_username: str | None = None,
        projects: list[str] | None = None,
    ) -> list[MergeRequestSummary]:
        """
        Get merge requests to review.

        Modes:
        - If `projects` is set: fetch open MRs from each project. If
          `reviewer_username` is also set, keep only MRs where that user is
          a reviewer.
        - Else if `reviewer_username` is set: fetch MRs assigned to that user.
        - Else: fetch MRs assigned to the current (token-owner) user.

        Args:
            state: MR state filter (opened, closed, merged, all)
            scope: Scope filter (only applies to the user-level query)
            reviewer_username: GitLab username of the reviewer to filter by
            projects: List of project paths (e.g. ["group/repo", ...])

        Returns:
            List of MergeRequestSummary objects (deduplicated by (project_id, iid))
        """
        if projects:
            return self._get_mrs_from_projects(
                projects=projects,
                state=state,
                reviewer_username=reviewer_username,
            )

        # Resolve the reviewer to filter by
        if reviewer_username:
            user = self.get_user_by_username(reviewer_username)
            if not user:
                raise ValueError(f"GitLab user '{reviewer_username}' not found")
        else:
            user = self.get_current_user()

        url = self._api_url("/merge_requests")
        params = {
            "reviewer_id": user["id"],
            "state": state,
            "scope": scope,
            "per_page": 100,
        }

        response = self.session.get(url, params=params)
        response.raise_for_status()

        return [self._mr_summary_from_api(mr) for mr in response.json()]

    def _get_mrs_from_projects(
        self,
        projects: list[str],
        state: str = "opened",
        reviewer_username: str | None = None,
    ) -> list[MergeRequestSummary]:
        """Fetch MRs from specific projects, optionally filtered by reviewer."""
        reviewer_id = None
        if reviewer_username:
            user = self.get_user_by_username(reviewer_username)
            if not user:
                raise ValueError(f"GitLab user '{reviewer_username}' not found")
            reviewer_id = user["id"]

        results: list[MergeRequestSummary] = []
        seen: set[tuple[int, int]] = set()

        for project in projects:
            encoded = self._encode_project(project)
            url = self._api_url(f"/projects/{encoded}/merge_requests")
            params: dict[str, str | int] = {"state": state, "per_page": _PER_PAGE}
            if reviewer_id is not None:
                params["reviewer_id"] = reviewer_id

            page = 1
            try:
                while True:
                    params["page"] = page
                    response = self.session.get(url, params=params)
                    response.raise_for_status()
                    batch = response.json()
                    if not batch:
                        break
                    for mr_data in batch:
                        key = (mr_data["project_id"], mr_data["iid"])
                        if key in seen:
                            continue
                        seen.add(key)
                        results.append(self._mr_summary_from_api(mr_data))
                    if len(batch) < _PER_PAGE:
                        break
                    page += 1
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else "?"
                if status in (401, 403, 404):
                    logger.warning(
                        "Skipping project '%s': %s (check that the path includes "
                        "the full GitLab group and that your token has access)",
                        project,
                        status,
                    )
                    continue
                raise

        return results

    def get_mr_notes(self, project: str, mr_iid: int) -> list[dict]:
        """
        Get all notes/comments on a merge request.

        Args:
            project: Project path
            mr_iid: Merge request internal ID

        Returns:
            List of note dicts
        """
        encoded_project = self._encode_project(project)
        url = self._api_url(f"/projects/{encoded_project}/merge_requests/{mr_iid}/notes")
        params = {"per_page": 100}

        response = self.session.get(url, params=params)
        response.raise_for_status()
        notes: list[dict] = response.json()
        return notes

    def has_bot_reviewed(self, project: str, mr_iid: int) -> bool:
        """
        Check whether a review has already been *posted* to this MR.

        A local (dry-run) review also leaves a marker note, so this cannot match
        the marker alone: doing so would let a dry run permanently suppress the
        real review it was meant to precede. ``telemetry.is_posted_review``
        distinguishes the two by the note's machine-readable trailer, and treats
        a marker note with no trailer as a posted review, since every note
        predating that trailer was one.

        Args:
            project: Project path
            mr_iid: Merge request internal ID

        Returns:
            True if a full review has already been posted
        """
        from .telemetry import is_posted_review

        return any(
            is_posted_review(note.get("body", "")) for note in self.get_mr_notes(project, mr_iid)
        )

    def get_file_content(
        self,
        project: str,
        file_path: str,
        ref: str,
    ) -> str | None:
        """
        Fetch the content of a file from a specific branch/commit.

        Args:
            project: Project path (e.g., "group/repo")
            file_path: Path to the file in the repository
            ref: Branch name or commit SHA

        Returns:
            File content as string, or None if file doesn't exist
        """
        encoded_project = self._encode_project(project)
        encoded_path = quote_plus(file_path)

        url = self._api_url(f"/projects/{encoded_project}/repository/files/{encoded_path}/raw")
        params = {"ref": ref}

        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            return response.text
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return None  # File doesn't exist
            raise

    def get_files_context(
        self,
        project: str,
        file_paths: list[str],
        ref: str,
        max_lines_per_file: int = 500,
    ) -> dict[str, str]:
        """
        Fetch content for multiple files, with size limits.

        Args:
            project: Project path
            file_paths: List of file paths to fetch
            ref: Branch name or commit SHA
            max_lines_per_file: Maximum lines to include per file

        Returns:
            Dict mapping file path to content (truncated if needed)
        """
        files_context = {}

        for file_path in file_paths:
            content = self.get_file_content(project, file_path, ref)
            if content:
                lines = content.split("\n")
                if len(lines) > max_lines_per_file:
                    # Keep first N lines (imports, class declarations) and note truncation
                    truncated = "\n".join(lines[:max_lines_per_file])
                    content = f"{truncated}\n\n... [truncated, {len(lines) - max_lines_per_file} more lines]"
                files_context[file_path] = content

        return files_context

    @staticmethod
    def parse_mr_url(url: str) -> tuple[str, str, int]:
        """
        Parse a GitLab MR URL into components.

        Args:
            url: Full MR URL (e.g., https://gitlab.com/group/repo/-/merge_requests/123)

        Returns:
            Tuple of (base_url, project_path, mr_iid)
        """
        # Pattern: https://gitlab.com/group/subgroup/repo/-/merge_requests/123
        pattern = r"(https?://[^/]+)/(.+?)/-/merge_requests/(\d+)"
        match = re.match(pattern, url)

        if not match:
            raise ValueError(f"Invalid GitLab MR URL: {url}")

        base_url = match.group(1)
        project = match.group(2)
        mr_iid = int(match.group(3))

        return base_url, project, mr_iid
