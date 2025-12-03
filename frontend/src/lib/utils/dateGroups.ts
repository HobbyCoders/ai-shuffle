import type { Session } from '$lib/api/client';

export interface DateGroup {
	label: string;
	key: string;
	sessions: Session[];
	defaultExpanded: boolean;
}

/**
 * Groups sessions by date categories: Today, Yesterday, This Week, This Month, Older
 */
export function groupSessionsByDate(sessions: Session[]): DateGroup[] {
	const now = new Date();
	const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
	const yesterday = new Date(today);
	yesterday.setDate(yesterday.getDate() - 1);
	const weekAgo = new Date(today);
	weekAgo.setDate(weekAgo.getDate() - 7);
	const monthAgo = new Date(today);
	monthAgo.setMonth(monthAgo.getMonth() - 1);

	const groups: Record<string, Session[]> = {
		today: [],
		yesterday: [],
		week: [],
		month: [],
		older: []
	};

	for (const session of sessions) {
		const date = new Date(session.updated_at);
		const sessionDate = new Date(date.getFullYear(), date.getMonth(), date.getDate());

		if (sessionDate >= today) {
			groups.today.push(session);
		} else if (sessionDate >= yesterday) {
			groups.yesterday.push(session);
		} else if (sessionDate >= weekAgo) {
			groups.week.push(session);
		} else if (sessionDate >= monthAgo) {
			groups.month.push(session);
		} else {
			groups.older.push(session);
		}
	}

	const result: DateGroup[] = [];

	if (groups.today.length > 0) {
		result.push({
			label: 'Today',
			key: 'today',
			sessions: groups.today,
			defaultExpanded: true
		});
	}

	if (groups.yesterday.length > 0) {
		result.push({
			label: 'Yesterday',
			key: 'yesterday',
			sessions: groups.yesterday,
			defaultExpanded: true
		});
	}

	if (groups.week.length > 0) {
		result.push({
			label: 'This Week',
			key: 'week',
			sessions: groups.week,
			defaultExpanded: false
		});
	}

	if (groups.month.length > 0) {
		result.push({
			label: 'This Month',
			key: 'month',
			sessions: groups.month,
			defaultExpanded: false
		});
	}

	if (groups.older.length > 0) {
		result.push({
			label: 'Older',
			key: 'older',
			sessions: groups.older,
			defaultExpanded: false
		});
	}

	return result;
}

/**
 * Format relative time for session cards
 */
export function formatRelativeTime(dateStr: string, abbreviated = false): string {
	const date = new Date(dateStr);
	const now = new Date();
	const diff = now.getTime() - date.getTime();
	const minutes = Math.floor(diff / 60000);
	const hours = Math.floor(diff / 3600000);
	const days = Math.floor(diff / 86400000);

	if (minutes < 1) return 'now';
	if (minutes < 60) return abbreviated ? `${minutes}m` : `${minutes}m ago`;
	if (hours < 24) return abbreviated ? `${hours}h` : `${hours}h ago`;
	if (days === 1) return abbreviated ? '1d' : 'Yesterday';
	if (days < 7) return abbreviated ? `${days}d` : `${days}d ago`;
	if (days < 30) {
		const weeks = Math.floor(days / 7);
		return abbreviated ? `${weeks}w` : `${weeks}w ago`;
	}
	return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

/**
 * Format cost with responsive abbreviation
 */
export function formatCost(cost: number | undefined | null, abbreviated = false): string {
	if (cost === undefined || cost === null || cost === 0) return '';
	if (abbreviated && cost >= 10) {
		return `$${Math.round(cost)}`;
	}
	if (abbreviated && cost >= 1) {
		return `$${cost.toFixed(1)}`;
	}
	return `$${cost.toFixed(2)}`;
}

/**
 * Format turn count with responsive abbreviation
 */
export function formatTurns(count: number, abbreviated = false): string {
	if (abbreviated) {
		return `${count}t`;
	}
	return count === 1 ? '1 turn' : `${count} turns`;
}
