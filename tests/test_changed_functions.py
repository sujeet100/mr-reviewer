"""Scope resolution and unit mapping, against a real git repository.

The span scanner has its own fixtures (`changed_functions.py --self-test`), which need no git.
What that cannot cover is the part that talks to git: which diff a scope turns into, and
whether uncommitted work is included. Those are the cases the skill depends on and the ones
most likely to break quietly, since every scope returns *something* plausible.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "mr-review"
    / "scripts"
    / "changed_functions.py"
)


@pytest.fixture(scope="module")
def cf():
    """Load the script as a module.

    It must be registered in ``sys.modules`` *before* it executes: ``Unit`` is a dataclass in a
    module using postponed annotations, and dataclass field resolution looks the defining module
    up by name to evaluate them.
    """
    spec = importlib.util.spec_from_file_location("changed_functions", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["changed_functions"] = module
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


ORIGINAL = """\
def alpha(x):
    return x + 1


def beta(y):
    return y * 2
"""

# alpha gains a guard clause; beta is untouched; gamma is new.
EDITED = """\
def alpha(x):
    if x is None:
        return 0
    return x + 1


def beta(y):
    return y * 2


def gamma(z):
    return z - 1
"""


@pytest.fixture
def repo(tmp_path):
    """A repo with `main` holding ORIGINAL, and a feature branch whose work is uncommitted."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Test")
    (r / "mod.py").write_text(ORIGINAL)
    git(r, "add", "mod.py")
    git(r, "commit", "-q", "-m", "initial")
    git(r, "checkout", "-q", "-b", "feature")
    (r / "mod.py").write_text(EDITED)
    return r


def symbols(units):
    return sorted(u.symbol for u in units if u.symbol)


def test_default_scope_includes_uncommitted_work(cf, repo):
    """The default has to reach the working tree — reviewing before the commit is the point."""
    scope, label = cf.resolve_scope(repo, None, None, staged=False, uncommitted=False)
    units = cf.collect(repo, scope, None)
    assert symbols(units) == ["alpha", "gamma"]
    assert "working tree" in label


def test_beta_is_not_reviewed(cf, repo):
    """An untouched function must not become a review unit, however near the edits it sits."""
    scope, _ = cf.resolve_scope(repo, None, None, staged=False, uncommitted=False)
    assert "beta" not in symbols(cf.collect(repo, scope, None))


def test_staged_scope_sees_only_what_is_staged(cf, repo):
    scope, label = cf.resolve_scope(repo, None, None, staged=True, uncommitted=False)
    assert cf.collect(repo, scope, None) == []
    assert label == "staged changes"

    git(repo, "add", "mod.py")
    assert symbols(cf.collect(repo, scope, None)) == ["alpha", "gamma"]


def test_uncommitted_scope_empties_once_committed(cf, repo):
    git(repo, "add", "mod.py")
    git(repo, "commit", "-q", "-m", "work")
    scope, _ = cf.resolve_scope(repo, None, None, staged=False, uncommitted=True)
    assert cf.collect(repo, scope, None) == []


def test_branch_scope_survives_the_commit(cf, repo):
    """Committing must not shrink the default scope — it is the branch's work, not the diff."""
    git(repo, "add", "mod.py")
    git(repo, "commit", "-q", "-m", "work")
    scope, _ = cf.resolve_scope(repo, None, None, staged=False, uncommitted=False)
    assert symbols(cf.collect(repo, scope, None)) == ["alpha", "gamma"]


def test_explicit_committed_range(cf, repo):
    git(repo, "add", "mod.py")
    git(repo, "commit", "-q", "-m", "work")
    scope, label = cf.resolve_scope(repo, "HEAD~1", "HEAD", staged=False, uncommitted=False)
    assert symbols(cf.collect(repo, scope, None)) == ["alpha", "gamma"]
    assert label == "committed range HEAD~1..HEAD"


def test_trunk_falls_back_to_local_main(cf, repo):
    """No remote here, so resolution must fall through origin/* to the local branch."""
    assert cf._trunk(repo) == "main"


def test_no_trunk_degrades_to_uncommitted(cf, tmp_path):
    """A repo with no recognisable trunk still reviews something rather than failing."""
    r = tmp_path / "odd"
    r.mkdir()
    git(r, "init", "-q", "-b", "wip-only")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Test")
    (r / "mod.py").write_text(ORIGINAL)
    git(r, "add", "mod.py")
    git(r, "commit", "-q", "-m", "initial")
    (r / "mod.py").write_text(EDITED)

    scope, label = cf.resolve_scope(r, None, None, staged=False, uncommitted=False)
    assert cf._trunk(r) is None
    assert "no trunk branch" in label
    assert symbols(cf.collect(r, scope, None)) == ["alpha", "gamma"]


