"""
Configuration handling for MR Reviewer.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load .env file from current directory or parent directories
load_dotenv()


@dataclass
class GitLabConfig:
    """GitLab connection configuration."""

    url: str = "https://gitlab.com"
    token: str = ""


@dataclass
class AnthropicConfig:
    """Anthropic API configuration."""

    api_key: str = ""
    model: str = "claude-opus-5"
    # None means each pass uses reviewer.EFFORT_BY_STAGE; a value overrides every pass.
    effort: str = ""


@dataclass
class ReviewConfig:
    """Review behavior configuration."""

    max_files: int = 50
    max_diff_lines: int = 3000
    max_parallel: int = 4
    reviewer_username: str = ""
    projects: list[str] = field(default_factory=list)
    # Parent GitLab group to auto-prepend to short project paths
    # (e.g. "team/foo" → "my-group/team/foo"). Paths that already start
    # with this prefix pass through unchanged. Set to "" to disable.
    default_group: str = ""
    checks: list[str] = field(
        default_factory=lambda: [
            "code_smells",
            "design_patterns",
            "potential_bugs",
            "complexity",
            "readability",
            "modern_constructs",
            "duplication",
        ]
    )


@dataclass
class JavaConfig:
    """Java-specific configuration."""

    min_version: int = 17
    frameworks: list[str] = field(default_factory=lambda: ["spring-boot"])


@dataclass
class AngularConfig:
    """Angular-specific configuration."""

    typescript_strict: bool = True


# The tool name a claude.ai Atlassian connector exposes. Connector names vary by account (a second
# Atlassian connection is suffixed, e.g. ``mcp__claude_ai_Atlassian_2__getJiraIssue``), which is why
# this is configurable rather than fixed.
DEFAULT_JIRA_MCP_TOOL = "mcp__claude_ai_Atlassian__getJiraIssue"


@dataclass
class JiraConfig:
    """Jira integration configuration (via an Atlassian MCP connector).

    Everything organisation-specific about Jira lives here, so pointing the reviewer at a new
    company is a config change rather than a code change.
    """

    # Your Atlassian site or cloud id, e.g. "your-company.atlassian.net". Empty disables Jira.
    cloud_id: str = ""
    enabled: bool = True
    # Project keys a ticket reference may use, e.g. ["PAY", "CORE"]. Empty accepts any
    # ``ABC-123`` shape, which also matches things like ``SHA-256`` in an MR description.
    project_keys: list[str] = field(default_factory=list)
    mcp_tool: str = DEFAULT_JIRA_MCP_TOOL


@dataclass
class Config:
    """Main configuration container."""

    gitlab: GitLabConfig = field(default_factory=GitLabConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    java: JavaConfig = field(default_factory=JavaConfig)
    angular: AngularConfig = field(default_factory=AngularConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)


def load_config(config_path: str | None = None) -> Config:
    """
    Load configuration from file and environment variables.

    Priority (highest to lowest):
    1. Environment variables
    2. Config file
    3. Default values

    Args:
        config_path: Optional path to config file

    Returns:
        Config object
    """
    config = Config()

    # Try to load config file
    if config_path:
        config_file = Path(config_path)
    else:
        # Look for config in standard locations
        search_paths = [
            Path.cwd() / "config.yaml",
            Path.cwd() / "config.yml",
            Path.cwd() / ".mr-reviewer.yaml",
            Path.home() / ".config" / "mr-reviewer" / "config.yaml",
        ]
        config_file = None
        for path in search_paths:
            if path.exists():
                config_file = path
                break

    if config_file and config_file.exists():
        with open(config_file) as f:
            file_config = yaml.safe_load(f) or {}

        # Apply file config
        if "gitlab" in file_config:
            gitlab_cfg = file_config["gitlab"]
            config.gitlab.url = gitlab_cfg.get("url", config.gitlab.url)
            config.gitlab.token = gitlab_cfg.get("token", config.gitlab.token)

        if "anthropic" in file_config:
            anthropic_cfg = file_config["anthropic"]
            config.anthropic.api_key = anthropic_cfg.get("api_key", config.anthropic.api_key)
            config.anthropic.model = anthropic_cfg.get("model", config.anthropic.model)
            config.anthropic.effort = anthropic_cfg.get("effort", config.anthropic.effort)

        if "review" in file_config:
            review_cfg = file_config["review"]
            config.review.max_files = review_cfg.get("max_files", config.review.max_files)
            config.review.max_diff_lines = review_cfg.get(
                "max_diff_lines", config.review.max_diff_lines
            )
            config.review.max_parallel = review_cfg.get("max_parallel", config.review.max_parallel)
            config.review.reviewer_username = review_cfg.get(
                "reviewer_username", config.review.reviewer_username
            )
            config.review.default_group = review_cfg.get(
                "default_group", config.review.default_group
            )
            if "projects" in review_cfg:
                config.review.projects = list(review_cfg["projects"])
            if "checks" in review_cfg:
                config.review.checks = review_cfg["checks"]

        if "java" in file_config:
            java_cfg = file_config["java"]
            config.java.min_version = java_cfg.get("min_version", config.java.min_version)
            if "frameworks" in java_cfg:
                config.java.frameworks = java_cfg["frameworks"]

        if "angular" in file_config:
            angular_cfg = file_config["angular"]
            config.angular.typescript_strict = angular_cfg.get(
                "typescript_strict", config.angular.typescript_strict
            )

        if "jira" in file_config:
            jira_cfg = file_config["jira"]
            # `site` is the friendlier name; `cloud_id` is kept because it is what the MCP call takes.
            config.jira.cloud_id = jira_cfg.get(
                "site", jira_cfg.get("cloud_id", config.jira.cloud_id)
            )
            config.jira.enabled = jira_cfg.get("enabled", config.jira.enabled)
            config.jira.mcp_tool = jira_cfg.get("mcp_tool", config.jira.mcp_tool)
            if "project_keys" in jira_cfg:
                config.jira.project_keys = [str(k) for k in jira_cfg["project_keys"] or []]

    # Override with environment variables
    if os.environ.get("GITLAB_URL"):
        config.gitlab.url = os.environ["GITLAB_URL"]
    if os.environ.get("GITLAB_TOKEN"):
        config.gitlab.token = os.environ["GITLAB_TOKEN"]
    if os.environ.get("MR_REVIEW_BOT_TOKEN"):
        config.gitlab.token = os.environ["MR_REVIEW_BOT_TOKEN"]

    if os.environ.get("ANTHROPIC_API_KEY"):
        config.anthropic.api_key = os.environ["ANTHROPIC_API_KEY"]
    if os.environ.get("ANTHROPIC_MODEL"):
        config.anthropic.model = os.environ["ANTHROPIC_MODEL"]
    if os.environ.get("MR_REVIEWER_EFFORT"):
        config.anthropic.effort = os.environ["MR_REVIEWER_EFFORT"]

    if os.environ.get("GITLAB_DEFAULT_GROUP") is not None:
        config.review.default_group = os.environ["GITLAB_DEFAULT_GROUP"]

    if os.environ.get("JIRA_SITE"):
        config.jira.cloud_id = os.environ["JIRA_SITE"]
    if os.environ.get("JIRA_CLOUD_ID"):
        config.jira.cloud_id = os.environ["JIRA_CLOUD_ID"]
    if os.environ.get("JIRA_ENABLED"):
        config.jira.enabled = os.environ["JIRA_ENABLED"].lower() in ("true", "1", "yes")
    if os.environ.get("JIRA_MCP_TOOL"):
        config.jira.mcp_tool = os.environ["JIRA_MCP_TOOL"]
    if os.environ.get("JIRA_PROJECT_KEYS"):
        config.jira.project_keys = _split_keys(os.environ["JIRA_PROJECT_KEYS"])

    # The tool name and project keys are read deep inside the prompt builders, which never see
    # this object. Publishing them to the environment matches how --enable-maker, --learn and
    # --brutal already reach those layers; setdefault keeps a real environment variable winning.
    os.environ.setdefault("JIRA_MCP_TOOL", config.jira.mcp_tool)
    if config.jira.project_keys:
        os.environ.setdefault("JIRA_PROJECT_KEYS", ",".join(config.jira.project_keys))

    # Expand any ${VAR} references in token/key values
    config.gitlab.token = _expand_env_vars(config.gitlab.token)
    config.anthropic.api_key = _expand_env_vars(config.anthropic.api_key)

    return config


def _split_keys(value: str) -> list[str]:
    return [k.strip().upper() for k in value.split(",") if k.strip()]


def jira_mcp_tool() -> str:
    """The MCP tool that fetches one Jira issue (``JIRA_MCP_TOOL``, or ``jira.mcp_tool``)."""
    return os.environ.get("JIRA_MCP_TOOL") or DEFAULT_JIRA_MCP_TOOL


def jira_project_keys() -> list[str]:
    """Project keys a ticket reference may use (``JIRA_PROJECT_KEYS``); empty accepts any."""
    return _split_keys(os.environ.get("JIRA_PROJECT_KEYS", ""))


def _expand_env_vars(value: str) -> str:
    """
    Expand ${VAR} references in a string.

    Args:
        value: String potentially containing ${VAR} references

    Returns:
        String with environment variables expanded
    """
    if not value:
        return value

    import re

    pattern = r"\$\{([^}]+)\}"

    def replace(match):
        var_name = match.group(1)
        return os.environ.get(var_name, "")

    return re.sub(pattern, replace, value)


def validate_config(config: Config, require_anthropic_key: bool = False) -> list[str]:
    """
    Validate configuration and return list of errors.

    Args:
        config: Configuration to validate
        require_anthropic_key: If True, require Anthropic API key (for API backend)

    Returns:
        List of error messages (empty if valid)
    """
    errors = []

    if not config.gitlab.token:
        errors.append("GitLab token not configured. Set GITLAB_TOKEN environment variable.")

    if require_anthropic_key and not config.anthropic.api_key:
        errors.append(
            "Anthropic API key not configured. Set ANTHROPIC_API_KEY environment variable."
        )

    return errors
