import { afterEach, beforeEach, expect, it, vi, type Mock } from 'vitest';
import { writable, get } from 'svelte/store';
import type { Component } from 'svelte';

const mocks = vi.hoisted(() => ({ goto: vi.fn(), init: vi.fn(), destroy: vi.fn(), hydrate: vi.fn(), history: vi.fn() }));
vi.mock('$app/navigation', () => ({ goto: mocks.goto }));
vi.mock('$lib/stores/theme.svelte', () => ({ theme: { init: vi.fn() } }));
vi.mock('$lib/stores/pwa.svelte', () => ({ pwa: { init: vi.fn(), current: { updateReady: false } } }));
vi.mock('$lib/constants/models', () => ({ ensureModelRegistryHydrated: mocks.hydrate }));
vi.mock('$lib/stores/notifications', () => ({ notify: { loadHistory: mocks.history, banner: vi.fn() } }));
vi.mock('$lib/services/error-reporter', () => ({ installGlobalErrorHandlers: () => () => {} }));
vi.mock('$lib/utils/visual-viewport', () => ({ bindVisualViewport: () => () => {} }));
vi.mock('$lib/services/live-sync', () => ({ startLiveSync: () => () => {} }));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/services/session-navigation', () => ({ openSession: vi.fn() }));
vi.mock('$lib/services/globalStt', () => ({ initGlobalStt: () => () => {} }));
vi.mock('$lib/services/keyboard', () => ({ registerShortcut: () => ({ unregister() {} }) }));
vi.mock('$lib/stores/sessionMode.svelte', () => ({ appMode: { set: vi.fn() } }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: {
	...writable({ sessionId: null }), init: mocks.init, destroy: mocks.destroy, newChat: vi.fn(), resolveComposerDefaultsAfterCatalog: vi.fn(),
	validateSelectedProfile: vi.fn(), reconcileSessionMode: vi.fn()
} }));
vi.mock('$lib/stores/app-data.svelte', () => ({
	appData: { current: { profiles: [], sessions: [] }, loadProfiles: vi.fn(), loadSessions: vi.fn(),
		loadProjects: vi.fn(), loadClusterProjects: vi.fn(), loadApiUserSessions: vi.fn() },
	profiles: writable([]), projects: writable([]), sessions: writable([]), sessionsTagFilter: writable(null)
}));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openChat: vi.fn() },
	activeView: writable({ type: 'session', sessionId: null }), sidebarCollapsed: writable(false), sidebarOverlay: writable(false) }));
vi.mock('$lib/components/InstallPrompt.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/BannerHost.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/ComputerUseBanner.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/layout/AppLayout.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/voice/ActiveVoiceCallBar.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/voice/GlobalVoiceInput.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/KeyboardShortcutsModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/ImportSessionModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/TagManager.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/SpotlightSearch.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/Banner.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('lucide-svelte', async () => {
	const { default: icon } = await import('$lib/test-stubs/EmptyRouteDependency.svelte');
	return { Link2: icon, ShieldCheck: icon, Smartphone: icon, KeyRound: icon, Loader2: icon,
		Check: icon, ExternalLink: icon, RefreshCw: icon, Wallet: icon };
});

let Harness: Component;
let render: typeof import('@testing-library/svelte')['render'];
let cleanup: typeof import('@testing-library/svelte')['cleanup'];
let fireEvent: typeof import('@testing-library/svelte')['fireEvent'];
let waitFor: typeof import('@testing-library/svelte')['waitFor'];
let auth: (typeof import('$lib/stores/auth.svelte'))['auth'];
let router: typeof import('$lib/test-stubs/app-stores');
let statusRequests: Mock<() => Promise<Response>>;
const signedIn = { authenticated: true, setup_required: false, onboarding_complete: true };
const anonymous = { authenticated: false, setup_required: false, onboarding_complete: true };
// A browser that cannot sign in from where it is (no owner status) sees this notice in place.
const DENIED = 'This browser cannot sign in to AI Shuffle at this address.';
const ownerStatus = { password_set: true, setup_allowed: false, required: false, trusted_lan: false,
	login_available: true, desktop_handoff: false, min_length: 12 };
function deferred() {
	let resolve!: (value: Response) => void;
	const promise = new Promise<Response>((done) => { resolve = done; });
	return { promise, resolve };
}
function navigate(path: string) { router.page.update(p => ({ ...p, url: new URL(path, 'http://localhost') })); }
function resume() { document.dispatchEvent(new Event('visibilitychange')); }

