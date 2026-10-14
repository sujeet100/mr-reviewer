"""
Claude integration for code review analysis.
Supports both Anthropic API and Claude Code CLI backends.
"""

import json
import logging
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .config import jira_mcp_tool
from .prompts import (
    get_audit_prompt,
    get_context_request_prompt,
    get_review_prompt,
    get_summary_prompt,
    get_verification_prompt,
)

logger = logging.getLogger(__name__)


@dataclass
class ReviewComment:
    """A single review comment."""

    file: str
    line: int
    severity: str  # CRITICAL, WARNING, SUGGESTION
    title: str
    body: str
    stage: str = "SENIOR"  # PO, ARCHITECT, SENIOR
    kind: str = ""  # SENIOR: "craft" (readability/maintainability) | "bug" (correctness).
    # MAKER: "design" (a root design decision, protected from the bug-heavy trim)
    inline: bool = True  # False when the reduce step's writing bar de-listed it: the finding is
    # real and stays in the report, but a body past the reducer's
    # REPORT_ONLY_WORDS is not read on a line of a diff, so posting it
    # inline costs attention and buys none.
    writing: str = ""  # The writing bar's note, when it acted: the word count and the ceiling.
    demoted_from: str = ""  # The severity the pass gave it, when the bar lowered one rank. Both are
    # the only record of why a badge sits below what the reviewer wrote, so
    # they travel with the comment rather than staying in the reducer.

    @property
    def severity_emoji(self) -> str:
        """Get emoji for severity level."""
        return {
            "CRITICAL": "🔴",
            "WARNING": "🟡",
            "SUGGESTION": "💡",
        }.get(self.severity, "💬")

    @property
    def stage_label(self) -> str:
        return {
            "PO": "Product Owner",
            "ARCHITECT": "Architect",
            "SENIOR": "Senior Developer",
            "MAKER": "Alternative implementation",
            "PROMPT": "Prompt Review",
        }.get(self.stage, "Reviewer")

    def to_markdown(self) -> str:
        """Format comment as markdown.

        Design findings get their own REDESIGN badge rather than a severity word. A root design
        decision is a different *kind* of feedback from a bug, not a milder one, and "🟡 WARNING"
        reads as the eighteenth item in a list rather than as the finding that reshapes the change.
        This stays a rendering concern on purpose: adding REDESIGN to the `severity` enum would put a
        kind value into an urgency field that five ranking sites and every prompt's severity ladder
        would then have to learn, and it could not order sensibly against CRITICAL.
        """
        if self.kind == "design":
            badge = "REDESIGN — MUST FIX" if self.severity == "CRITICAL" else "REDESIGN"
            return f"🔷 **{badge}**: {self.title}\n\n{self.body}"
        return f"{self.severity_emoji} **{self.severity}**: {self.title}\n\n{self.body}"


# The only bound applied to a single pass's output, and it is a malfunction guard rather than a
# cap: an agent told to rank its findings and return the important ones does not return forty, so
# forty means the instruction was ignored and the tail is padding. Everything past this is dropped
# lowest-ranked first, loudly.
#
# What used to be here was a per-stage cap (4 per pass, with a floor of 2 reserved for craft)
# applied at *parse* time — before the reduce step ever saw the findings, and therefore before
# dedup could tell it which of them were the same finding twice. It was the bound that bound
# hardest: a pass that found nine things had five of them deleted for being fifth. Volume is now
# decided once, at the end, by posture, where the whole picture is available and every suppressed
# finding is still recorded with the cap that removed it. That is also what made brutal mode
# almost indistinguishable from a normal run for so long — lifting the later caps did nothing
# while this one had already thrown the findings away.
RUNAWAY_GUARD = 15

# PO is exempt, and deliberately. It emits one comment per PARTIAL or MISSING acceptance criterion
# plus two roll-ups, so a story with fifteen gap criteria is a *correct* PO pass rather than a
# malfunctioning one. The deleted `STAGE_CAPS` said the same thing with `"PO": None`, and losing it
# here would cut exactly the two SUGGESTION-tier roll-ups the reduce step then protects by name.
GUARD_EXEMPT_STAGES = ("PO",)

