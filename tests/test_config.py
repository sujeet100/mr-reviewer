"""Organisation-specific settings come from config, never from literals in the code."""

from unittest.mock import MagicMock

import pytest

from mr_reviewer import config, telemetry
from mr_reviewer.main import extract_jira_tickets

JIRA_VARS = (
    "JIRA_SITE",
    "JIRA_CLOUD_ID",
    "JIRA_ENABLED",
    "JIRA_MCP_TOOL",
    "JIRA_PROJECT_KEYS",
    "GITLAB_URL",
    "GITLAB_DEFAULT_GROUP",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # delenv records the absence, so anything load_config publishes is removed on teardown.
    for var in (*JIRA_VARS, "MR_REVIEWER_RECORD_RUNS"):
        monkeypatch.delenv(var, raising=False)


def write_config(tmp_path, body):
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return str(path)


def test_any_ticket_shape_matches_without_project_keys():
    assert extract_jira_tickets("PAY-12: fix", "uses SHA-256") == ["PAY-12", "SHA-256"]


def test_project_keys_restrict_what_counts_as_a_ticket(monkeypatch):
    monkeypatch.setenv("JIRA_PROJECT_KEYS", "pay, core")
    assert extract_jira_tickets("PAY-12: fix", "uses SHA-256, see CORE-3") == ["PAY-12", "CORE-3"]


def test_defaults_carry_no_organisation(tmp_path):
    cfg = config.load_config(write_config(tmp_path, "{}"))
    assert cfg.jira.cloud_id == ""
    assert cfg.review.default_group == ""
    assert cfg.jira.mcp_tool == config.DEFAULT_JIRA_MCP_TOOL


def test_file_jira_settings_reach_the_prompt_builders(tmp_path):
    path = write_config(
        tmp_path,
        "jira:\n  site: acme.atlassian.net\n  project_keys: [PAY]\n"
        "  mcp_tool: mcp__claude_ai_Atlassian_2__getJiraIssue\n",
    )
    cfg = config.load_config(path)
    assert cfg.jira.cloud_id == "acme.atlassian.net"
    assert config.jira_project_keys() == ["PAY"]
    assert config.jira_mcp_tool() == "mcp__claude_ai_Atlassian_2__getJiraIssue"


def test_environment_beats_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_MCP_TOOL", "mcp__other__getIssue")
    monkeypatch.setenv("JIRA_SITE", "env.atlassian.net")
    path = write_config(
        tmp_path, "jira:\n  site: file.atlassian.net\n  mcp_tool: mcp__file__getIssue\n"
    )
    cfg = config.load_config(path)
    assert cfg.jira.cloud_id == "env.atlassian.net"
    assert config.jira_mcp_tool() == "mcp__other__getIssue"


def test_run_records_are_opt_in():
    gitlab = MagicMock()
    assert telemetry.record_local_run(gitlab, "g/r", 1, [], "abc") is False
    gitlab.post_note.assert_not_called()


def test_run_records_post_when_enabled(monkeypatch):
    monkeypatch.setenv("MR_REVIEWER_RECORD_RUNS", "1")
    gitlab = MagicMock()
    gitlab.get_mr_notes.return_value = []
    assert telemetry.record_local_run(gitlab, "g/r", 1, [], "abc") is True
    gitlab.post_note.assert_called_once()