beforeEach(async () => {
	vi.resetModules(); vi.clearAllMocks();
	({ render, cleanup, fireEvent, waitFor } = await import('@testing-library/svelte'));
	Object.defineProperty(document, 'hidden', { configurable: true, value: false });
	Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
	statusRequests = vi.fn();
	vi.stubGlobal('fetch', vi.fn((url: string) => url === '/api/v1/auth/status'
		? statusRequests() : Promise.resolve(Response.json([]))));
	mocks.init.mockResolvedValue(undefined); mocks.hydrate.mockResolvedValue(undefined); mocks.history.mockResolvedValue(undefined);
	router = await import('$lib/test-stubs/app-stores');
	mocks.goto.mockImplementation(async (path: string) => navigate(path));
	auth = (await import('$lib/stores/auth.svelte')).auth;
	Harness = (await import('$lib/test-stubs/AuthRouteHarness.svelte')).default;
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it.each(['503', 'network', 'empty-json', 'invalid-boolean', 'html'])('keeps a failed fresh bootstrap out of the local-network notice, then recovers on resume (%s)', async failure => {
	if (failure === '503') statusRequests.mockResolvedValueOnce(Response.json({ detail: 'Unavailable' }, { status: 503 }));
	else if (failure === 'network') statusRequests.mockRejectedValueOnce(new TypeError('Failed to fetch'));
	else if (failure === 'empty-json') statusRequests.mockResolvedValueOnce(Response.json({}));
	else if (failure === 'invalid-boolean') statusRequests.mockResolvedValueOnce(Response.json({ authenticated: 'false' }));
	else statusRequests.mockResolvedValueOnce(new Response('<html>Proxy unavailable</html>', { headers: { 'Content-Type': 'text/html' } }));
	const view = render(Harness);
	await waitFor(() => expect(auth.current.error).not.toBeNull());
	await new Promise(resolve => setTimeout(resolve, 0));
	expect(mocks.goto).not.toHaveBeenCalled();
	expect(mocks.init).not.toHaveBeenCalled();
	expect(view.getByRole('button', { name: 'Try again' })).toBeTruthy();
	statusRequests.mockResolvedValueOnce(Response.json(signedIn));
	resume();
	await waitFor(() => expect(mocks.init).toHaveBeenCalledOnce());
	expect(get(router.page).url.pathname).toBe('/');
	expect(mocks.goto).not.toHaveBeenCalled();
});

it('retries an unavailable bootstrap only on explicit retry or resume, and still honors denial', async () => {
	statusRequests.mockResolvedValueOnce(Response.json({}, { status: 503 }));
	const view = render(Harness);
	await waitFor(() => expect(auth.current.error).not.toBeNull());
	statusRequests.mockResolvedValueOnce(Response.json(anonymous));
	await fireEvent.click(view.getByRole('button', { name: 'Try again' }));
	await waitFor(() => expect(view.getByText(DENIED)).toBeTruthy());
	expect(statusRequests).toHaveBeenCalledTimes(2);
	expect(mocks.goto).not.toHaveBeenCalled(); expect(mocks.init).not.toHaveBeenCalled();
});

it('does not route on an older status continuation while a newer check is pending', async () => {
	statusRequests.mockResolvedValueOnce(Response.json(signedIn));
	render(Harness);
	await waitFor(() => expect(mocks.init).toHaveBeenCalledOnce());
	statusRequests.mockResolvedValueOnce(Response.json(anonymous));
	await auth.checkAuth();
	// Keep the protected route visible to isolate the visibility continuation.
	navigate('/'); mocks.goto.mockClear();
	const older = deferred(), newer = deferred();
	statusRequests.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
	resume();
	const checking = auth.checkAuth();
	older.resolve(Response.json(anonymous));
	await new Promise(resolve => setTimeout(resolve, 0));
	expect(auth.current.loading).toBe(true);
	expect(mocks.goto).not.toHaveBeenCalled();
	newer.resolve(Response.json(signedIn)); await checking;
	await waitFor(() => expect(auth.current.loading).toBe(false));
	expect(mocks.goto).not.toHaveBeenCalled();
});

it.each([
	[signedIn, '/'], [{ ...signedIn, setup_required: true }, '/setup'],
	[{ ...signedIn, onboarding_complete: false }, '/setup'],
	[{ authenticated: true, setup_required: false }, '/']
])('recovers from the local-network notice after a fresh successful check (%j)', async (status, target) => {
	statusRequests.mockResolvedValueOnce(Response.json(anonymous));
	const view = render(Harness);
	await waitFor(() => expect(view.getByText(DENIED)).toBeTruthy());
	expect(mocks.goto).not.toHaveBeenCalled(); expect(mocks.init).not.toHaveBeenCalled();
	statusRequests.mockResolvedValueOnce(Response.json(status));
	resume();
	await waitFor(() => expect(get(router.page).url.pathname).toBe(target));
	await waitFor(() => expect(view.queryByText(DENIED)).toBeNull());
	if (target === '/') await waitFor(() => expect(mocks.init).toHaveBeenCalledOnce());
});

it('replaces a previously authenticated route with the local-network notice when the current worker explicitly denies it', async () => {
	statusRequests.mockResolvedValueOnce(Response.json(signedIn));
	const view = render(Harness); await waitFor(() => expect(mocks.init).toHaveBeenCalledOnce());
	statusRequests.mockResolvedValueOnce(Response.json(anonymous)); resume();
	await waitFor(() => expect(view.getByText(DENIED)).toBeTruthy());
	expect(mocks.goto).not.toHaveBeenCalled();
	expect(get(router.page).url.pathname).toBe('/');
});

it('keeps the existing authenticated page mounted through a failed refresh', async () => {
	statusRequests.mockResolvedValueOnce(Response.json(signedIn));
	render(Harness); await waitFor(() => expect(mocks.init).toHaveBeenCalledOnce());
	statusRequests.mockResolvedValueOnce(Response.json({}, { status: 503 })); resume();
	await waitFor(() => expect(auth.current.error).not.toBeNull());
	expect(mocks.goto).not.toHaveBeenCalled(); expect(mocks.destroy).not.toHaveBeenCalled();
	statusRequests.mockResolvedValueOnce(Response.json(signedIn)); resume();
	await waitFor(() => expect(auth.current.error).toBeNull());
	expect(mocks.init).toHaveBeenCalledOnce(); expect(mocks.destroy).not.toHaveBeenCalled();
});

it('keeps the actual pairing form reachable while status is pending or unavailable', async () => {
	navigate('/pair'); const pending = deferred(); statusRequests.mockReturnValueOnce(pending.promise);
	const view = render(Harness);
	await waitFor(() => expect(view.getByRole('heading', { name: 'Connect this device' })).toBeTruthy());
	pending.resolve(Response.json({}, { status: 503 }));
	await waitFor(() => expect(auth.current.error).not.toBeNull());
	expect(view.getByLabelText('Pairing code')).toBeTruthy();
	expect(mocks.goto).not.toHaveBeenCalled(); expect(mocks.init).not.toHaveBeenCalled();
});

it('does not leave required setup just because onboarding is complete', async () => {
	navigate('/setup'); statusRequests.mockResolvedValueOnce(Response.json({ ...signedIn, setup_required: true }));
	const view = render(Harness);
	await waitFor(() => expect(view.getByText('Setup fixture')).toBeTruthy());
	expect(mocks.goto).not.toHaveBeenCalled(); expect(mocks.init).not.toHaveBeenCalled();
});

it('does not repeat bootstrap metadata requests when their 401 confirms valid worker auth', async () => {
	const confirmation = deferred();
	statusRequests.mockResolvedValueOnce(Response.json(signedIn)).mockReturnValueOnce(confirmation.promise)
		.mockImplementation(() => new Promise(() => {}));
	const { api } = await import('$lib/api/client');
	mocks.history.mockImplementation(() => api.get('/notifications'));
	vi.stubGlobal('fetch', vi.fn((url: string) => url === '/api/v1/auth/status' ? statusRequests()
		: url === '/api/v1/notifications' ? Promise.resolve(Response.json({ detail: 'Unavailable provider' }, { status: 401 }))
		: Promise.resolve(Response.json([]))));
	render(Harness);
	await waitFor(() => expect(statusRequests).toHaveBeenCalledTimes(2));
	confirmation.resolve(Response.json(signedIn));
	await waitFor(() => expect(mocks.init).toHaveBeenCalledOnce());
	expect(mocks.history).toHaveBeenCalledOnce(); expect(mocks.hydrate).toHaveBeenCalledOnce();
	expect(statusRequests).toHaveBeenCalledTimes(2); expect(mocks.goto).not.toHaveBeenCalled();
});

it('shows the owner-password sign-in to a LAN browser and enters the app after it', async () => {
	let signedInNow = false;
	const login = vi.fn(() => { signedInNow = true; return Promise.resolve(Response.json({ status: 'ok' })); });
	vi.stubGlobal('fetch', vi.fn((url: string) => url === '/api/v1/auth/status'
		? Promise.resolve(Response.json(signedInNow ? signedIn : anonymous))
		: url === '/api/v1/owner/status' ? Promise.resolve(Response.json(ownerStatus))
		: url === '/api/v1/owner/login' ? login()
		: Promise.resolve(Response.json([]))));
	const view = render(Harness);
	await waitFor(() => expect(view.getByRole('heading', { name: 'Sign in to AI Shuffle' })).toBeTruthy());
	await fireEvent.input(view.getByLabelText('Owner password'), { target: { value: 'correct horse battery' } });
	await fireEvent.click(view.getByRole('button', { name: 'Sign in' }));
	await waitFor(() => expect(mocks.init).toHaveBeenCalledOnce());
	expect(login).toHaveBeenCalledOnce();
	expect(view.queryByRole('heading', { name: 'Sign in to AI Shuffle' })).toBeNull();
});

it('blocks an owner without a password on the set-password step', async () => {
	vi.stubGlobal('fetch', vi.fn((url: string) => url === '/api/v1/auth/status'
		? Promise.resolve(Response.json({ ...signedIn, is_admin: true }))
		: url === '/api/v1/owner/status'
			? Promise.resolve(Response.json({ ...ownerStatus, password_set: false, required: true, login_available: false }))
			: Promise.resolve(Response.json([]))));
	const view = render(Harness);
	await waitFor(() => expect(view.getByRole('heading', { name: 'Set an owner password' })).toBeTruthy());
	expect(view.getByLabelText('Repeat it')).toBeTruthy();
});

it('shows a fresh install the wizard welcome, then the owner password step, before anything else', async () => {
	try { sessionStorage.clear(); } catch { /* not available */ }
	let created = false;
	const create = vi.fn(() => { created = true; return Promise.resolve(Response.json({ status: 'ok' })); });
	const fresh = { ...ownerStatus, password_set: false, setup_allowed: true, required: true, login_available: false };
	vi.stubGlobal('fetch', vi.fn((url: string) => url === '/api/v1/auth/status'
		? Promise.resolve(Response.json(created ? { ...signedIn, is_admin: true, onboarding_complete: false, setup_required: true }
			: { ...anonymous, onboarding_complete: false, setup_required: true }))
		: url === '/api/v1/owner/status' ? Promise.resolve(Response.json(created ? { ...fresh, password_set: true, setup_allowed: false } : fresh))
		: url === '/api/v1/owner/password' ? create()
		: Promise.resolve(Response.json([]))));
	const view = render(Harness);
	await waitFor(() => expect(view.getByRole('heading', { name: 'Welcome to AI Shuffle' })).toBeTruthy());
	expect(view.getByText('You bring your own AI account')).toBeTruthy();
	expect(view.getByText('Update checks')).toBeTruthy();
	expect(view.getByRole('link', { name: /What is sent/ }).getAttribute('href')).toBe('https://hobbycoders.com/privacy#updates');
	const progress = view.getByRole('navigation', { name: 'Setup progress' });
	for (const label of ['Welcome', 'Password', 'License', 'Mode', 'Sign in', 'Workspace', 'First run', 'Account & GitHub'])
		expect(progress.textContent).toContain(label);
	await fireEvent.click(view.getByRole('button', { name: 'Continue' }));
	await waitFor(() => expect(view.getByRole('heading', { name: 'Set an owner password' })).toBeTruthy());
	await fireEvent.input(view.getByLabelText(/New password/), { target: { value: 'correct horse battery' } });
	await fireEvent.input(view.getByLabelText('Repeat it'), { target: { value: 'correct horse battery' } });
	await fireEvent.click(view.getByRole('button', { name: 'Save password' }));
	await waitFor(() => expect(create).toHaveBeenCalledOnce());
	await waitFor(() => expect(view.queryByRole('heading', { name: 'Set an owner password' })).toBeNull());
	expect(sessionStorage.getItem('ai-shuffle.setup.welcome-seen')).toBe('1');
});