# The one ordering of the three severities. It was written out four times — here and at three sorts
# in main.py — so the rule that decides which findings survive a trim had no source of truth.
SEVERITY_RANK = {"CRITICAL": 0, "WARNING": 1, "SUGGESTION": 2}


def _guard_runaway(comments: list["ReviewComment"]) -> list["ReviewComment"]:
    """Drop everything past ``RUNAWAY_GUARD`` from one pass, keeping the highest-severity.

    The pass is named from the findings rather than passed in: the one caller,
    ``_parse_response_to_comments``, does not know which stage it is parsing, and up to six passes
    run concurrently on the orchestrator's pool — so a warning that cannot say which pass tripped
    it arrives in interleaved logs attached to nothing.
    """
    bounded = [c for c in comments if c.stage in GUARD_EXEMPT_STAGES]
    rest = [c for c in comments if c.stage not in GUARD_EXEMPT_STAGES]
    if len(rest) <= RUNAWAY_GUARD:
        return comments
    ranked = sorted(rest, key=lambda c: SEVERITY_RANK.get(c.severity, 9))
    stages = ", ".join(sorted({c.stage for c in rest})) or "unknown"
    logger.warning(
        f"[{stages}] {len(rest)} findings from one pass — past the runaway guard of "
        f"{RUNAWAY_GUARD}; dropping {len(rest) - RUNAWAY_GUARD} lowest-severity. A pass this "
        "loud has ignored its ranking instruction, so the tail is unlikely to be the good part."
    )
    return bounded + ranked[:RUNAWAY_GUARD]


