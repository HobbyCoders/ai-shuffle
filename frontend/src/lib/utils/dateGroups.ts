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
 * Format time of day (e.g., "9:45 AM")
 */
export function formatTimeOfDay(dateStr: string): string {
	const date = new Date(dateStr);
	return date.toLocaleTimeString('en-US', {
		hour: 'numeric',
		minute: '2-digit',
		hour12: true
	});
}

/**
 * Get status display info for a session
 */
export function getStatusDisplay(status: string, isStreaming: boolean, isOpen: boolean = false): { label: string; color: string } | null {
	if (isStreaming) {
		return { label: 'Processing', color: 'bg-orange-500' };
	}
	if (status === 'active') {
		// Show "Active Session" for open tabs, "Previous Session" for history items
		return { label: isOpen ? 'Active Session' : 'Previous Session', color: 'bg-teal-500' };
	}
	if (status === 'error') {
		return { label: 'Error', color: 'bg-destructive' };
	}
	return null;
}
