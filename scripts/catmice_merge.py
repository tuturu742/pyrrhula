"""Land a green work branch on main (run inside the api pod): argv = store_key, branch.

Delegated work items each branch from main, so a later item cannot see an earlier one
until it lands. The runner calls this after CI goes green, which is what a human would do
by merging the PR.
"""

from __future__ import annotations

import asyncio
import sys

from adapters.mcp.git_store import GitStore, GitStoreError, default_git_root


async def main() -> None:
    store_key, branch = sys.argv[1], sys.argv[2]
    store = GitStore(default_git_root())
    try:
        sha = await store.merge_branch(store_key, branch)
    except GitStoreError as exc:
        print(f"merge_failed={exc}")
        raise SystemExit(1) from exc
    print(f"merged={branch}")
    print(f"head={sha}")


asyncio.run(main())
