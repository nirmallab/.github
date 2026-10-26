"""Print the id of the newest run in this repository with a live ``ci-durations`` artifact.

    python find_durations.py

The weekly report merges every Linux shard's per-test times into one
``ci-durations`` artifact (kept 90 days); the next sharded run balances its
shards on it. Prints nothing when there is none, or when the API cannot be
reached: stale or missing durations cost shard balance, never a test.
Needs ``GITHUB_TOKEN`` with ``actions: read`` and ``GITHUB_REPOSITORY``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def newest(repo: str, token: str) -> str:
    if not repo or not token:
        return ""
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    request = urllib.request.Request(
        f"{api}/repos/{repo}/actions/artifacts?name=ci-durations&per_page=20",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            artifacts = json.load(response).get("artifacts", [])
    except Exception as exc:
        print(f"::warning::Could not list ci-durations artifacts: {exc}", file=sys.stderr)
        return ""
    live = sorted((a for a in artifacts if not a.get("expired")),
                  key=lambda a: a.get("created_at", ""), reverse=True)
    return str(live[0]["workflow_run"]["id"]) if live else ""


if __name__ == "__main__":
    print(newest(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", "")))