def test_non_code_files_are_skipped(cf, repo):
    (repo / "data.json").write_text('{"a": 1}\n')
    (repo / "notes.txt").write_text("hello\n")
    git(repo, "add", "-A")
    scope, _ = cf.resolve_scope(repo, None, None, staged=False, uncommitted=False)
    files = {u.file for u in cf.collect(repo, scope, None)}
    assert files == {"mod.py"}


# --- classification, ordering and batching --------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/test/java/com/x/FooTest.java", True),
        ("src/intTest/java/com/x/FooIT.java", True),
        ("src/app/thing.spec.ts", True),
        ("src/app/thing.test.tsx", True),
        ("tests/test_thing.py", True),
        ("app/test_thing.py", True),
        ("__tests__/thing.js", True),
        ("src/main/java/com/x/Foo.java", False),
        ("src/app/thing.component.ts", False),
        ("mr_reviewer/prompts.py", False),
        # A production class whose name merely contains "test" is not a test file.
        ("src/main/java/com/x/LatestPriceService.java", False),
    ],
)
def test_test_paths_are_classified(cf, path, expected):
    assert bool(cf._TEST_PATH.search(path)) is expected


def test_production_units_sort_before_tests(cf, repo):
    """The defect this fixes: tests are verbose, so ranking by volume buried production code."""
    (repo / "mod_test.py").write_text(EDITED + "\n\ndef extra_test():\n    " + "pass\n    " * 40)
    git(repo, "add", "-A")
    scope, _ = cf.resolve_scope(repo, None, None, staged=False, uncommitted=False)
    units = cf.collect(repo, scope, None)
    first_test = next(i for i, u in enumerate(units) if u.is_test)
    assert all(not u.is_test for u in units[:first_test])
    assert all(u.is_test for u in units[first_test:])


def unit(cf, file, symbol, changed, is_test=False):
    return cf.Unit(file, "java", symbol, 1, 10, list(range(changed)), changed, 500, is_test)


def test_batches_never_span_files(cf):
    units = [unit(cf, "A.java", f"a{i}", 5) for i in range(3)] + [
        unit(cf, "B.java", f"b{i}", 5) for i in range(3)
    ]
    for b in cf.batch_units(units):
        assert len({u.file for u in b.units}) == 1


def test_batch_respects_the_unit_cap(cf):
    units = [unit(cf, "A.java", f"a{i}", 5) for i in range(9)]
    batches = cf.batch_units(units, max_units=4)
    assert [len(b.units) for b in batches] == [4, 4, 1]


def test_a_large_unit_reviews_alone(cf):
    units = [
        unit(cf, "A.java", "small", 5),
        unit(cf, "A.java", "huge", cf.SOLO_CHANGED_LINES + 1),
        unit(cf, "A.java", "also_small", 5),
    ]
    batches = cf.batch_units(units)
    huge = [b for b in batches if any(u.symbol == "huge" for u in b.units)]
    assert len(huge) == 1 and len(huge[0].units) == 1


def test_budget_prefers_production_but_reserves_a_test_slot(cf):
    batches = cf.batch_units(
        [unit(cf, f"P{i}.java", "p", 50) for i in range(10)]
        + [unit(cf, f"T{i}Test.java", "t", 90, is_test=True) for i in range(10)]
    )
    selected, skipped = cf.select_batches(batches, budget=8)
    assert len(selected) == 8
    assert len(skipped) == 12
    n_tests = sum(1 for b in selected if b.is_test)
    assert n_tests == 2, "expected 25% of an 8-slot budget reserved for tests"
    assert sum(1 for b in selected if not b.is_test) == 6


def test_budget_gives_spare_slots_back_to_tests(cf):
    """With only two production batches, tests should take the rest — not sit idle at 25%."""
    batches = cf.batch_units(
        [unit(cf, f"P{i}.java", "p", 50) for i in range(2)]
        + [unit(cf, f"T{i}Test.java", "t", 50, is_test=True) for i in range(10)]
    )
    selected, _ = cf.select_batches(batches, budget=8)
    assert sum(1 for b in selected if b.is_test) == 6


