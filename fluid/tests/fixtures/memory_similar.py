"""An invented agent store for the save-time similar-facts hint (no private data).

``STORE`` is what a working store holds: owner rules and agent findings on many
subjects, each with its when-line. ``RESTATED`` are later saves on a subject the
store already has, in other words, each naming the index of the fact it
restates: the hint must name that fact. ``NEW_SUBJECTS`` are later saves on a
subject the store has no fact about but whose vocabulary overlaps it (push,
tests, deploy, browser): the hint must stay quiet on them.
"""

# (text, when)
STORE = [
    ("Run the backend tests with the project venv's python -m pytest; the worker's own "
     "Python has no pytest installed.",
     "When running Python tests on this worker"),
    ("Before every push run the linter and the secret scan; the pre-push hook refuses a "
     "push that skipped the scan.",
     "Before pushing a branch"),
    ("Pull request bodies end with a Running-app checks section that says what was "
     "exercised live and what only tests cover.",
     "When writing a pull request description"),
    ("Deploy the website from main only, after the release tag exists; verify the live "
     "site afterwards.",
     "When deploying the website"),
    ("The docs page and its frontend mirror under src/lib/docs must stay byte-identical; "
     "edit both in the same commit.",
     "When changing user documentation pages"),
    ("The agent browser runs on another host: bind dev servers to 0.0.0.0 and open them "
     "by the worker's LAN address, never localhost.",
     "When opening a local dev server in the agent browser"),
    ("On Windows workers, quote paths with spaces in PowerShell and never pipe npm output "
     "through findstr; it hangs the shell.",
     "When running shell commands on a Windows worker"),
    ("CI runs only on free standard GitHub runners; ask the owner before adding any paid "
     "or larger runner.",
     "When changing CI workflows or runner types"),
    ("Code review subagents run on the strongest available model and only read; they "
     "never push or edit the branch they review.",
     "When starting a code review subagent"),
    ("The dev instance listens on port 18080 and its database on 15432; the owner's live "
     "app keeps port 8000.",
     "When starting a dev instance or picking a port"),
    ("Testing screenshots stay in the scratch folder; never attach them to a pull request "
     "or commit them.",
     "When taking screenshots to verify UI work"),
    ("Every commit carries a Signed-off-by trailer matching the author, then the AI "
     "co-author trailer last.",
     "When writing commit messages"),
    ("Schema changes ship as additive migrations; a column is dropped only one release "
     "after nothing reads it.",
     "When changing the database schema"),
    ("The owner prefers short status updates: one sentence of outcome first, details "
     "after, no headings.",
     "When reporting progress to the owner"),
    ("Merge with plain gh pr merge, never --delete-branch, because sibling worktrees make "
     "the branch delete fail after the merge landed.",
     "When merging a pull request"),
    ("Nightly builds publish once a night at midnight New York time; a merge never starts "
     "one.",
     "When checking whether a merged change is in a nightly"),
    ("Frontend unit tests run with npx vitest run from the frontend folder after npm ci; "
     "the root has no vitest.",
     "When running frontend unit tests"),
    ("The mobile app has no lockfile: install the frontend first, then npm run deps in "
     "mobile, never npm ci there.",
     "When installing mobile dependencies"),
    ("API keys and tokens go only through the Vault card; never ask for them in chat or "
     "echo them in a command.",
     "When a task needs a credential"),
    ("Session journeys live in frontend/tests/journeys and run against a dev instance "
     "with node run-all.mjs --changed.",
     "When running UI journeys"),
]

# (text, when, index into STORE of the fact it restates)
RESTATED = [
    ("Backend pytest runs must use the project's venv interpreter (python -m pytest); "
     "pytest is not installed in the worker Python.",
     "When running backend tests", 0),
    ("Run the secret scan and the linter before pushing; the pre-push hook blocks pushes "
     "that skip the scan.",
     "When pushing commits", 1),
    ("Bind local dev servers to 0.0.0.0 and browse them by the worker's LAN address; the "
     "agent browser runs on another host, so localhost fails.",
     "When browsing a dev server from the agent browser", 5),
    ("Only free standard GitHub runners for CI; adding a paid or larger runner needs the "
     "owner's approval first.",
     "When editing CI workflow runners", 7),
    ("Use gh pr merge without --delete-branch: with sibling worktrees the branch delete "
     "fails after the merge already landed.",
     "When merging PRs", 14),
    ("Edit the docs page and its src/lib/docs frontend mirror together in one commit; "
     "the two copies must stay byte-identical.",
     "When editing documentation", 4),
    ("Mobile has no lockfile, so never npm ci in mobile: install frontend dependencies "
     "first, then run npm run deps there.",
     "When setting up the mobile app", 17),
]

# (text, when): new subjects sharing vocabulary with STORE, not restating it.
NEW_SUBJECTS = [
    ("Force-pushing a branch that has an open pull request restarts its whole CI suite; "
     "push review fixes as new commits.",
     "When pushing fixes to an open pull request"),
    ("Python tests that touch PostgreSQL need the disposable cluster from the dev "
     "instance; the shim cannot run SET.",
     "When a backend test needs PostgreSQL"),
    ("Deploy the license server only after the account hub is live, and roll back by "
     "redeploying the previous tag.",
     "When deploying the license server"),
    ("The browser panel cannot capture the microphone; voice checks need the owner's own "
     "browser.",
     "When testing voice features"),
    ("Windows installers are signed in CI from the release tag; a local build is unsigned "
     "and only for testing.",
     "When building a Windows installer"),
    ("Review findings about a race need a concrete interleaving before they are fixed; "
     "a timing guess alone is not a finding.",
     "When triaging code review findings"),
    ("Commit messages use a conventional type prefix such as fix, feat or docs, and a "
     "subject line under 72 characters.",
     "When choosing a commit subject line"),
]
