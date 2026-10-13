/**
 * Startup fan-out benchmark (perf sprint 2026-10, journey 1 offender #1).
 *
 * Replays a desktop launch (`/`, owner signed in) against the REAL startup
 * readers: the API client, preference sync, live sync with its first
 * `sync_ready`, the app-data loaders, the model registry, notifications,
 * voice settings and usage limits. Only the network (fetch, WebSocket) and UI
 * stores are stubbed. The order follows RootLayout (owner + auth probes, then
 * the boot preparation) and AppPage (catalog, independent loads, init), plus
 * the composer's usage and voice reads.
 *
 * It counts every API request in the launch window (through the refresh the
 * first sync_ready schedules) and every URL requested more than once. Lab
 * baseline on 04d9a0cfb: 27 requests before usable and 10 duplicated paths
 * (docs/perf/sprint-2026-10.md). This replay has fewer readers than the live
 * page (no transcript, Git or panel reads), so its absolute count is lower;
 * the ratchet is on the duplicates and on the replay's own count.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const auth = vi.hoisted(() => ({
	listener: undefined as undefined | ((s: unknown) => void),
	status: null as unknown
}));
vi.mock('$lib/stores/auth.svelte', () => ({
	auth: {
		get current() { return { loading: false, status: auth.status }; },
		subscribe: (fn: (s: unknown) => void) => { auth.listener = fn; return () => {}; },
		checkAuth: vi.fn()
	}
}));
vi.mock('$lib/stores/active-session', async () => {
	const { composerPreferences } = await import('$lib/stores/composer-preferences');
	return {
		handleGlobalUserEvent: vi.fn(),
		activeSession: {
			current: { sessionId: null },
			// The part of init that reads the network: the composer defaults.
			init: async () => { await composerPreferences.hydrate(); },
			applyComposerDefaults: vi.fn(),
			resetForAccountChange: vi.fn(),
			newChat: vi.fn(),
			validateSelectedProfile: vi.fn()
		}
	};
});
vi.mock('$lib/stores/session/v2/feed-controller', () => ({
	feedController: { invalidate: vi.fn(), invalidateUnverified: vi.fn(), isFeeding: () => false, hasLogEpoch: () => false, restart: vi.fn(), revive: vi.fn() }
}));
// UI preference stores live-sync imports for their registrations only.
vi.mock('$lib/stores/theme.svelte', () => ({}));
vi.mock('$lib/stores/timezonePreference.svelte', () => ({}));
vi.mock('$lib/stores/chatPreferences.svelte', () => ({}));
vi.mock('$lib/stores/hotkeyPreferences.svelte', () => ({}));
vi.mock('$lib/stores/gitActionPreferences.svelte', () => ({}));
vi.mock('$lib/stores/modelFavorites.svelte', () => ({}));
vi.mock('$lib/stores/files.svelte', () => ({}));
vi.mock('$lib/stores/groups.svelte', () => ({}));
vi.mock('$lib/stores/layout.svelte', () => ({}));
vi.mock('$lib/stores/sidebar-prefs.svelte', () => ({}));
vi.mock('$lib/stores/sessionMode.svelte', () => ({}));
vi.mock('$lib/stores/browser-panel.svelte', () => ({}));

import { getUsageLimits, getTags } from '$lib/api/client';
import { fetchOwnerStatus } from '$lib/api/owner';
import * as authApi from '$lib/api/auth';
import { loadVoiceSettings } from '$lib/api/voice';
import { ensureModelRegistryHydrated } from '$lib/constants/models';
import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { notify } from '$lib/stores/notifications';
import { startLiveSync } from './live-sync';
import { resourceSync } from './resource-sync';

class Socket {
	static OPEN = 1; static CLOSED = 3; static all: Socket[] = [];
	readyState = 0;
	onopen?: () => void; onmessage?: (event: { data: string }) => void; onclose?: (event: { code: number }) => void; onerror?: () => void;
	constructor(public url: string) { Socket.all.push(this); }
	send = vi.fn(); close = vi.fn(() => { this.readyState = 3; });
	emit(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }); }
}

const STATUS = {
	authenticated: true, username: 'owner', is_admin: true, setup_required: false, onboarding_complete: true,
	preference_identity: { user_type: 'admin', user_id: 'owner' }
};
const OWNER = { password_set: true, setup_allowed: false, required: false, trusted_lan: false, login_available: true, desktop_handoff: false, min_length: 8 };
const PREFERENCE_KEYS = ['composer.profile', 'composer.project', 'composer.checkout', 'composer.model', 'composer.effort',
	'composer.permissionMode', 'composer.contextWindowChoice', 'composer.fastMode', 'composer.worktreeIntent',
	'context_breakdown_open', 'context_usage_sections', 'sidebar_collapsed_groups', 'context_usage_hidden_providers', 'install_prompt'];
function body(path: string): unknown {
	if (path === '/owner/status') return OWNER;
	if (path === '/auth/status') return STATUS;
	// An existing account: every synced key is on the server (no migration writes).
	if (path.startsWith('/preferences/all')) return { preferences: Object.fromEntries(PREFERENCE_KEYS.map(k => [k, null])), versions: {} };
	if (path.startsWith('/preferences/')) return { value: null, version: null };
	if (path.startsWith('/sessions/cursor') || path.startsWith('/sessions/paginated')) return { items: [], next_cursor: null, next_cursor_id: null, has_more: false, total: 0 };
	if (path === '/models') return { models: [], providers: [], default: null };
	if (path === '/notifications') return { notifications: [] };
	if (path === '/cluster/projects') return { projects: [] };
	if (path === '/settings/voice' || path.startsWith('/usage/')) return {};
	return [];
}

/** Each request answers after LATENCY ms of fake time. */
const LATENCY = 20;
let requests: string[] = [];
let stop: () => void = () => {};