def test_budget_of_zero_is_unlimited(cf):
    batches = cf.batch_units([unit(cf, f"P{i}.java", "p", 5) for i in range(40)])
    selected, skipped = cf.select_batches(batches, budget=0)
    assert len(selected) == 40 and skipped == []


def test_nothing_is_lost_between_selected_and_skipped(cf):
    batches = cf.batch_units([unit(cf, f"P{i}.java", "p", 5) for i in range(30)])
    selected, skipped = cf.select_batches(batches, budget=7)
    assert len(selected) + len(skipped) == len(batches)


# --- triage selection -----------------------------------------------------------------


def test_unit_ids_are_stable_across_runs(cf, repo):
    scope, _ = cf.resolve_scope(repo, None, None, staged=False, uncommitted=False)
    first = [u.id for u in cf.collect(repo, scope, None)]
    second = [u.id for u in cf.collect(repo, scope, None)]
    assert first == second
    assert all(":" in i and "-" in i for i in first)


def test_select_by_id_keeps_the_given_order(cf):
    """Triage ranks by risk; re-sorting by volume here would discard that judgement."""
    units = [unit(cf, "A.java", "big", 90), unit(cf, "B.java", "small", 2)]
    kept, unknown = cf.select_by_id(units, [units[1].id, units[0].id])
    assert [u.symbol for u in kept] == ["small", "big"]
    assert unknown == []


def test_select_by_id_reports_unknown_ids(cf):
    units = [unit(cf, "A.java", "a", 5)]
    kept, unknown = cf.select_by_id(units, [units[0].id, "made/Up.java:1-2"])
    assert [u.symbol for u in kept] == ["a"]
    assert unknown == ["made/Up.java:1-2"]


def test_batching_preserves_triage_order_when_asked(cf):
    """A 2-line unit triage ranked first must batch ahead of a 90-line one."""
    units = [unit(cf, "B.java", "small", 2), unit(cf, "A.java", "big", 90)]
    ordered = cf.batch_units(units, preserve_order=True)
    assert [b.files[0] for b in ordered] == ["B.java", "A.java"]
    # Without the flag, volume wins — which is the fallback behaviour.
    assert [b.files[0] for b in cf.batch_units(units)] == ["A.java", "B.java"]


# --- fit-to-budget: cover everything by growing batches ------------------------------


def test_fit_covers_every_unit(cf):
    """The point of --fit: nothing is dropped, however tight the budget."""
    units = [unit(cf, f"pkg/F{i}.java", f"m{i}", 10) for i in range(30)]
    for budget in (1, 3, 7, 30, 100):
        batches = cf.fit_to_budget(units, budget)
        covered = [u.id for b in batches for u in b.units]
        assert sorted(covered) == sorted(u.id for u in units), f"lost units at budget {budget}"


def test_fit_respects_the_budget(cf):
    units = [unit(cf, f"pkg/F{i}.java", f"m{i}", 10) for i in range(30)]
    for budget in (1, 3, 7, 12):
        assert len(cf.fit_to_budget(units, budget)) <= budget


def test_fit_keeps_files_apart_when_the_budget_allows(cf):
    """Cross-file batching is the failure mode; it must only happen under pressure."""
    units = [unit(cf, f"pkg/F{i}.java", f"m{i}", 10) for i in range(5)]
    assert all(not b.spans_files for b in cf.fit_to_budget(units, 5))


def test_fit_spans_files_only_when_forced(cf):
    units = [unit(cf, f"pkg/F{i}.java", f"m{i}", 10) for i in range(10)]
    assert any(b.spans_files for b in cf.fit_to_budget(units, 3))


def test_fit_uses_spare_budget_to_split_large_batches(cf):
    """Twelve functions in one file and a budget of 4 should not stay one batch."""
    units = [unit(cf, "pkg/Big.java", f"m{i}", 10) for i in range(12)]
    batches = cf.fit_to_budget(units, 4)
    assert len(batches) == 4
    assert max(len(b.units) for b in batches) <= 3


def test_multi_file_batch_is_labelled_by_its_common_directory(cf):
    units = [unit(cf, "app/svc/A.java", "a", 5), unit(cf, "app/svc/B.java", "b", 5)]
    b = cf.fit_to_budget(units, 1)[0]
    assert b.spans_files and "app/svc" in b.label and "2 files" in b.label


