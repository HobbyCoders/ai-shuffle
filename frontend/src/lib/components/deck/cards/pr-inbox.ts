/**
 * Pure helpers for the cross-project pull request inbox card: the client-side
 * Review / Checks / Project filters and the labels each value shows.
 */
import type { InboxChecks, InboxPullRequest, InboxReview } from '$lib/api/github';

export type ServerState = 'open' | 'merged' | 'closed' | 'all';

export interface InboxFilters {
	review: InboxReview | 'any';
	checks: InboxChecks | 'any';
	/** A project id, or 'any'. */
	project: string;
	/** Hide drafts (only meaningful for open pull requests). */
	hideDrafts: boolean;
}

export const DEFAULT_FILTERS: InboxFilters = { review: 'any', checks: 'any', project: 'any', hideDrafts: false };

export const STATE_OPTIONS: { value: ServerState; label: string }[] = [
	{ value: 'open', label: 'Open' },
	{ value: 'merged', label: 'Merged' },
	{ value: 'closed', label: 'Closed' },
	{ value: 'all', label: 'All' }
];

export const REVIEW_LABELS: Record<InboxReview, string> = {
	approved: 'Approved',
	changes_requested: 'Changes requested',
	review_required: 'Review required',
	none: 'No review'
};

export const CHECKS_LABELS: Record<InboxChecks, string> = {
	passing: 'Checks passing',
	failing: 'Checks failing',
	pending: 'Checks running',
	none: 'No checks'
};

export function filterInbox(items: readonly InboxPullRequest[], filters: InboxFilters): InboxPullRequest[] {
	return items.filter(
		(pr) =>
			(filters.review === 'any' || pr.review === filters.review) &&
			(filters.checks === 'any' || pr.checks === filters.checks) &&
			(filters.project === 'any' || pr.projects.some((p) => p.id === filters.project)) &&
			!(filters.hideDrafts && pr.state === 'draft')
	);
}

/** Every project that appears on a pull request, by name. */
export function inboxProjects(items: readonly InboxPullRequest[]): { id: string; name: string }[] {
	const byId = new Map<string, string>();
	for (const pr of items) for (const p of pr.projects) if (!byId.has(p.id)) byId.set(p.id, p.name);
	return [...byId].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name));
}

/** "5m", "3h", "2d", or a date, for the row's last-activity stamp. */
export function activityAge(iso: string, now: number = Date.now()): string {
	const then = Date.parse(iso);
	if (Number.isNaN(then)) return '';
	const minutes = Math.max(0, Math.floor((now - then) / 60_000));
	if (minutes < 1) return 'now';
	if (minutes < 60) return `${minutes}m`;
	const hours = Math.floor(minutes / 60);
	if (hours < 24) return `${hours}h`;
	const days = Math.floor(hours / 24);
	if (days < 30) return `${days}d`;
	return new Date(then).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