class BaseReviewer(ABC):
    """Abstract base class for code reviewers."""

    # Subclasses set this via factory; verification is opt-in.
    enable_verification: bool = False

    @abstractmethod
    def _send_prompt(self, prompt: str, max_tokens: int = 4096) -> str:
        """Send a prompt and return the response text."""
        pass

    def review_diff(
        self,
        diff_content: str,
        language: str,
        mr_title: str,
        mr_description: str = "",
        files_context: dict[str, str] | None = None,
        related_files: dict[str, str] | None = None,
        jira_tickets: list[str] | None = None,
        jira_cloud_id: str = "",
        has_markdown_files: bool = False,
        prompt_guide_content: str | None = None,
    ) -> list[ReviewComment] | None:
        """
        Review a diff and return comments.

        Args:
            diff_content: The diff content to review
            language: Programming language (java, angular, generic)
            mr_title: MR title for context
            mr_description: MR description for context
            files_context: Optional dict of file paths to full file content for context
            related_files: Optional dict of related files (entities, interfaces) for domain context
            jira_tickets: Optional list of Jira ticket IDs to fetch via MCP
            jira_cloud_id: Atlassian cloud ID for MCP tool calls
            has_markdown_files: True when one or more changed files is markdown (.md / .markdown); triggers prompt-review guidance (the LLM decides per-file whether each is actually a prompt)
            prompt_guide_content: Optional repo prompt-engineering guide content to embed

        Returns:
            List of ReviewComment objects, or None if parsing failed
        """
        prompt = get_review_prompt(
            language,
            diff_content,
            mr_title,
            mr_description,
            files_context,
            related_files,
            jira_tickets,
            jira_cloud_id,
            has_markdown_files=has_markdown_files,
            prompt_guide_content=prompt_guide_content,
        )

        logger.info("Sending diff to Claude for review...")
        try:
            response_text = self._send_prompt(prompt)
        except Exception as e:
            logger.error(f"Failed to get Claude response: {e}")
            return None
        comments = self._parse_response_to_comments(response_text)
        if comments:
            comments = self._verify_comments(comments, diff_content)
        return comments

    def _parse_response_to_comments(self, response_text: str) -> list[ReviewComment] | None:
        """Parse Claude's JSON response into ReviewComments. Return None on JSON failure.

        Volume is not bounded here any more. The per-stage caps this method used to apply are
        gone; what runs is the runaway guard, which trims only a pass that ignored its ranking
        instruction. How much of what was found gets reported is decided once, later, by the
        reduce step's posture — see `mr_reviewer/reduce.py`.
        """
        logger.info("Received response from Claude, parsing comments...")
        try:
            comments_data = self._parse_json_response(response_text)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {e}")
            return None  # None signals parse failure (not "no issues")

        comments = []
        for item in comments_data:
            try:
                comments.append(
                    ReviewComment(
                        file=item["file"],
                        line=int(item["line"]),
                        severity=item.get("severity", "SUGGESTION").upper(),
                        title=item["title"],
                        body=item["body"],
                        stage=item.get("stage", "SENIOR").upper(),
                        kind=item.get("kind", "").lower(),
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning(f"Skipping malformed comment: {e}")
                continue

        comments = _guard_runaway(comments)
        logger.info(f"Review complete: {len(comments)} comment(s) parsed")
        return comments

    def _verify_comments(
        self,
        comments: list[ReviewComment],
        diff_content: str,
    ) -> list[ReviewComment]:
        """
        Second pass: re-evaluate proposed comments against the diff and drop weak findings.

        Trades one extra LLM call for a meaningful reduction in noisy/speculative comments.
        On any failure (parse error, exception), returns the original comments — verification
        is a precision booster, not a gate.
        """
        if not comments:
            return comments

        if not self.enable_verification:
            logger.info(
                f"Verification disabled (pass --enable-verification to turn on); keeping all {len(comments)} comments"
            )
            return comments

        comments_dicts = [
            {
                "stage": c.stage,
                "kind": c.kind,
                "file": c.file,
                "line": c.line,
                "severity": c.severity,
                "title": c.title,
                "body": c.body,
            }
            for c in comments
        ]

        prompt = get_verification_prompt(diff_content, comments_dicts)
        logger.info(f"Verification pass: re-evaluating {len(comments)} comments...")

        try:
            response_text = self._send_prompt(prompt)
        except Exception as e:
            logger.warning(f"Verification pass failed ({e}); keeping original comments")
            return comments

        try:
            kept_data = self._parse_json_response(response_text)
        except json.JSONDecodeError as e:
            logger.warning(
                f"Could not parse verification response ({e}); keeping original comments"
            )
            return comments

        kept: list[ReviewComment] = []
        for item in kept_data:
            try:
                kept.append(
                    ReviewComment(
                        file=item["file"],
                        line=int(item["line"]),
                        severity=item.get("severity", "SUGGESTION").upper(),
                        title=item["title"],
                        body=item["body"],
                        stage=item.get("stage", "SENIOR").upper(),
                        kind=item.get("kind", "").lower(),
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning(f"Skipping malformed verified comment: {e}")
                continue

        dropped = len(comments) - len(kept)
        logger.info(
            f"Verification dropped {dropped} of {len(comments)} comments ({len(kept)} kept)"
        )
        if dropped > 0:
            kept_keys = {(c.file, c.line, c.title) for c in kept}
            for c in comments:
                if (c.file, c.line, c.title) not in kept_keys:
                    logger.info(
                        f"  ✗ dropped [{c.stage}/{c.severity}] {c.file}:{c.line} — {c.title}"
                    )
        return kept

    def generate_summary(
        self,
        comments: list[ReviewComment],
        mr_title: str,
    ) -> str:
        """
        Generate a summary comment for the review.

        Args:
            comments: List of review comments
            mr_title: MR title

        Returns:
            Summary text
        """
        if not comments:
            return (
                "✅ **Code Review Complete**\n\nNo significant issues found. The code looks good!"
            )

        comments_data = [{"severity": c.severity, "title": c.title} for c in comments]

        prompt = get_summary_prompt(comments_data, mr_title)

        try:
            summary = self._send_prompt(prompt, max_tokens=500)

            # Build formatted summary. Design findings are counted separately rather than folded into
            # the warning tally, so the headline reflects that the review contains a different class of
            # feedback — matching the REDESIGN badge each of them carries inline.
            design = [c for c in comments if c.kind == "design"]
            rest = [c for c in comments if c.kind != "design"]
            critical = sum(1 for c in rest if c.severity == "CRITICAL")
            warnings = sum(1 for c in rest if c.severity == "WARNING")
            suggestions = sum(1 for c in rest if c.severity == "SUGGESTION")

            header = "## 🤖 Automated Code Review\n\n"
            counts = [f"{critical} critical", f"{warnings} warnings", f"{suggestions} suggestions"]
            if design:
                counts.append(f"{len(design)} redesign")
            stats = f"**Summary**: {', '.join(counts)}\n\n"

            return f"{header}{stats}{summary}"

        except Exception as e:
            logger.error(f"Failed to generate summary: {e}")
            # Fallback summary
            return f"## 🤖 Automated Code Review\n\nFound {len(comments)} issues to review."

    def _parse_json_response(self, response: str) -> list[dict]:
        """
        Parse JSON from Claude's response, handling markdown code blocks.

        Args:
            response: Raw response text

        Returns:
            Parsed JSON as list of dicts
        """

        def as_list(parsed: object) -> list[dict] | None:
            """Findings arrive as a JSON array; anything else is a failed parse, not a result."""
            return parsed if isinstance(parsed, list) else None

        def try_parse(s: str) -> list[dict] | None:
            """Try parsing JSON, with newline fix if needed."""
            try:
                return as_list(json.loads(s))
            except json.JSONDecodeError:
                # Try fixing unescaped newlines in strings
                fixed = self._fix_json_newlines(s)
                if fixed != s:
                    try:
                        return as_list(json.loads(fixed))
                    except json.JSONDecodeError:
                        pass
            return None

        # Try direct parse first
        result = try_parse(response)
        if result is not None:
            return result

        # Try extracting from markdown code block
        if "```json" in response:
            start = response.find("```json") + 7
            end = response.find("```", start)
            if end > start:
                json_str = response[start:end].strip()
                result = try_parse(json_str)
                if result is not None:
                    return result

        # Try extracting from generic code block
        if "```" in response:
            start = response.find("```") + 3
            # Skip language identifier if present
            newline = response.find("\n", start)
            if newline > start:
                start = newline + 1
            end = response.find("```", start)
            if end > start:
                json_str = response[start:end].strip()
                result = try_parse(json_str)
                if result is not None:
                    return result

        # Try finding array in response
        start = response.find("[")
        end = response.rfind("]") + 1
        if start >= 0 and end > start:
            json_str = response[start:end]
            result = try_parse(json_str)
            if result is not None:
                return result

        raise json.JSONDecodeError("Could not find JSON in response", response, 0)

    def _fix_json_newlines(self, s: str) -> str:
        """
        Fix unescaped newlines inside JSON strings.
        Claude sometimes outputs literal newlines in string values instead of \\n.
        """
        result = []
        in_string = False
        i = 0
        while i < len(s):
            char = s[i]

            if char == '"' and (i == 0 or s[i - 1] != "\\"):
                in_string = not in_string
                result.append(char)
            elif char == "\n" and in_string:
                # Replace literal newline with escaped newline
                result.append("\\n")
            elif char == "\r" and in_string:
                # Skip carriage returns inside strings
                pass
            else:
                result.append(char)

            i += 1

        return "".join(result)

    def request_context_needs(
        self,
        diff_content: str,
        mr_title: str,
        mr_description: str = "",
    ) -> dict:
        """
        First pass: Ask Claude what context files it needs for the review.

        Args:
            diff_content: The diff content to analyze
            mr_title: MR title
            mr_description: MR description

        Returns:
            Dict with 'files_needed' (list of file paths) and 'can_review_without_context' (bool)
        """
        prompt = get_context_request_prompt(diff_content, mr_title, mr_description)

        logger.info("Pass 1: Asking Claude what context is needed...")

        try:
            response_text = self._send_prompt(prompt, max_tokens=1024)
            result = self._parse_context_response(response_text)

            if not isinstance(result, dict):
                logger.warning(f"_parse_context_response returned non-dict: {type(result)}")
                return {"files_needed": [], "can_review_without_context": True}

            files_count = len(result.get("files_needed", []))
            logger.info(f"Claude requested {files_count} file(s) for context")
            return result

        except Exception as e:
            logger.warning(f"Failed to get context needs: {e}")
            # Return empty on failure - will proceed without context
            return {"files_needed": [], "can_review_without_context": True}

    def _parse_context_response(self, response: str) -> dict:
        """
        Parse the context request response from Claude.

        Args:
            response: Raw response text

        Returns:
            Parsed dict with files_needed and can_review_without_context
        """

        def validate_result(data: dict) -> dict:
            """Ensure result has expected structure."""
            if not isinstance(data, dict):
                raise ValueError("Response is not a dict")
            # Normalize files_needed to list of dicts with 'path' key
            files = data.get("files_needed", [])
            if not isinstance(files, list):
                files = []
            normalized = []
            for f in files:
                if isinstance(f, dict) and "path" in f:
                    normalized.append(f)
                elif isinstance(f, str):
                    # Handle case where Claude returns just paths
                    normalized.append({"path": f, "reason": "requested"})
            data["files_needed"] = normalized
            return data

        # Try direct parse first
        try:
            return validate_result(json.loads(response))
        except (json.JSONDecodeError, ValueError):
            pass

        # Try extracting from markdown code block
        if "```" in response:
            # Find JSON block
            for marker in ["```json", "```"]:
                if marker in response:
                    start = response.find(marker) + len(marker)
                    # Skip to next line after marker
                    newline_pos = response.find("\n", start)
                    if newline_pos > start:
                        start = newline_pos + 1
                    end = response.find("```", start)
                    if end > start:
                        json_str = response[start:end].strip()
                        try:
                            return validate_result(json.loads(json_str))
                        except (json.JSONDecodeError, ValueError):
                            pass
                    break  # Only try first code block

        # Try finding JSON object in response
        start = response.find("{")
        end = response.rfind("}") + 1
        if start >= 0 and end > start:
            json_str = response[start:end]
            try:
                return validate_result(json.loads(json_str))
            except (json.JSONDecodeError, ValueError):
                pass

        # Default fallback
        logger.warning(f"Could not parse context response: {response[:200]!r}")
        return {"files_needed": [], "can_review_without_context": True}


class APIReviewer(BaseReviewer):
    """Code reviewer using Anthropic API directly.

    DEPRECATED (diff mode): embeds a truncated diff + fetched context in the prompt and
    has no codebase access. Produces materially weaker structural/craft findings than
    clone mode. Use LocalReviewer (clone mode). Retained only as a fallback; slated for removal.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-4-8",
        max_tokens: int = 4096,
    ):
        """
        Initialize the API reviewer.

        Args:
            api_key: Anthropic API key
            model: Model to use for review
            max_tokens: Maximum output tokens
        """
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.default_max_tokens = max_tokens

    def _send_prompt(self, prompt: str, max_tokens: int = 4096) -> str:
        """Send prompt via Anthropic API."""
        import anthropic

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens or self.default_max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()

        except anthropic.APIError as e:
            logger.error(f"Claude API error: {e}")
            raise


class CLIReviewer(BaseReviewer):
    """Code reviewer using Claude Code CLI (pipe mode, no file access).

    DEPRECATED (diff mode): reviews only the embedded diff and cannot browse the codebase.
    Use LocalReviewer (clone mode), the supported review path. Retained only as a fallback;
    slated for removal.
    """

    def __init__(self, model: str | None = None, effort: str | None = None):
        """
        Initialize the CLI reviewer.

        Args:
            model: Optional model override (e.g., "sonnet", "opus")
            effort: Optional reasoning effort (low|medium|high|xhigh|max)
        """
        self.model = model
        self.effort = effort
        self._verify_claude_cli()

    def _verify_claude_cli(self):
        """Verify that Claude CLI is available."""
        if not shutil.which("claude"):
            raise RuntimeError(
                "Claude Code CLI not found. Please install it first:\n"
                "  npm install -g @anthropic-ai/claude-code\n"
                "Or use the API backend instead."
            )

    def _send_prompt(self, prompt: str, max_tokens: int = 4096) -> str:
        """Send prompt via Claude Code CLI."""
        # Use stdin for prompt to avoid command-line length limits with large diffs
        cmd = ["claude", "-p", "-", "--output-format", "text"]

        if self.model:
            cmd.extend(["--model", self.model])
        if self.effort:
            cmd.extend(["--effort", self.effort])

        logger.debug("Running Claude CLI command...")

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=900,  # 15 minute timeout
            )

            if result.returncode != 0:
                error_msg = result.stderr or result.stdout or "Unknown error"
                logger.error(f"Claude CLI error: {error_msg}")
                raise RuntimeError(f"Claude CLI failed: {error_msg}")

            return result.stdout.strip()

        except subprocess.TimeoutExpired:
            logger.error("Claude CLI timed out")
            raise RuntimeError("Claude CLI request timed out after 15 minutes") from None


# Reasoning effort per pass. Effort multiplies output tokens, so it is spent where a pass has to
# derive a consequence from several facts and withheld where the pass is classification.
#
# The split follows whether a pass has a checkable answer. PO and triage do — a
# criterion is either evidenced or it is not — and a wrong answer there is visible and bounded.
# Craft, ARCHITECT and BLIND DESIGN/compare have no ground truth, and `merge_findings.py` is forbidden
# from judging whether a finding is right, so for those the generating model is the only filter
# and deliberation is the only thing standing between a real smell and confident noise.
#
# SENIOR (craft) sits at medium rather than high on cost grounds alone: it is by far the most
# numerous pass, so its effort is the second-largest lever in the run after invocation count.
# Nobody has yet compared Sonnet-at-high against Opus-at-medium for the same spend; that is the
# first thing the seeded-canary benchmark should settle, and until it does this is a guess.
EFFORT_BY_STAGE: dict[str, str] = {
    "PO": "medium",  # walk the criteria, find the evidence, classify
    "ARCHITECT": "high",  # one call, whole-change shape, most leverage per call
    "SENIOR": "medium",  # the craft pass, and the one that runs many times
    "PROMPT": "medium",
    # Keyed by the labels `run_maker` actually passes. They read "BLIND DESIGN/..." rather than
    # "MAKER/..." because that is what the call sites say, and a key that matches nothing is not a
    # wrong level but no level at all: `_send_prompt` omits the flag and the pass silently inherits
    # the session's. `test_effort.py` walks the call sites so the two cannot drift apart again.
    "BLIND DESIGN/design": "medium",  # generative planning, not review
    "BLIND DESIGN/compare": "high",  # holds two designs side by side and prices the divergence
    "LEARN": "medium",
    "LEARN/comments": "medium",
}

VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# The id only reaches the Anthropic SDK on the deprecated --use-api path; in clone mode it is
# collapsed to the bare `opus`/`sonnet`/`haiku` alias for `claude -p --model`. It is pinned here
# so the three copies that used to sit in main.py's signatures cannot drift apart again.
DEFAULT_MODEL = "claude-opus-5"


class LocalReviewer(BaseReviewer):
    """
    Code reviewer using Claude Code CLI with local file access (clone mode).

    THIS IS THE SUPPORTED, STANDARD REVIEWER. Runs from a cloned repo directory with
    --allowedTools Read,Glob,Grep so Claude can browse the full codebase — required for
    structural and function-level (craft) review quality, and matching local terminal
    review quality. The diff-mode backends (CLIReviewer, APIReviewer) are deprecated.
    """

    def __init__(
        self,
        repo_dir: str,
        model: str | None = None,
        jira_enabled: bool = False,
        effort: str | None = None,
    ):
        """
        Initialize the local reviewer.

        Args:
            repo_dir: Path to the cloned repo directory
            model: Optional model override (e.g., "sonnet", "opus")
            jira_enabled: If True, include Jira MCP tool in allowed tools
        """
        self.repo_dir = str(repo_dir)
        self.model = model
        self.jira_enabled = jira_enabled
        # An explicit override applies to every pass; None means each pass uses EFFORT_BY_STAGE,
        # and a stage absent from that map inherits whatever the ambient session is set to.
        self.effort = effort
        self._verify_claude_cli()

    def _verify_claude_cli(self):
        """Verify that Claude CLI is available."""
        if not shutil.which("claude"):
            raise RuntimeError(
                "Claude Code CLI not found. Please install it first:\n"
                "  npm install -g @anthropic-ai/claude-code\n"
                "Or use the API backend instead."
            )

    def _send_prompt(
        self,
        prompt: str,
        max_tokens: int = 4096,
        timeout_secs: int = 1200,
        cwd: str | None = None,
        effort: str | None = None,
    ) -> str:
        """Send prompt via Claude Code CLI with file access tools.

        ``cwd`` overrides the working directory for this call only. The MAKER pass uses it to run
        against a pre-change worktree, so a single reviewer instance can serve both without any
        shared mutable state to race on across the orchestrator's thread pool.
        """
        allowed_tools = "Read,Glob,Grep"
        if self.jira_enabled:
            allowed_tools += f",{jira_mcp_tool()}"

        cmd = [
            "claude",
            "-p",
            "-",
            "--output-format",
            "text",
            "--allowedTools",
            allowed_tools,
        ]

        if self.model:
            cmd.extend(["--model", self.model])
        level = self.effort or effort
        if level:
            cmd.extend(["--effort", level])

        run_dir = cwd or self.repo_dir
        logger.debug(f"Running Claude CLI with file access from {run_dir}")

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout_secs,
                cwd=run_dir,
            )

            if result.returncode != 0:
                error_msg = result.stderr or result.stdout or "Unknown error"
                logger.error(f"Claude CLI error: {error_msg}")
                raise RuntimeError(f"Claude CLI failed: {error_msg}")

            return result.stdout.strip()

        except subprocess.TimeoutExpired:
            logger.error(f"Claude CLI timed out after {timeout_secs}s")
            raise RuntimeError(
                f"Claude CLI request timed out after {timeout_secs} seconds"
            ) from None

    def review_diff(
        self,
        diff_content: str,
        language: str,
        mr_title: str,
        mr_description: str = "",
        files_context: dict[str, str] | None = None,
        related_files: dict[str, str] | None = None,
        jira_tickets: list[str] | None = None,
        jira_cloud_id: str = "",
        has_markdown_files: bool = False,
        prompt_guide_content: str | None = None,
        focus_stage: str | None = None,
        focus_note: str = "",
        timeout_secs: int = 1200,
    ) -> list[ReviewComment] | None:
        """
        Review a diff with full repo access.

        Builds a prompt that tells Claude to browse files via Read/Glob/Grep tools
        instead of embedding context files. files_context, related_files, and
        prompt_guide_content are ignored because Claude reads them on demand.

        When ``focus_stage`` is set ("PO" | "ARCHITECT" | "SENIOR") the call is scoped to a
        single review stage — used by the orchestrated team (see orchestrator.py) so each
        persona / file-batch runs in its own focused call. ``focus_note`` adds per-pass
        guidance (e.g. batch description). Verification is skipped for focused passes; the
        orchestrator's reduce step handles dedup/precision across passes.
        """
        from .prompts import get_local_review_prompt

        prompt = get_local_review_prompt(
            language=language,
            diff_content=diff_content,
            mr_title=mr_title,
            mr_description=mr_description,
            jira_tickets=jira_tickets if self.jira_enabled else None,
            jira_cloud_id=jira_cloud_id if self.jira_enabled else "",
            has_markdown_files=has_markdown_files,
            focus_stage=focus_stage,
            focus_note=focus_note,
        )

        label = f"[{focus_stage}] " if focus_stage else ""
        logger.info(f"{label}Sending review prompt with file access to Claude...")
        try:
            response_text = self._send_prompt(
                prompt, timeout_secs=timeout_secs, effort=EFFORT_BY_STAGE.get(focus_stage or "")
            )
        except Exception as e:
            logger.error(f"{label}Failed to get Claude response: {e}")
            return None
        comments = self._parse_response_to_comments(response_text)
        # For focused team passes, skip the per-call verification — the orchestrator's
        # reduce step owns dedup/precision across all passes.
        if comments and not focus_stage:
            comments = self._verify_comments(comments, diff_content)
        return comments

    def run_text_prompt(
        self,
        prompt: str,
        cwd: str | None = None,
        timeout_secs: int = 1200,
        label: str = "",
    ) -> str | None:
        """Run a prompt that returns prose rather than review JSON. None on failure.

        Used for the MAKER design phase, whose output is a markdown design sketch fed into its own
        second phase — not comments to post. Kept generic so the orchestrator owns the choreography
        and this class stays a thin wrapper over the CLI.
        """
        tag = f"[{label}] " if label else ""
        try:
            text = self._send_prompt(
                prompt, cwd=cwd, timeout_secs=timeout_secs, effort=EFFORT_BY_STAGE.get(label)
            )
        except Exception as e:
            logger.error(f"{tag}Claude call failed: {e}")
            return None
        if not text:
            logger.warning(f"{tag}Claude returned an empty response")
            return None
        return text

    def review_with_prompt(
        self,
        prompt: str,
        cwd: str | None = None,
        timeout_secs: int = 1200,
        label: str = "",
    ) -> list[ReviewComment] | None:
        """Run a fully-built review prompt and parse the JSON comments out of it.

        The orchestrator uses this for passes whose prompt it assembles itself (MAKER), rather than
        going through ``review_diff``'s stage-scoped prompt builder. Verification is skipped here
        for the same reason it is skipped for focused passes: the reduce step owns cross-pass
        precision.
        """
        text = self.run_text_prompt(prompt, cwd=cwd, timeout_secs=timeout_secs, label=label)
        if text is None:
            return None
        return self._parse_response_to_comments(text)

    def audit_repo(
        self,
        language: str,
        repo_name: str,
        branch: str,
        scope: str = "",
        timeout_secs: int = 1800,
    ) -> str:
        """
        Audit the entire repo and return a markdown report.

        Unlike review_diff (which produces JSON inline comments), this returns
        the raw markdown produced by Claude — the audit prompt instructs the
        model to emit a structured Markdown document.

        Args:
            language: Dominant language of the repo (java, angular, etc.)
            repo_name: Display name for the report header
            branch: Branch being audited (for the report header)
            scope: Optional subtree to restrict the audit to (relative to repo root)
            timeout_secs: Subprocess timeout (audits take longer than diff reviews)

        Returns:
            Markdown report string
        """
        prompt = get_audit_prompt(
            language=language,
            repo_name=repo_name,
            branch=branch,
            scope=scope,
        )
        logger.info(
            f"Auditing repo '{repo_name}' (branch: {branch}, lang: {language}{', scope: ' + scope if scope else ''})..."
        )
        return self._send_prompt(prompt, timeout_secs=timeout_secs, effort="high")


# Factory function for creating reviewers
def Reviewer(
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    use_cli: bool = False,
    repo_dir: str | None = None,
    jira_enabled: bool = False,
    enable_verification: bool = False,
    effort: str | None = None,
    **kwargs,
) -> BaseReviewer:
    """
    Factory function to create the appropriate reviewer.

    Args:
        api_key: Anthropic API key (required for API backend)
        model: Model to use
        use_cli: If True, use Claude Code CLI instead of API
        repo_dir: If provided, use LocalReviewer with file access from this directory
        jira_enabled: If True, allow MCP Jira tool in CLI reviewers
        effort: Reasoning effort override for every pass; None lets each pass use
            ``EFFORT_BY_STAGE``, and a pass absent from that map inherits the session level

    Returns:
        Configured reviewer instance
    """
    if effort and effort not in VALID_EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(VALID_EFFORTS)}, got {effort!r}")

    # Map full model names to CLI model shortcuts
    cli_model = None
    if "opus" in model.lower():
        cli_model = "opus"
    elif "sonnet" in model.lower():
        cli_model = "sonnet"
    elif "haiku" in model.lower():
        cli_model = "haiku"

    reviewer: BaseReviewer
    if repo_dir:
        reviewer = LocalReviewer(
            repo_dir=repo_dir, model=cli_model, jira_enabled=jira_enabled, effort=effort
        )
    elif use_cli:
        reviewer = CLIReviewer(model=cli_model, effort=effort)
    else:
        if not api_key:
            raise ValueError(
                "API key required for API backend. Use --use-cli flag for CLI backend."
            )
        reviewer = APIReviewer(api_key=api_key, model=model, **kwargs)
    reviewer.enable_verification = enable_verification
    return reviewer


def chunk_diff(diff_content: str, max_lines: int = 3000) -> list[str]:
    """
    Split a large diff into smaller chunks for processing.

    Args:
        diff_content: Full diff content
        max_lines: Maximum lines per chunk

    Returns:
        List of diff chunks
    """
    lines = diff_content.split("\n")

    if len(lines) <= max_lines:
        return [diff_content]

    chunks: list[str] = []
    current_chunk: list[str] = []

    for line in lines:
        # Track file headers
        if line.startswith("diff --git") or line.startswith("--- ") or line.startswith("+++ "):
            # Start of a new file: close the chunk first if it is already over the budget.
            if line.startswith("diff --git") and len(current_chunk) > max_lines:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
            current_chunk.append(line)
        else:
            current_chunk.append(line)

    # Add remaining chunk
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks
