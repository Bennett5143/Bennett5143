#!/usr/bin/env python3
"""Generate the profile README card.

The card is a fixed-width box. Every line is exactly ``WIDTH`` characters, which is
the whole point of generating it: the dot leaders are computed as "whatever space is
left", so a stat going from 99 to 103 shortens the leader instead of pushing the
right border out of alignment. Alignment is an invariant of this file, not something
anyone has to maintain by hand.

Standard library only, so this runs on a bare GitHub Actions runner with no
``pip install`` step and locally without a virtualenv.

Usage::

    python3 scripts/generate_readme.py            # write README.md
    python3 scripts/generate_readme.py --stdout   # print instead of writing

Without ``GITHUB_TOKEN`` in the environment the script does not call the API at all
and renders the ``<n>`` placeholders, so it stays runnable with no credentials.
"""

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

LOGIN = "Bennett5143"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

API_ROOT = "https://api.github.com"
USER_AGENT = "Bennett5143-profile-readme"

# --- Geometry ---------------------------------------------------------------
# Measured off the approved card, not invented. The row layout is:
#   │ + duck cell (DUCK_WIDTH) + panel (PANEL_WIDTH) + one space + │
WIDTH = 57
INNER_WIDTH = WIDTH - 2
DUCK_WIDTH = 21
PANEL_WIDTH = INNER_WIDTH - DUCK_WIDTH - 1
PLACEHOLDER = "<n>"

# 11 rows, drawn left of the panel. Trailing space is added by the renderer.
DUCK = [
    "     %%%%%%@",
    "    @******%@",
    " =======%@%%@",
    " =@@@@==%%%%@",
    "      @:::%@   %%%",
    "    %%%%%%%@@@+++@",
    "   %%%%%%%+++++++@",
    "   @%%%%%+++++++@",
    "   @@%%%%%%%%%@@",
    "     @@@@@@=@@",
    "      @@@@@@@",
]

# Panel rows, in order. Values may reference stats via ``str.format`` field names.
TITLE = "bennett@github"
SECTION = "Stats"
PANEL_ROWS = [
    ("title", TITLE),
    ("rule", ""),
    ("kv", ("OS:", "macOS")),
    ("kv", ("Code:", "C#, Python")),
    ("kv", ("Currently:", "Dual Study (CS)")),
    ("kv", ("Mail:", "bennett@steenfatt.dev")),
    ("blank", ""),
    ("section", SECTION),
    ("kv", ("Repos:", "{repos}")),
    ("kv", ("Commits:", "{commits}")),
    ("kv", ("Ducks consulted:", "1")),
]


class CardError(RuntimeError):
    """Raised when the card cannot be rendered without breaking its own frame."""


# --- Rendering --------------------------------------------------------------


def leader_row(key, value):
    """Render one ``key ....... value`` row, right-aligning the value.

    The dot count is the leftover width, which is what keeps the right border
    aligned no matter how long the value is. One space separates the key and the
    value from the dots so the leader never touches the text.
    """
    dots = PANEL_WIDTH - len(key) - len(value) - 2
    if dots < 1:
        raise CardError(
            f"{key!r} + {value!r} needs {len(key) + len(value) + 3} of "
            f"{PANEL_WIDTH} panel columns; no room left for a dot leader"
        )
    return f"{key} {'.' * dots} {value}"


def section_row(label):
    """Render a ``─ Label ─────`` divider that fills the panel width."""
    prefix = f"─ {label} "
    return prefix + "─" * (PANEL_WIDTH - len(prefix))


def panel_lines(stats):
    """Build the panel column as a list of PANEL_WIDTH-wide strings."""
    lines = []
    for kind, payload in PANEL_ROWS:
        if kind == "title":
            line = payload.ljust(PANEL_WIDTH)
        elif kind == "rule":
            line = "─" * PANEL_WIDTH
        elif kind == "blank":
            line = " " * PANEL_WIDTH
        elif kind == "section":
            line = section_row(payload)
        elif kind == "kv":
            key, value = payload
            line = leader_row(key, value.format(**stats))
        else:  # pragma: no cover - guards against a typo in PANEL_ROWS
            raise CardError(f"unknown panel row kind {kind!r}")
        if len(line) != PANEL_WIDTH:
            raise CardError(f"panel row {line!r} is {len(line)}, expected {PANEL_WIDTH}")
        lines.append(line)
    return lines


