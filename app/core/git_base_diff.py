"""Session branch review against a freshly resolved merge base (no status cache)."""
import os
import subprocess


class BaseReviewUnavailable(ValueError):
    """The checkout cannot provide a meaningful base comparison."""


def _name_status(output):
    fields = iter(output.split("\0"))
    rows = []
    for status in fields:
        if not status:
            continue
        path = next(fields)
        original = None
        if status[0] in "RC":
            original, path = path, next(fields)
        rows.append({"file": path, "status": status[0], "original_file": original})
    return rows


class GitBaseDiffMixin:
    def _base_run(self, working_dir, args, timeout=15):
        try:
            return self._run_git(working_dir, args, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise BaseReviewUnavailable("Base review failed while reading this checkout. Retry when Git is available.") from exc

    def _base_git(self, working_dir, args):
        result = self._base_run(working_dir, args)
        if result.returncode != 0:
            raise BaseReviewUnavailable("Base review failed while reading this checkout. Refresh and retry.")
        return result.stdout

    def _review_base(self, working_dir):
        branch = self._base_run(working_dir, ["symbolic-ref", "--quiet", "HEAD"], timeout=5)
        if branch.returncode != 0:
            raise BaseReviewUnavailable("Base review is unavailable for a detached or unborn HEAD. Check out a branch first.")
        remotes = self._base_git(working_dir, ["remote"]).splitlines()
        if not remotes:
            raise BaseReviewUnavailable("Base review requires a remote and a default branch. Configure a remote first.")
        head = self._base_git(working_dir, ["rev-parse", "--verify", "HEAD^{commit}"]).strip()
        default = self.get_default_branch(working_dir)
        for ref, full in ((f"origin/{default}", f"refs/remotes/origin/{default}"),
                          (default, f"refs/heads/{default}")):
            tip = self._base_run(working_dir, ["rev-parse", "--verify", full + "^{commit}"], timeout=5)
            if tip.returncode != 0:
                continue
            base = self._base_run(working_dir, ["merge-base", head, tip.stdout.strip()], timeout=5)
            if base.returncode != 0:
                raise BaseReviewUnavailable("Base review is unavailable: this branch and the default branch have unrelated history.")
            return {"base_ref": ref, "merge_base": base.stdout.strip()}, head
        raise BaseReviewUnavailable("Base review is unavailable: no default branch ref exists. Fetch the default branch first.")

    def _base_changes(self, working_dir):
        from app.core.git_diff_ops import _parse_numstat_z

        metadata, head = self._review_base(working_dir)
        base = metadata["merge_base"]
        options = ["diff", "--no-ext-diff", "--no-textconv", "--find-renames"]
        committed = _name_status(self._base_git(working_dir, options + ["--name-status", "-z", base, head, "--"]))
        dirty = _name_status(self._base_git(working_dir, options + ["--name-status", "-z", head, "--"]))
        untracked = self._base_git(working_dir, ["ls-files", "--others", "--exclude-standard", "-z"]).split("\0")
        # HEAD-to-worktree can be empty when index and worktree changes cancel.
        # Keep these paths too: they still contain uncommitted index changes.
        staged = _name_status(self._base_git(working_dir, options + ["--cached", "--name-status", "-z", head, "--"]))
        records = {}
        for row in committed:
            records[row["file"]] = {**row, "change_origin": "committed"}
        for row in dirty + staged:
            path = row["file"]
            prior = records.get(path)
            if prior is None and row["original_file"]:
                prior = records.pop(row["original_file"], None)
            origin = "both" if prior and prior["change_origin"] in ("committed", "both") else "uncommitted"
            records[path] = {**row, "change_origin": origin,
                             "original_file": (prior or {}).get("original_file") or row["original_file"]}
        for path in filter(None, untracked):
            prior = records.get(path)
            records[path] = {**(prior or {"file": path, "status": "?", "original_file": None}),
                             "change_origin": "both" if prior and prior["change_origin"] in ("committed", "both") else "uncommitted"}
        net = _name_status(self._base_git(working_dir, options + ["--name-status", "-z", base, "--"]))
        for row in net:
            prior = records.get(row["file"])
            if prior:
                records[row["file"]] = {**prior, **row}
        stats = {path: (added, deleted, binary) for path, added, deleted, binary in _parse_numstat_z(
            self._base_git(working_dir, options + ["--numstat", "-z", base, "--"]))}
        for path, row in records.items():
            added, deleted, binary = stats.get(path, (0, 0, False))
            if path in untracked:
                result = self._base_run(working_dir, ["diff", "--no-ext-diff", "--no-textconv", "--no-index", "--numstat", "-z", "--", os.devnull, path], timeout=15)
                if result.returncode not in (0, 1):
                    raise BaseReviewUnavailable("Base review could not read an untracked file.")
                rows = _parse_numstat_z(result.stdout)
                if rows:
                    _, extra, _, binary_new = rows[0]
                    added += extra
                    binary = binary or binary_new
            row.update(category="base", additions=added, deletions=deleted, is_binary=binary)
        return metadata, records

    def get_base_changed_files(self, working_dir):
        metadata, records = self._base_changes(working_dir)
        files = sorted(records.values(), key=lambda row: row["file"])
        return {**metadata, "files": files,
                "total_additions": sum(row["additions"] for row in files),
                "total_deletions": sum(row["deletions"] for row in files)}

    def get_base_file_diff(self, working_dir, file_path, max_chars=50000):
        metadata, records = self._base_changes(working_dir)
        row = records.get(file_path)
        if row is None:
            raise BaseReviewUnavailable("This file is no longer in the base review. Refresh the changed files.")
        result = {**metadata, "file": file_path, "diff": "", "is_binary": row["is_binary"], "truncated": False}
        if row["is_binary"]:
            result["diff"] = "Binary file (not displayable)"
            return result
        paths = [f":(literal){file_path}"]
        if row["original_file"]:
            paths.append(f":(literal){row['original_file']}")
        diff = self._base_git(working_dir, ["diff", "--no-ext-diff", "--no-textconv", "--find-renames", metadata["merge_base"], "--", *paths])
        # Git's tracked diff omits untracked content. Include its new-file
        # patch, with the same traversal check and overall truncation limit.
        untracked = self._base_git(working_dir, ["ls-files", "--others", "--exclude-standard", "-z"]).split("\0")
        if file_path in untracked:
            full_path = os.path.realpath(os.path.join(working_dir, file_path))
            root = os.path.realpath(working_dir)
            if not full_path.startswith(root + os.sep):
                raise BaseReviewUnavailable("Base review blocked a file outside this checkout.")
            extra = self._base_run(working_dir, [
                "diff", "--no-ext-diff", "--no-textconv", "--no-index", "--", os.devnull, file_path,
            ])
            if extra.returncode not in (0, 1):
                raise BaseReviewUnavailable("Base review could not read an untracked file.")
            diff += extra.stdout
        if len(diff) > max_chars:
            diff = diff[:max_chars] + "\n\n[... diff truncated ...]"
            result["truncated"] = True
        result["diff"] = diff
        return result
