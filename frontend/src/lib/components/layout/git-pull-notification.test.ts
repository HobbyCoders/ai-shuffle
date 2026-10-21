import { describe, expect, it } from 'vitest';
import { pullNotification } from './git-pull-notification';

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