def render_card(stats):
    """Render the full card, including its frame, as a single string."""
    panel = panel_lines(stats)
    if len(panel) != len(DUCK):
        raise CardError(f"{len(DUCK)} duck rows but {len(panel)} panel rows")

    lines = ["╭" + "─" * INNER_WIDTH + "╮"]
    for duck_row, panel_row in zip(DUCK, panel):
        if len(duck_row) > DUCK_WIDTH:
            raise CardError(f"duck row {duck_row!r} exceeds {DUCK_WIDTH} columns")
        lines.append("│" + duck_row.ljust(DUCK_WIDTH) + panel_row + " " + "│")
    lines.append("╰" + "─" * INNER_WIDTH + "╯")

    for line in lines:
        if len(line) != WIDTH:
            raise CardError(f"line {line!r} is {len(line)}, expected {WIDTH}")
    return "\n".join(lines)


def render_readme(stats):
    """Render the complete README: the fenced card and nothing else.

    No language tag on the fence — a tag would hand the box-drawing characters to
    GitHub's syntax highlighter; a plain fence renders as inert monospace text.
    """
    return "```\n" + render_card(stats) + "\n```\n"


# --- Stats ------------------------------------------------------------------


def year_windows(created_at, now):
    """Split ``created_at``..``now`` into windows of at most one year.

    ``contributionsCollection`` rejects a span longer than a year outright
    ("The total time spanned by 'from' and 'to' must not exceed 1 year"), so a
    lifetime total has to be summed over several windows. Windows are half-open in
    effect — each starts one second after the previous one ends — so no commit is
    counted twice.
    """
    windows = []
    start = created_at
    while start < now:
        end = min(start + timedelta(days=365), now)
        windows.append((_iso(start), _iso(end)))
        start = end + timedelta(seconds=1)
    return windows


def _iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text):
    # Written the long way instead of fromisoformat("...Z"), which only learned to
    # accept the trailing Z in Python 3.11.
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def build_contributions_query(login, windows):
    """Build one GraphQL document that asks for every window in a single request."""
    fields = "\n".join(
        f'    w{i}: contributionsCollection(from: "{start}", to: "{end}") {{\n'
        f"      totalCommitContributions\n"
        f"    }}"
        for i, (start, end) in enumerate(windows)
    )
    return f'query {{\n  user(login: "{login}") {{\n{fields}\n  }}\n}}\n'


def parse_repo_count(user_payload):
    """Public repo count, forks included — that is what ``public_repos`` reports."""
    return int(user_payload["public_repos"])


def parse_commit_total(graphql_payload):
    """Sum ``totalCommitContributions`` across every window alias in the response."""
    user = graphql_payload["data"]["user"]
    return sum(
        window["totalCommitContributions"]
        for key, window in user.items()
        if key.startswith("w") and isinstance(window, dict)
    )


def _request(url, token, data=None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        # Header, never a query string: query strings end up in request logs.
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_stats(token, now=None):
    """Fetch the live stats. Raises on any API problem — see ``main``."""
    now = now or datetime.now(timezone.utc)
    user_payload = _request(f"{API_ROOT}/users/{LOGIN}", token)
    created_at = _parse_iso(user_payload["created_at"])

    query = build_contributions_query(LOGIN, year_windows(created_at, now))
    graphql_payload = _request(f"{API_ROOT}/graphql", token, {"query": query})
    if "errors" in graphql_payload:
        raise CardError(f"GraphQL error: {json.dumps(graphql_payload['errors'])}")

    return {
        "repos": str(parse_repo_count(user_payload)),
        "commits": str(parse_commit_total(graphql_payload)),
    }


def placeholder_stats():
    return {"repos": PLACEHOLDER, "commits": PLACEHOLDER}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stdout", action="store_true", help="print the README instead of writing it"
    )
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    if token:
        # Deliberately not falling back to placeholders on an API failure: that would
        # silently replace real numbers with <n> and look like a successful run. A
        # loud failure leaves the last good README in place.
        try:
            stats = fetch_stats(token)
        except (urllib.error.URLError, KeyError, ValueError, CardError) as error:
            print(f"error: could not fetch stats: {error}", file=sys.stderr)
            return 1
    else:
        print("note: GITHUB_TOKEN not set, rendering placeholders", file=sys.stderr)
        stats = placeholder_stats()

    readme = render_readme(stats)
    if args.stdout:
        sys.stdout.write(readme)
    else:
        README.write_text(readme, encoding="utf-8")
        print(f"wrote {README}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
