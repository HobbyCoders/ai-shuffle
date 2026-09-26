import { describe, it, expect, vi, beforeEach } from 'vitest';

// Bypass is a per-session choice: it must never become the remembered default
// a new session starts from (SEC-13).

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

	it('never carries bypass into the next session', () => {
		setPersistedRunBinding('permissionMode', 'bypassPermissions');
		expect(localStorage.getItem('aihub_lastPermissionMode')).toBe('default');
		expect(composerPreferences.current.permissionMode).toBe('default');
		expect(getPersistedPermissionMode()).toBe('default');
	});

	it('reads an already-stored bypass as default', () => {
		localStorage.setItem('aihub_lastPermissionMode', 'bypassPermissions');
		expect(getPersistedPermissionMode()).toBe('default');
	});

	it('keeps every other mode as chosen', () => {
		setPersistedRunBinding('permissionMode', 'acceptEdits');
		expect(getPersistedPermissionMode()).toBe('acceptEdits');
		expect(rememberablePermissionMode('plan')).toBe('default');
		expect(rememberablePermissionMode(null)).toBeNull();
	});
});