def test_stats_flag_oversized_batches(cf):
    """A budget far below the natural batch count must be reported, not hidden."""
    units = [unit(cf, "pkg/Big.java", f"m{i}", 5) for i in range(cf.BATCH_WARN_UNITS + 4)]
    stats = cf.batch_stats(cf.fit_to_budget(units, 1))
    assert stats["oversized"] == 1
    assert stats["max_units"] == cf.BATCH_WARN_UNITS + 4


def test_stats_are_quiet_when_batches_are_small(cf):
    units = [unit(cf, f"pkg/F{i}.java", "m", 5) for i in range(6)]
    stats = cf.batch_stats(cf.fit_to_budget(units, 6))
    assert stats["oversized"] == 0 and stats["spanning_files"] == 0


# --- change context -------------------------------------------------------------------


def test_a_deletion_only_hunk_still_produces_a_unit(cf, repo):
    """A removed guard clause must not vanish from the review because no new line exists."""
    git(repo, "add", "mod.py")
    git(repo, "commit", "-q", "-m", "edited")
    (repo / "mod.py").write_text(
        ORIGINAL + "\n\ndef gamma(z):\n    return z - 1\n"
    )  # guard deleted
    scope, _ = cf.resolve_scope(repo, None, None, staged=False, uncommitted=True)
    units = cf.collect(repo, scope, None)
    alpha = next(u for u in units if u.symbol == "alpha")
    assert alpha.removed_count == 2
    assert alpha.trivial is False


def test_an_import_only_edit_is_set_aside_as_trivial(cf, repo):
    (repo / "mod.py").write_text("import os\n\n" + EDITED)
    scope, _ = cf.resolve_scope(repo, None, None, staged=False, uncommitted=False)
    units = cf.collect(repo, scope, None)
    orphan = next(u for u in units if u.symbol is None)
    assert orphan.trivial is True
    assert "imports" in orphan.trivial_reason
    assert next(u for u in units if u.symbol == "alpha").trivial is False


def test_callers_and_test_references_come_from_the_repository(cf, tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Test")
    (r / "pricing.py").write_text("def compute_total(items):\n    return sum(items)\n")
    (r / "checkout.py").write_text(
        "from pricing import compute_total\n\n\ndef pay(items):\n    return compute_total(items)\n"
    )
    (r / "test_pricing.py").write_text(
        "from pricing import compute_total\n\n\ndef test_total():\n    assert compute_total([1]) == 1\n"
    )
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "initial")
    (r / "pricing.py").write_text(
        "def compute_total(items):\n    return round(sum(items) * 1.2, 2)\n"
    )
    scope, _ = cf.resolve_scope(r, None, None, staged=False, uncommitted=True)
    units = cf.collect(r, scope, None)
    total = next(u for u in units if u.symbol == "compute_total")
    assert total.caller_count == 2
    assert total.callers_in_tests == 1
    assert "checkout.py:5" in total.callers
    assert total.untested is False
    assert "money" in total.tags and total.risk == "high"


def test_signals_are_empty_for_a_single_file_change_with_nothing_structural(cf, repo):
    scope, _ = cf.resolve_scope(repo, None, None, staged=False, uncommitted=False)
    units = cf.collect(repo, scope, None)
    signals = cf.change_signals(repo, scope, units)
    assert signals["architect"] == []
    assert signals["maker"] == []


def test_refill_spends_idle_agents_in_the_given_order(cf):
    def unit(path, start, name):
        return cf.Unit(path, "python", name, start, start + 5, [start + 1], 1, 100)

    chosen = [unit("a.py", 1, "a1")]
    pool = [
        unit("a.py", 20, "a2"),
        unit("b.py", 1, "b1"),
        unit("c.py", 1, "c1"),
        unit("d.py", 1, "d1"),
    ]
    units, batches, added = cf.refill(chosen, pool, budget=2, max_units=4, max_lines=400)
    assert len(batches) == 2
    assert added == ["a.py:20-25", "b.py:1-6"], (
        "same-file unit is free; the next file spends the last agent"
    )
    assert [u.symbol for u in units] == ["a1", "a2", "b1"]


