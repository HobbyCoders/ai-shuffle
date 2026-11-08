import { describe, expect, it } from 'vitest';
import { pullConflicts, pullNotification, resolveConflictsPrompt, resolveWithAgentReason } from './git-pull-notification';

describe('MainPanel Pull notifications', () => {
    it('says a conflicting pull was stopped, naming the target and files without asking for a commit', () => {
        const message = 'Pull from other/main was stopped due to conflicts in a.txt. Nothing was changed.';
        const notification = pullNotification({ success: false, message });
        expect(notification).toEqual({ message, severity: 'error' });
        expect(notification.message).not.toMatch(/resolve|then commit/i);
    });

    it('reports unrelated histories as a stopped pull rather than a success', () => {
        const message = 'Pull stopped: this branch and origin/main share no history (the remote may have changed). Nothing was changed.';
        expect(pullNotification({ success: false, message })).toEqual({ message, severity: 'error' });
    });

    it('preserves shallow-clone recovery instructions', () => {
        const message = "Pull stopped: the shared history with origin/main isn't in this shallow clone. Run `git fetch --unshallow` (or `--deepen`). Nothing was changed.";
        expect(pullNotification({ success: false, message })).toEqual({ message, severity: 'error' });
    });

    it('shows a warning when integration succeeded but restoring stashed changes failed', () => {
        const message = 'Pull from other/main succeeded but failed to restore stashed changes: conflict';
        expect(pullNotification({ success: true, warning: true, message })).toEqual({ message, severity: 'warning' });
        // Severity is driven by the structured flag, not by wording.
        expect(pullNotification({ success: true, warning: true, message: 'Recovery required' }).severity).toBe('warning');
    });

    it('retains the actual target in the successful pull message', () => {
        const message = 'Pulled from other/main using rebase';
        expect(pullNotification({ success: true, message })).toEqual({ message, severity: 'success' });
    });

    it('provides severity-appropriate fallback messages', () => {
        expect(pullNotification({ success: false, message: '' })).toEqual({ message: 'Pull stopped. Nothing was changed.', severity: 'error' });
        expect(pullNotification({ success: true, warning: true, message: '' }).severity).toBe('warning');
        expect(pullNotification({ success: true, message: '' })).toEqual({ message: 'Pulled latest changes', severity: 'success' });
    });
});

describe('a pull stopped on conflicts', () => {
    it('lists the conflicting files only when the pull stopped on them', () => {
        expect(pullConflicts({ success: false, conflicts: ['README.md', 'a/b.csproj'], has_conflicts: true })).toEqual(['README.md', 'a/b.csproj']);
        // Either flag is enough: an older worker may send only one of them.
        expect(pullConflicts({ success: false, conflicts: ['README.md'], has_conflicts: false })).toEqual(['README.md']);
        expect(pullConflicts({ success: false, conflicts: [], has_conflicts: true })).toEqual([]);
        expect(pullConflicts({ success: false, conflicts: [], has_conflicts: false })).toBeNull();
        expect(pullConflicts({ success: true, conflicts: ['README.md'], has_conflicts: true })).toBeNull();
    });

    it('asks the agent to merge the upstream into this branch, resolve each file and commit', () => {
        const prompt = resolveConflictsPrompt({ target: 'origin/main', branch: 'feat/x', conflicts: ['README.md', 'app/x.cs'] });
        expect(prompt).toContain('merge `origin/main` into the current branch (`feat/x`)');
        expect(prompt).toMatch(/resolve the merge conflicts in these files, and commit the merge/);
        expect(prompt).toContain('- `README.md`');
        expect(prompt).toContain('- `app/x.cs`');
        // A stopped pull that could not be undone leaves a merge in progress.
        expect(prompt).toMatch(/still in progress, finish that one/);
        expect(prompt).toMatch(/Do not push/);
    });

    it('still makes a usable request without a target, branch or file list', () => {
        const prompt = resolveConflictsPrompt({ target: null, branch: null, conflicts: [] });
        expect(prompt).toContain('merge `the upstream branch` into the current branch,');
        expect(prompt).toMatch(/git status/);
    });

    it('offers "Resolve with agent" only in the idle main chat the sync ran in', () => {
        const base = { panelSessionId: 's1', liveSessionId: 's1', isSubagent: false, streaming: false };
        expect(resolveWithAgentReason(base)).toBeNull();
        expect(resolveWithAgentReason({ ...base, streaming: true })).toMatch(/current reply finishes/);
        expect(resolveWithAgentReason({ ...base, liveSessionId: 's2' })).toMatch(/Open the chat this sync ran in/);
        expect(resolveWithAgentReason({ ...base, panelSessionId: null, liveSessionId: null })).toMatch(/Open a chat/);
        expect(resolveWithAgentReason({ ...base, isSubagent: true })).toMatch(/helper agent/);
    });
});
