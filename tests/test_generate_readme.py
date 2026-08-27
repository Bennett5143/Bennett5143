"""Tests for the README generator.

No network and no token: every API-shaped input comes from a fixture captured from
the live API on 2026-08-27.

Run with::

    python3 -m unittest discover -s tests -v
"""

import json
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_readme as gen  # noqa: E402


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class GeometryTests(unittest.TestCase):
    """The card's frame must survive any value we can plausibly put in it."""

    def test_every_line_is_exactly_width(self):
        for repos, commits in [("<n>", "<n>"), ("3", "228"), ("0", "1"), ("128", "99999")]:
            card = gen.render_card({"repos": repos, "commits": commits})
            widths = {len(line) for line in card.split("\n")}
            self.assertEqual(widths, {gen.WIDTH}, f"repos={repos} commits={commits}")

    def test_leader_shrinks_as_the_value_grows(self):
        short = gen.leader_row("Commits:", "7")
        long = gen.leader_row("Commits:", "1234567")
        self.assertEqual(len(short), len(long))
        self.assertGreater(short.count("."), long.count("."))

    def test_value_that_cannot_fit_raises_instead_of_breaking_the_frame(self):
        with self.assertRaises(gen.CardError):
            gen.leader_row("Commits:", "x" * gen.PANEL_WIDTH)

    def test_border_characters(self):
        lines = gen.render_card(gen.placeholder_stats()).split("\n")
        self.assertTrue(lines[0].startswith("╭") and lines[0].endswith("╮"))
        self.assertTrue(lines[-1].startswith("╰") and lines[-1].endswith("╯"))
        for line in lines[1:-1]:
            self.assertTrue(line.startswith("│") and line.endswith("│"))

    def test_duck_is_unchanged_and_fits_its_column(self):
        self.assertEqual(len(gen.DUCK), 11)
        self.assertLessEqual(max(len(row) for row in gen.DUCK), gen.DUCK_WIDTH)


class ApprovedCardTests(unittest.TestCase):
    """P2: the generator reproduces the hand-approved card byte for byte."""

    def test_placeholder_render_matches_the_approved_readme(self):
        approved = (FIXTURES / "approved_readme.md").read_text(encoding="utf-8")
        self.assertEqual(gen.render_readme(gen.placeholder_stats()), approved)

    def test_live_readme_differs_from_approved_only_in_stats_lines(self):
        approved = (FIXTURES / "approved_readme.md").read_text(encoding="utf-8").split("\n")
        live = (REPO_ROOT / "README.md").read_text(encoding="utf-8").split("\n")
        self.assertEqual(len(approved), len(live))
        for approved_line, live_line in zip(approved, live):
            if approved_line != live_line:
                self.assertRegex(live_line, r"(Repos:|Commits:)")


class WindowTests(unittest.TestCase):
    """contributionsCollection rejects a span longer than one year."""

    def test_windows_never_exceed_one_year(self):
        created = gen._parse_iso("2024-11-06T15:05:05Z")
        now = created + timedelta(days=1000)
        for start, end in gen.year_windows(created, now):
            span = gen._parse_iso(end) - gen._parse_iso(start)
            self.assertLessEqual(span, timedelta(days=365))

    def test_windows_do_not_overlap_and_cover_the_range(self):
        created = gen._parse_iso("2024-11-06T15:05:05Z")
        now = created + timedelta(days=1000)
        windows = gen.year_windows(created, now)
        self.assertEqual(windows[0][0], "2024-11-06T15:05:05Z")
        self.assertEqual(gen._parse_iso(windows[-1][1]), now)
        for (_, earlier_end), (later_start, _) in zip(windows, windows[1:]):
            self.assertGreater(gen._parse_iso(later_start), gen._parse_iso(earlier_end))

    def test_single_window_when_account_is_young(self):
        created = gen._parse_iso("2026-01-01T00:00:00Z")
        now = created + timedelta(days=30)
        self.assertEqual(len(gen.year_windows(created, now)), 1)

    def test_query_document_contains_one_alias_per_window(self):
        windows = [("2024-01-01T00:00:00Z", "2024-12-31T00:00:00Z")] * 3
        query = gen.build_contributions_query("Bennett5143", windows)
        for alias in ("w0:", "w1:", "w2:"):
            self.assertIn(alias, query)
        self.assertIn('user(login: "Bennett5143")', query)


class ParsingTests(unittest.TestCase):
    """Fixture-driven: what the live API actually returned on 2026-08-27."""

    def test_repo_count_includes_forks(self):
        # public_repos was 3 while the account held 2 own repos and 1 fork.
        self.assertEqual(gen.parse_repo_count(load("user.json")), 3)

    def test_commit_total_sums_all_windows(self):
        self.assertEqual(gen.parse_commit_total(load("contributions.json")), 228)

    def test_commit_total_of_a_single_empty_window(self):
        payload = {"data": {"user": {"w0": {"totalCommitContributions": 0}}}}
        self.assertEqual(gen.parse_commit_total(payload), 0)

    def test_created_at_round_trips(self):
        created = gen._parse_iso(load("user.json")["created_at"])
        self.assertEqual(created.tzinfo, timezone.utc)
        self.assertEqual(gen._iso(created), "2024-11-06T15:05:05Z")

    def test_full_render_from_fixtures(self):
        stats = {
            "repos": str(gen.parse_repo_count(load("user.json"))),
            "commits": str(gen.parse_commit_total(load("contributions.json"))),
        }
        card = gen.render_card(stats)
        self.assertIn("Repos: ........................ 3 ", card)
        self.assertIn("Commits: .................... 228 ", card)
        self.assertEqual({len(line) for line in card.split("\n")}, {gen.WIDTH})


class OfflineTests(unittest.TestCase):
    """Without a token the script must not touch the network."""

    def test_placeholder_stats_are_the_documented_placeholder(self):
        self.assertEqual(
            gen.placeholder_stats(), {"repos": "<n>", "commits": "<n>"}
        )

    def test_main_with_stdout_and_no_token_renders_placeholders(self):
        import io
        import os
        from contextlib import redirect_stderr, redirect_stdout

        previous = os.environ.pop("GITHUB_TOKEN", None)
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = gen.main(["--stdout"])
        finally:
            if previous is not None:
                os.environ["GITHUB_TOKEN"] = previous
        self.assertEqual(code, 0)
        self.assertIn("<n>", out.getvalue())
        self.assertIn("GITHUB_TOKEN not set", err.getvalue())


if __name__ == "__main__":
    unittest.main()