beforeEach(() => {
	vi.useFakeTimers();
	Socket.all = [];
	requests = [];
	auth.status = null;
	localStorage.clear();
	sessionStorage.clear();
	Object.defineProperty(document, 'hidden', { configurable: true, value: false });
	vi.stubGlobal('WebSocket', Socket);
	vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input).replace(/^.*\/api\/v1/, '');
		requests.push(`${init?.method ?? 'GET'} ${url}`);
		const payload = JSON.stringify(body(url.split('?')[0]));
		return new Promise<Response>(resolve => setTimeout(
			() => resolve(new Response(payload, { status: 200, headers: { 'content-type': 'application/json' } })), LATENCY));
	}));
});
afterEach(() => {
	stop();
	resourceSync.clear();
	vi.useRealTimers();
	vi.unstubAllGlobals();
	vi.unstubAllEnvs();
});

/** RootLayout + AppPage launch; resolves when the sidebar list and composer are ready. */
async function launch(): Promise<void> {
	stop = startLiveSync();
	// RootLayout.refreshAuth: owner status first; the auth probe alongside it
	// where this build sends it early (VITE_PERF_PARALLEL_AUTH_PROBES).
	const owner = fetchOwnerStatus();
	const parallel = 'parallelAuthProbesEnabled' in authApi && (authApi as { parallelAuthProbesEnabled: () => boolean }).parallelAuthProbesEnabled();
	if (parallel) (authApi as unknown as { prefetchAuthStatus: () => void }).prefetchAuthStatus();
	await owner;
	auth.status = await authApi.getAuthStatus();
	auth.listener?.({ loading: false, status: auth.status });
	// RootLayout's init effect, then AppPage's mount once preparation settles.
	(appData as { prefetchSessions?: () => void }).prefetchSessions?.();
	await Promise.allSettled([ensureModelRegistryHydrated(), notify.loadHistory()]);
	// The server answers the socket's subscription during the fan-out.
	Socket.all.at(-1)?.emit({ type: 'sync_ready' });
	const catalog = Promise.all([appData.loadProjects(), appData.loadClusterProjects()]);
	const independent = Promise.all([appData.loadProfiles(), appData.loadSessions(), appData.loadApiUserSessions(), getTags()]);
	await activeSession.init({ deepLinkSessionId: null } as never);
	// The composer dock and the usage store each read usage; voice input and
	// the dock each read voice settings.
	const composer = Promise.allSettled([getUsageLimits(), getUsageLimits(), loadVoiceSettings(), loadVoiceSettings()]);
	await Promise.all([catalog, independent, composer]);
}

async function measure() {
	const done = launch();
	await vi.advanceTimersByTimeAsync(2_000);
	await done;
	// Let the first sync_ready's debounced refresh run and answer.
	await vi.advanceTimersByTimeAsync(1_000);
	const counts = new Map<string, number>();
	for (const r of requests) counts.set(r, (counts.get(r) ?? 0) + 1);
	if (process.env.FANOUT_DEBUG) console.log(requests.join('\n'));
	const duplicates = [...counts].filter(([, n]) => n > 1).map(([r, n]) => `${r} x${n}`);
	return { requests: [...requests], duplicates };
}

describe('startup fan-out (journey 1)', () => {
	it('requests each startup resource once', async () => {
		const { requests: sent, duplicates } = await measure();
		expect(duplicates).toEqual([]);
		// Ratchet: the replay's reads, each once. Raise only with a new reader.
		expect(sent.length).toBeLessThanOrEqual(15);
	});

	it('sends the sidebar list as soon as auth answers, ahead of the page fan-out', async () => {
		const done = launch();
		await vi.advanceTimersByTimeAsync(2_000);
		await done;
		const list = requests.findIndex(r => r.startsWith('GET /sessions/cursor'));
		const auth = requests.indexOf('GET /auth/status');
		const catalog = requests.indexOf('GET /projects');
		expect(list).toBeGreaterThan(auth);
		expect(catalog).toBeGreaterThanOrEqual(0);
		expect(list).toBeLessThan(catalog);
	});

	it('keeps the old fan-out with the kill switches off', async () => {
		vi.stubEnv('VITE_PERF_STARTUP_DEDUPE', '0');
		vi.stubEnv('VITE_PERF_EARLY_SESSION_LIST', '0');
		const { duplicates } = await measure();
		expect(duplicates.length).toBeGreaterThan(0);
	});
});
