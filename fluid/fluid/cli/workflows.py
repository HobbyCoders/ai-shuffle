"""Local-owner review for prose proposals. Deliberately no --yes/auto approval."""
from __future__ import annotations

import json
import sys

from fluid.codemode.skills import SkillStore
from fluid.codemode.workflows import WorkflowProposals
from fluid.cli.evolve import skills_root


def cmd_workflows(args, config):
    queue = WorkflowProposals(SkillStore(skills_root(config)))
    rows = queue.list()
    if args.workflows_command == "list":
        print(json.dumps([{k: r[k] for k in ("id", "name", "project", "status")} for r in rows], indent=2))
        return 0
    row = next((r for r in rows if r["id"] == args.proposal_id), None)
    if row is None:
        print("Unknown workflow proposal", file=sys.stderr)
        return 1
    # The owner sees exact content and evidence before any approval question.
    print(json.dumps(row, ensure_ascii=False, indent=2))
    if args.workflows_command == "show":
        return 0
    if not sys.stdin.isatty():
        print("Workflow approval/rejection requires interactive local-owner review; no changes made.", file=sys.stderr)
        return 1
    if args.workflows_command == "reject":
        if input("Type reject and the full proposal ID: ").strip() != "reject " + row["id"]:
            return 1
        queue.reject(row["id"])
        return 0
    print("Review the COMPLETE cited sessions, scope and exceptions. Confirm the workflow was repeated successfully, "
          "was not corrected/rejected, does not duplicate installed skills, contains no secrets, and grants no permissions. "
          "This installs only in the current project. Do not approve solely on the model's summary.")
    if input("To approve this exact content, type approve and the full proposal ID: ").strip() != "approve " + row["id"]:
        return 1
    try:
        path = queue.promote(row["id"], config.cwd, approved_digest=row["id"])
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Promoted {path}")
    return 0
