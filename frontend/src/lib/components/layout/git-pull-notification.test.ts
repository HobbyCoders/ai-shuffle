import { describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Exercise MainPanel's actual action without mounting the unrelated workspace UI.
const source = readFileSync(resolve(process.cwd(), 'src/lib/components/layout/MainPanel.svelte'), 'utf8');
const handler = source.match(/async function handlePull\(opts: PullOptions = \{\}\) \{[\s\S]*?(?=\n\s*async function handleCreatePR)/)?.[0];
if (!handler) throw new Error('MainPanel handlePull was not found');
const runPull = new Function('harness', `
    const { git, notify, gitActionsBlocked, captureGitRetryGuard, markGitActivity,
        gitActionPreferences, gitOperations, isApiError, errText } = harness;
    ${handler.replace('opts: PullOptions = {}', 'opts = {}')}
    return handlePull();
`);

async function pullNotification(response: { success: boolean; message: string; has_conflicts: boolean; conflicts: string[] }) {
    const notify = vi.fn();
    await runPull({
        git: { pull: vi.fn().mockResolvedValue(response), refreshPrState: vi.fn().mockResolvedValue(undefined) },
        notify,
        gitActionsBlocked: () => false,
        captureGitRetryGuard: () => (retry: () => void) => retry(),
        markGitActivity: vi.fn(),
        gitActionPreferences: { current: { pullStrategy: 'rebase', autoStash: false } },
        gitOperations: { begin: vi.fn(), clear: vi.fn() },
        isApiError: () => false,
        errText: (error: unknown) => String(error)
    });
    return notify;
}

describe('MainPanel Pull notifications', () => {
    it('says a conflicting pull was stopped, naming the target and files without asking for a commit', async () => {
        const message = 'Pull from other/main was stopped due to conflicts in a.txt. Nothing was changed.';
        const notify = await pullNotification({ success: false, message, has_conflicts: true, conflicts: ['a.txt'] });
        expect(notify).toHaveBeenCalledExactlyOnceWith(message, { severity: 'error' });
        expect(notify.mock.calls[0][0]).not.toMatch(/resolve|then commit/i);
    });

    it('reports unrelated histories as a stopped pull, not a success even without conflicts', async () => {
        const message = 'Pull stopped: this branch and origin/main share no history (the remote may have changed). Nothing was changed.';
        const notify = await pullNotification({ success: false, message, has_conflicts: false, conflicts: [] });
        expect(notify).toHaveBeenCalledExactlyOnceWith(message, { severity: 'error' });
    });

    it('retains the actual target in the successful pull message', async () => {
        const message = 'Pulled from other/main using rebase';
        const notify = await pullNotification({ success: true, message, has_conflicts: false, conflicts: [] });
        expect(notify).toHaveBeenCalledExactlyOnceWith(message, { severity: 'success' });
    });
});
