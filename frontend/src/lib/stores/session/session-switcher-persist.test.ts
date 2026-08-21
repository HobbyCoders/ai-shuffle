import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';

/**
 * Phase 3.3 — per-tab session scoping (client-side sessionStorage slot).
 *
 * Design:
 *   - persist writes the value to BOTH (a) sessionStorage under the tab-local
 *     key and (b) the server under the device key `active_session:<device>`
 *     (unchanged).
 *   - load prefers the sessionStorage slot; corrupt/absent → server device key.
 *   - NO server key is ever tab-scoped, so no per-tab DB rows leak.
 */

const apiGetMock = vi.fn();
vi.mock('$lib/api/client', () => ({
	api: {
		get: (path: string) => apiGetMock(path),
		put: vi.fn(async () => ({}))
	}
}));

vi.mock('../app-data.svelte', () => ({
	appData: { get current() { return { sessions: [] }; } }
}));

import { createSessionSwitcher, type SessionSwitcherDeps } from './session-switcher';
import { TAB_ACTIVE_SESSION_KEY } from '../device-id';

const DEVICE = 'device-XYZ';
const DEVICE_KEY = `active_session:${DEVICE}`;

function minimalState(): ActiveSessionState {
	// Only the fields buildPersistValue / loadPersistedSession touch matter here.
	return {
		sessionId: 'sess-1',
		title: 'T',
		profile: 'p',
		project: 'proj',
		modelOverride: null,
		permissionModeOverride: null,
		effortLevelOverride: null,
		contextWindowChoice: 'standard',
		worktreeIntent: 'local',
		worktreeId: null,
		worktreeBranch: '',
		worktreeBaseBranch: 'main',
		selectedWorktreeBranch: null,
		worktreeMode: 'new',
		worktreeDeleted: false
	} as unknown as ActiveSessionState;
}

function makeSwitcher() {
	let state = minimalState();
	const internal: InternalMutableState = {
		pendingTools: new Set<string>(),
		interruptPending: false,
		awaitingNewSession: false,
		pendingRequestId: null,
		stopRequestedBeforeSession: false,
		worktreeCreationGeneration: 0,
		worktreeCreateAbort: null,
		interruptConfirmResolve: null
	};
	const deps: SessionSwitcherDeps = {
		update: (fn) => { state = fn(state); },
		getState: () => state,
		wsSend: vi.fn(() => true),
		internal,
		sessionEphemeralCache: { save: vi.fn(), restore: () => null } as unknown as SessionSwitcherDeps['sessionEphemeralCache'],
		onPersistTimerChanged: vi.fn()
	};
	return createSessionSwitcher(deps);
}

beforeEach(() => {
	localStorage.clear();
	sessionStorage.clear();
	localStorage.setItem('ai-shuffle-device-id', DEVICE);
	apiGetMock.mockReset();
	vi.restoreAllMocks();
});

describe('session-switcher · loadPersistedSession (sessionStorage-first)', () => {
	it('prefers the tab-local sessionStorage slot over the server', async () => {
		sessionStorage.setItem(TAB_ACTIVE_SESSION_KEY, JSON.stringify({ sessionId: 'tab-session', title: 'Tab' }));
		apiGetMock.mockResolvedValue({ key: DEVICE_KEY, value: { sessionId: 'server-session' } });

		const switcher = makeSwitcher();
		const result = await switcher.loadPersistedSession();

		expect(result).toMatchObject({ sessionId: 'tab-session' });
		// Server must NOT be queried when the tab-local slot resolves.
		expect(apiGetMock).not.toHaveBeenCalled();
	});

	it('falls back to the server device key when the tab slot is absent', async () => {
		apiGetMock.mockResolvedValue({ key: DEVICE_KEY, value: { sessionId: 'server-session' } });

		const switcher = makeSwitcher();
		const result = await switcher.loadPersistedSession();

		expect(result).toMatchObject({ sessionId: 'server-session' });
		expect(apiGetMock).toHaveBeenCalledWith(`/preferences/${encodeURIComponent(DEVICE_KEY)}`);
	});

	it('falls back to the server device key when the tab slot is corrupt JSON', async () => {
		sessionStorage.setItem(TAB_ACTIVE_SESSION_KEY, '{not valid json');
		apiGetMock.mockResolvedValue({ key: DEVICE_KEY, value: { sessionId: 'server-session' } });

		const switcher = makeSwitcher();
		const result = await switcher.loadPersistedSession();

		expect(result).toMatchObject({ sessionId: 'server-session' });
		expect(apiGetMock).toHaveBeenCalledWith(`/preferences/${encodeURIComponent(DEVICE_KEY)}`);
	});

	it('never queries a tab-scoped server key', async () => {
		sessionStorage.setItem(TAB_ACTIVE_SESSION_KEY, '{corrupt');
		apiGetMock.mockResolvedValue(null);

		const switcher = makeSwitcher();
		await switcher.loadPersistedSession();

		for (const call of apiGetMock.mock.calls) {
			expect(call[0]).toBe(`/preferences/${encodeURIComponent(DEVICE_KEY)}`);
			expect(call[0]).not.toMatch(new RegExp(`${DEVICE}:`)); // no `<device>:<tab>`
		}
	});

	it('returns null when neither the tab slot nor the server has a value', async () => {
		apiGetMock.mockResolvedValue(null);
		const switcher = makeSwitcher();
		expect(await switcher.loadPersistedSession()).toBeNull();
	});
});

describe('session-switcher · persistSession (writes both slots)', () => {
	it('writes the tab-local sessionStorage slot AND the server device key', async () => {
		const fetchMock = vi.fn(async () => ({ ok: true }));
		global.fetch = fetchMock as unknown as typeof fetch;

		const switcher = makeSwitcher();
		switcher.persistSession();
		// persistSession debounces (500ms); flush timers.
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled(), { timeout: 1500 });

		// (a) sessionStorage slot holds the serialized value.
		const raw = sessionStorage.getItem(TAB_ACTIVE_SESSION_KEY);
		expect(raw).toBeTruthy();
		expect(JSON.parse(raw as string)).toMatchObject({
			sessionId: 'sess-1',
			contextWindowChoice: 'standard'
		});

		// (b) server PUT targets the DEVICE key — never a tab-scoped key.
		const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
		expect(url).toBe(`/api/v1/preferences/${encodeURIComponent(DEVICE_KEY)}`);
		expect(opts.method).toBe('PUT');
		const body = JSON.parse(opts.body as string);
		expect(body.key).toBe(DEVICE_KEY);
		expect(body.value.sessionId).toBe('sess-1');

		switcher.destroy();
	});

	it('still writes the server device key even if sessionStorage throws', async () => {
		const fetchMock = vi.fn(async () => ({ ok: true }));
		global.fetch = fetchMock as unknown as typeof fetch;
		const setItemSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
			throw new Error('quota exceeded');
		});

		const switcher = makeSwitcher();
		switcher.persistSession();
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled(), { timeout: 1500 });

		const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
		expect(url).toBe(`/api/v1/preferences/${encodeURIComponent(DEVICE_KEY)}`);

		setItemSpy.mockRestore();
		switcher.destroy();
	});
});
