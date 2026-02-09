/**
 * Tab Utilities
 *
 * Pure utility functions used by the tabs store.
 * Extracted from tabs.ts to reduce file size and improve testability.
 */

/**
 * Format worktree error messages into user-friendly text.
 * Parses common git/worktree errors and provides helpful context.
 */
export function formatWorktreeError(rawError: string, branchName: string): string {
	const lowerError = rawError.toLowerCase();

	// Branch already has a worktree
	if (lowerError.includes('already checked out') || lowerError.includes('is already used by worktree')) {
		return `A worktree already exists for branch '${branchName}'. Select it from Active Worktrees instead.`;
	}

	// Branch doesn't exist
	if (lowerError.includes('not a valid branch') || lowerError.includes('branch not found') ||
	    lowerError.includes('unknown revision') || lowerError.includes('pathspec') && lowerError.includes('did not match')) {
		return `Branch '${branchName}' doesn't exist. Try fetching from remote first or create a new branch.`;
	}

	// Path conflict - folder already exists
	if (lowerError.includes('already exists') && (lowerError.includes('path') || lowerError.includes('folder') || lowerError.includes('directory'))) {
		return `Cannot create worktree: a folder already exists at the target path. Try a different branch name.`;
	}

	// Git lock error
	if (lowerError.includes('.lock') || lowerError.includes('unable to lock') || lowerError.includes('index.lock')) {
		return `Git operation in progress. Please wait a moment and try again.`;
	}

	// Permission errors
	if (lowerError.includes('permission denied') || lowerError.includes('access denied')) {
		return `Permission denied while creating worktree. Check folder permissions.`;
	}

	// Network/fetch errors
	if (lowerError.includes('could not resolve') || lowerError.includes('connection refused') ||
	    lowerError.includes('network') || lowerError.includes('timeout')) {
		return `Network error while creating worktree. Check your connection and try again.`;
	}

	// Generic fallback with original message
	return `Failed to create worktree: ${rawError}`;
}

/**
 * Persistent device ID for multi-device sync.
 * Ensures the same browser/device keeps the same ID across reconnections.
 *
 * Uses the canonical key 'ai-shuffle-device-id' (shared with device.ts).
 * Migrates from the old key 'aihub_device_id' if present.
 */
export function getOrCreateDeviceId(): string {
	const CANONICAL_KEY = 'ai-shuffle-device-id';
	const LEGACY_KEY = 'aihub_device_id';

	if (typeof window === 'undefined') {
		return `device-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
	}

	// Check canonical key first
	let deviceId = localStorage.getItem(CANONICAL_KEY);
	if (deviceId) return deviceId;

	// Migrate from legacy key if present
	const legacyId = localStorage.getItem(LEGACY_KEY);
	if (legacyId) {
		localStorage.setItem(CANONICAL_KEY, legacyId);
		localStorage.removeItem(LEGACY_KEY);
		console.log('[Tabs] Migrated device ID from legacy key:', legacyId);
		return legacyId;
	}

	// Generate new ID
	deviceId = `device-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
	localStorage.setItem(CANONICAL_KEY, deviceId);
	console.log('[Tabs] Generated new device ID:', deviceId);
	return deviceId;
}

/**
 * Generate a unique tab ID.
 */
export function generateTabId(): string {
	return `tab-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}