def test_briefs_are_written_as_facts(cf, repo, tmp_path):
    scope, label = cf.resolve_scope(repo, None, None, staged=False, uncommitted=False)
    units = cf.collect(repo, scope, None)
    batches = cf.batch_units(units)
    out = cf.write_briefs(
        tmp_path / "briefs", label, scope, units, batches, cf.change_signals(repo, scope, units)
    )
    context = Path(out["context"]).read_text()
    assert "CHANGED FUNCTIONS" in context and "alpha" in context and "gamma" in context
    for severity in ("CRITICAL", "WARNING", "SUGGESTION"):
        assert severity not in context, "the context carries facts, never findings"
    brief = Path(out["batches"][0]).read_text()
    assert brief.startswith("REVIEW BRIEF 1 of 1")
    assert "mod.py" in brief


# --- reading the right tree -------------------------------------------------------------
# The mapper takes changed line numbers from the diff but used to take function *bodies* from
# the working tree. When the checkout does not contain the range's files, every span that cannot
# be parsed silently disappears: on a large reference MR the same `--base X --head Y` produced 61
# units against an unrelated checkout and 207 against the range's own tree, warning neither time.


def test_committed_range_reads_the_head_ref_not_the_checkout(cf, repo):
    """A committed range must be mapped from its own head, whatever is checked out."""
    git(repo, "add", "mod.py")
    git(repo, "commit", "-q", "-m", "work")
    head = git(repo, "rev-parse", "HEAD").strip()
    base = git(repo, "rev-parse", "main").strip()

    # Leave the working tree on a branch that has none of the range's content.
    git(repo, "checkout", "-q", "main")
    (repo / "mod.py").unlink()
    (repo / "unrelated.py").write_text("def other():\n    return 0\n")

    scope, _ = cf.resolve_scope(repo, base, head, staged=False, uncommitted=False)
    assert symbols(cf.collect(repo, scope, None, read_ref=head)) == ["alpha", "gamma"]
    # Without the ref it reads the checkout, where the file is gone, and finds nothing at all.
    assert cf.collect(repo, scope, None, read_ref=None) == []


def test_missing_file_at_the_ref_is_skipped_not_fatal(cf, repo):
    git(repo, "add", "mod.py")
    git(repo, "commit", "-q", "-m", "work")
    head = git(repo, "rev-parse", "HEAD").strip()
    assert cf._file_text(repo, "never-existed.py", head) is None


# --- cards and invocations --------------------------------------------------------------


def card(cf, file, symbol, changed, file_lines=500, is_test=False, kind="region"):
    u = cf.Unit(
        file, "python", symbol, 1, 10, list(range(1, changed + 1)), changed, file_lines, is_test
    )
    return cf._card(file, [u], kind)


def test_a_new_test_module_is_one_card_not_one_per_case(cf, repo):
    """The clearest symptom of conflating the review unit with the agent call."""
    suite = "\n\n".join(f"def test_case_{i}():\n    assert True" for i in range(30)) + "\n"
    (repo / "tests").mkdir()
    (repo / "tests" / "test_suite.py").write_text(suite)
    git(repo, "add", "tests/test_suite.py")

    scope, _ = cf.resolve_scope(repo, None, None, staged=False, uncommitted=False)
    units = [u for u in cf.collect(repo, scope, None) if u.file.startswith("tests/")]
    assert len(units) == 30, "every case is still a unit"

    cards = cf.make_cards(units)
    assert len(cards) == 1
    assert cards[0].kind == "suite"
    assert len(cards[0].units) == 30


def test_packing_covers_every_card_and_keeps_them_separate(cf):
    cards = [card(cf, f"app/m{i}.py", "f", 2, file_lines=30) for i in range(12)]
    batches = cf.pack_cards(cards)
    assert sum(len(b.cards) for b in batches) == 12
    assert len(batches) < 12, "packing is the point"
    assert all(len(b.cards) <= cf.INVOCATION_MAX_CARDS for b in batches)


def test_packing_never_mixes_production_and_tests(cf):
    cards = [card(cf, f"app/m{i}.py", "f", 2, file_lines=30) for i in range(3)] + [
        card(cf, f"tests/test_m{i}.py", "T.t", 2, file_lines=30, is_test=True, kind="suite")
        for i in range(3)
    ]
    for b in cf.pack_cards(cards):
        assert len({c.is_test for c in b.cards}) == 1


def test_an_oversized_card_is_reviewed_alone_rather_than_dropped(cf):
    big = card(cf, "app/whole.py", "f", 900, file_lines=900, kind="new-file")
    small = card(cf, "app/small.py", "f", 2, file_lines=30)
    batches = cf.pack_cards([big, small], max_read=600)
    assert sum(len(b.cards) for b in batches) == 2
    assert any(len(b.cards) == 1 and b.cards[0].file == "app/whole.py" for b in batches)


