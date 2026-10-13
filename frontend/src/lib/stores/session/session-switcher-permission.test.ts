import { describe, it, expect, vi, beforeEach } from 'vitest';

// The last explicit pick is a default for NEW sessions, not an existing binding.

vi.mock('$lib/api/client', () => ({
	api: { get: vi.fn(async () => ({})), put: vi.fn(async () => ({})) }
}));
vi.mock('../app-data.svelte', () => ({
	appData: { get current() { return { sessions: [] }; } }
}));

import {
	getPersistedPermissionMode,
	rememberablePermissionMode,
	setPersistedRunBinding
} from './session-switcher';
import { composerPreferences } from '../composer-preferences';

describe('remembered permission mode', () => {
	beforeEach(() => localStorage.clear());

	it('carries an explicit bypass pick into the next session', () => {
		setPersistedRunBinding('permissionMode', 'bypassPermissions');
		expect(localStorage.getItem('aihub_lastPermissionMode')).toBe('bypassPermissions');
		expect(composerPreferences.current.permissionMode).toBe('bypassPermissions');
		expect(getPersistedPermissionMode()).toBe('bypassPermissions');
	});

	it('preserves an already-stored bypass pick', () => {
		localStorage.setItem('aihub_lastPermissionMode', 'bypassPermissions');
		expect(getPersistedPermissionMode()).toBe('bypassPermissions');
	});

	it('keeps every other mode as chosen', () => {
		setPersistedRunBinding('permissionMode', 'acceptEdits');
		expect(getPersistedPermissionMode()).toBe('acceptEdits');
		expect(rememberablePermissionMode('plan')).toBe('default');
		expect(rememberablePermissionMode(null)).toBeNull();
	});
});
