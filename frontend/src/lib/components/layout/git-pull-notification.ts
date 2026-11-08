import type { PullResponse } from '$lib/api/git';
import type { NotificationSeverity } from '$lib/stores/notifications';
import { askAgentUnavailableReason } from '$lib/review/scope';

/** Keep backend recovery details intact; a completed pull can still need attention. */
export function pullNotification(response: Pick<PullResponse, 'success' | 'message' | 'warning'>): {
    message: string;
    severity: NotificationSeverity;
} {
    if (!response.success) {
        return { message: response.message || 'Pull stopped. Nothing was changed.', severity: 'error' };
    }
    if (response.warning) {
        return { message: response.message || 'Pull completed, but stashed changes could not be restored.', severity: 'warning' };
    }
    return { message: response.message || 'Pulled latest changes', severity: 'success' };
}

/**
 * The files a stopped pull conflicted on, or null when it did not stop on
 * conflicts. The backend sets both `has_conflicts` and `conflicts`; either is
 * enough so an older worker that sends only one still gets the conflict view.
 */
export function pullConflicts(response: Pick<PullResponse, 'success' | 'conflicts' | 'has_conflicts'>): string[] | null {
    if (response.success) return null;
    const files = (response.conflicts ?? []).filter((f) => !!f);
    if (files.length === 0 && !response.has_conflicts) return null;
    return files;
}

/**
 * The chat message "Resolve with agent" sends. It names the upstream to merge
 * and every conflicting file, and covers the case where the stopped pull could
 * not be undone (a merge or rebase is still in progress).
 */
export function resolveConflictsPrompt(opts: { target?: string | null; branch?: string | null; conflicts: string[] }): string {
    const upstream = opts.target?.trim() || 'the upstream branch';
    const branch = opts.branch?.trim() ? `the current branch (\`${opts.branch.trim()}\`)` : 'the current branch';
    const files = opts.conflicts.length > 0
        ? opts.conflicts.map((f) => `- \`${f}\``).join('\n')
        : '- (the pull did not list them; run `git status` after merging to see them)';
    return [
        `Syncing with \`${upstream}\` stopped on merge conflicts. Please fetch, merge \`${upstream}\` into ${branch}, resolve the merge conflicts in these files, and commit the merge:`,
        files,
        'If a merge or rebase from the stopped sync is still in progress, finish that one instead of starting another. Keep the intent of both sides; ask me if a conflict needs a decision. Do not push.'
    ].join('\n\n');
}

/**
 * Why "Resolve with agent" can't send right now, or null when it can. The
 * request must land in the chat whose Git panel stopped, so the same rule as
 * review line comments applies, plus: never into a helper agent's chat, and
 * not while a reply is still streaming.
 */
export function resolveWithAgentReason(opts: {
    panelSessionId: string | null | undefined;
    liveSessionId: string | null | undefined;
    isSubagent: boolean;
    streaming: boolean;
}): string | null {
    if (opts.isSubagent) return "A helper agent's chat can't take this. Open its main chat.";
    if (askAgentUnavailableReason(opts.panelSessionId, opts.liveSessionId)) {
        return opts.panelSessionId ? 'Open the chat this sync ran in to hand it to the agent.' : 'Open a chat to hand this to the agent.';
    }
    if (opts.streaming) return 'Available when the current reply finishes.';
    return null;
}