def test_read_cost_drops_when_the_duplicate_file_reads_go(cf):
    """Five cards of one file must not each cost a whole-file read."""
    units = [
        cf.Unit("app/big.py", "python", f"A.m{i}", i * 20 + 1, i * 20 + 10, [i * 20 + 2], 1, 2000)
        for i in range(5)
    ]
    cards = cf.make_cards(units)
    assert sum(c.read_lines for c in cards) < 2000


def test_fit_packs_everything_within_the_budget(cf):
    cards = [card(cf, f"app/m{i}.py", "f", 5, file_lines=60) for i in range(20)]
    batches = cf.fit_cards(cards, 3)
    assert len(batches) <= 3
    assert sum(len(b.cards) for b in batches) == 20


def test_briefs_tell_the_agent_the_cards_are_independent(cf, tmp_path):
    """Without this line a packed agent hunts for a relationship between unrelated files."""
    cards = [card(cf, f"app/m{i}.py", "f", 2, file_lines=30) for i in range(3)]
    batches = cf.pack_cards(cards)
    out = cf.write_briefs(
        tmp_path / "briefs",
        "scope",
        ["HEAD"],
        [u for b in batches for u in b.units],
        batches,
        {"architect": [], "integration": [], "maker": []},
    )
    text = Path(out["batches"][0]).read_text()
    assert "INDEPENDENT review target" in text
    assert "Do NOT infer that two" in text
    assert "CARD 1 of 3" in text


PRODUCER = """\
def convert_amount(amount, currency):
    return amount * 1.0
"""

# The signature grows a parameter. Nothing in this repo calls it; the caller is next door.
PRODUCER_EDITED = """\
def convert_amount(amount, currency, as_of):
    return amount * 1.0
"""

CONSUMER = """\
from rates import convert_amount


def total(order):
    return convert_amount(order.amount, order.currency)
"""


@pytest.fixture
def split_change(tmp_path):
    """A producer whose changed contract has its only caller in a second repository."""
    producer, consumer = tmp_path / "producer", tmp_path / "consumer"
    for path, name, body in ((producer, "rates.py", PRODUCER), (consumer, "billing.py", CONSUMER)):
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        git(path, "config", "user.email", "t@example.com")
        git(path, "config", "user.name", "Test")
        (path / name).write_text(body)
        git(path, "add", name)
        git(path, "commit", "-q", "-m", "initial")
    git(producer, "checkout", "-q", "-b", "feature")
    (producer / "rates.py").write_text(PRODUCER_EDITED)
    return producer, consumer


def test_a_caller_in_another_repo_is_invisible_without_the_flag(cf, split_change):
    """The failure this exists for: no callers found, so the change reads as safe to make."""
    producer, _ = split_change
    scope, _ = cf.resolve_scope(producer, None, None, staged=False, uncommitted=False)
    units = cf.collect(producer, scope, None)
    assert [u.callers for u in units] == [[]]


def test_a_sibling_repo_supplies_the_missing_call_site(cf, split_change):
    producer, consumer = split_change
    siblings = [cf.Sibling(label="consumer", path=consumer)]
    scope, _ = cf.resolve_scope(producer, None, None, staged=False, uncommitted=False)
    units = cf.collect(producer, scope, None, siblings=siblings)
    (unit,) = units
    assert unit.callers == ["consumer:billing.py:5"]
    assert unit.callers_in_siblings == 1
    signals = cf.change_signals(producer, scope, units, siblings)
    assert any("called from consumer" in reason for reason in signals["integration"])


def test_the_brief_explains_how_a_cross_repo_call_site_is_written(cf, split_change, tmp_path):
    """A labelled hit reads as a path unless the reviewer is told the repository set exists."""
    producer, consumer = split_change
    siblings = [cf.Sibling(label="consumer", path=consumer)]
    scope, _ = cf.resolve_scope(producer, None, None, staged=False, uncommitted=False)
    units = cf.collect(producer, scope, None, siblings=siblings)
    cf.write_briefs(
        tmp_path / "briefs",
        "scope",
        scope,
        units,
        cf.pack_cards(cf.make_cards(units)),
        {"architect": [], "integration": [], "maker": []},
        siblings,
    )
    text = (tmp_path / "briefs" / "context.txt").read_text()
    assert "REPOSITORIES" in text
    assert "`consumer:path:line`" in text
