/**
 * The one-line summaries of setup's optional last step: each part (account,
 * GitHub, earlier conversations) is a row that says where it stands, so the
 * step reads at a glance and opens a part only when the owner wants it.
 */

export type ExtraTone = 'idle' | 'busy' | 'done' | 'error';

export interface ExtraSummary {
	tone: ExtraTone;
	text: string;
}

export interface AccountSummaryInput {
	choice: 'account' | 'invite';
	syncing: boolean;
	joinedOther: boolean;
	networkName?: string | null;
	linked: boolean;
	waitingOnAccount: boolean;
	/** An invite join is under way (its enrollment is pending or syncing). */
	joining: boolean;
	/** An invite join ended without joining (failed, rejected or expired). */
	joinFailed?: boolean;
}

/**
 * Where an invite join stands, read the way the join flow reads it: pending
 * and syncing are under way; any other state but complete did not go through.
 */
export function inviteJoinState(enrolled: boolean, status: string | null | undefined): 'joining' | 'failed' | null {
	if (enrolled || !status || status === 'complete') return null;
	return status === 'pending' || status === 'syncing' ? 'joining' : 'failed';
}

export function accountSummary(s: AccountSummaryInput): ExtraSummary {
	if (s.syncing) return { tone: 'busy', text: 'Copying settings from your other worker…' };
	if (s.joinedOther) return { tone: 'done', text: `Syncing settings with ${s.networkName || 'your other workers'}` };
	if (s.joining) return { tone: 'busy', text: 'Joining with a worker invite…' };
	if (s.joinFailed) return { tone: 'error', text: "Invite didn't go through · open to retry" };
	if (s.waitingOnAccount) return { tone: 'done', text: "Signed in · joining your account's network" };
	if (s.linked) return { tone: 'busy', text: 'Signed in · connecting this worker…' };
	if (s.choice === 'invite') return { tone: 'idle', text: 'Join another worker with its invite' };
	return { tone: 'idle', text: 'Not signed in · for the Android app, other workers and remote access' };
}

export interface GithubSummaryInput {
	loading: boolean;
	authenticated: boolean;
	user: string | null;
	/** A web sign-in code is waiting for approval on GitHub. */
	pending: boolean;
	error: string;
}

export function githubSummary(s: GithubSummaryInput): ExtraSummary {
	if (s.authenticated) return { tone: 'done', text: s.user ? `Connected as @${s.user}` : 'Connected' };
	if (s.pending) return { tone: 'busy', text: 'Waiting for approval on GitHub…' };
	if (s.error) return { tone: 'error', text: 'Not connected · open to see what went wrong' };
	if (s.loading) return { tone: 'busy', text: 'Checking GitHub…' };
	return { tone: 'idle', text: 'Not connected · for your repositories and pull requests' };
}

export interface HistorySummaryInput {
	/** Conversations still listed for import. */
	remaining: number;
	imported: number;
}

const plural = (n: number) => `${n} ${n === 1 ? 'conversation' : 'conversations'}`;

export function historySummary(s: HistorySummaryInput): ExtraSummary {
	if (s.imported && s.remaining) return { tone: 'done', text: `Imported ${s.imported} · ${s.remaining} more found` };
	if (s.imported) return { tone: 'done', text: `Imported ${plural(s.imported)}` };
	return { tone: 'idle', text: `${plural(s.remaining)} found from the Claude Code CLI` };
}
