"""Every charter file either consumer names must exist.

The charter is shared by the CLI and the skill and is referenced by *name* from both — a
`_load_prompt("java")` here, a `charter/function_review.md` in a skill instruction there. A
typo or a rename therefore fails at runtime, mid-review, in whichever pass happened to need
that file, which is the worst possible moment and place to discover it. These tests turn that
into an import-time failure instead.
"""

import re
from pathlib import Path

import pytest

from mr_reviewer import prompts

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "mr-review"
CHARTER = PLUGIN / "charter"
SKILLS = sorted((PLUGIN / "skills").glob("*/SKILL.md"))


def test_the_charter_is_where_both_consumers_look():
    assert prompts.charter_dir() == CHARTER


def test_every_prompt_the_python_loads_exists():
    """Covers the `_load_prompt("name")` literals across the package."""
    names = set()
    for source in (ROOT / "mr_reviewer").glob("*.py"):
        names |= set(re.findall(r'_load_prompt\(\s*"([a-z_]+)"', source.read_text()))
    assert names, "no _load_prompt calls found — has the loader been renamed?"
    missing = sorted(n for n in names if not (CHARTER / f"{n}.md").is_file())
    assert not missing, f"charter files referenced by the Python but absent: {missing}"


def test_every_language_alias_resolves():
    """`get_language_prompt` builds the filename from a map, so the map needs checking too."""
    for alias in (
        "java",
        "angular",
        "typescript",
        "react",
        "jsx",
        "tsx",
        "python",
        "py",
        "generic",
        "something-unknown",
    ):
        assert prompts.get_language_prompt(alias), f"{alias} produced an empty prompt"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_every_charter_file_the_skill_names_exists(skill):
    """Any `*.md` the skill mentions must be a real charter file.

    Placeholders like `<language>.md` are excluded — the skill substitutes those from the
    language map, which `test_every_language_alias_resolves` covers.
    """
    text = skill.read_text()
    named = set(re.findall(r"([a-z_]+\.md)", text))
    named -= {"skill.md"}
    missing = sorted(n for n in named if not (CHARTER / n).is_file())
    assert not missing, f"{skill.parent.name} names charter files that do not exist: {missing}"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_skill_name_matches_its_directory(skill):
    """Claude Code resolves a skill by its frontmatter name; a mismatch is an invisible break."""
    first = skill.read_text().split("---")[1]
    declared = re.search(r"^name:\s*(\S+)", first, re.M)
    assert declared, f"{skill} has no name in its frontmatter"
    assert declared.group(1) == skill.parent.name


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_skill_scripts_exist(skill):
    """The skill shells out to scripts by path; those have to be there."""
    text = skill.read_text()
    for script in set(re.findall(r"scripts/([\w.]+\.(?:py|sh))", text)):
        assert (PLUGIN / "scripts" / script).is_file(), f"missing script: {script}"


def test_manifest_versions_agree():
    """A marketplace entry disagreeing with plugin.json installs the wrong thing silently."""
    import json

    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    entry = next(p for p in market["plugins"] if p["name"] == plugin["name"])
    assert entry["version"] == plugin["version"]
    assert entry["source"].endswith(PLUGIN.name)


def test_stage_files_are_spliced_into_the_base_prompt():
    """The three stage bodies live in their own files so the skill's wide passes can read only
    theirs; the CLI must still get one assembled rubric with the stages in order and no marker."""
    body = prompts.get_base_prompt()
    assert "<!-- STAGES -->" not in body
    positions = [
        body.index(h)
        for h in (
            "## STAGE 1",
            "## STAGE 2",
            "## STAGE 3",
            "## LANGUAGE GUIDANCE",
            "## CLEAN-CODE CHARTER",
            "# REVIEW WRITING STYLE",
        )
    ]
    assert positions == sorted(positions), (
        "stages, then the clean-code charter, then how to write the finding"
    )
    for name in prompts._STAGE_FILES:
        assert (CHARTER / f"{name}.md").is_file(), f"{name}.md is missing"


def test_every_lens_the_mapper_can_name_exists():
    """`changed_functions.py` maps risk tags to lens files by name; a missing one fails mid-review."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "changed_functions", PLUGIN / "scripts" / "changed_functions.py"
    )
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["changed_functions"] = module
    spec.loader.exec_module(module)
    missing = sorted(
        lens for lens in set(module._LENS_FILE.values()) if not (CHARTER / lens).is_file()
    )
    assert not missing, f"lens files named by the mapper but absent: {missing}"
