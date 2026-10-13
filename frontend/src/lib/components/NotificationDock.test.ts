/**
 * #1823: every notification draws in the one shared dock, under the page
 * header and centred on the chat column, and the dock never sits in a desktop
 * drag region. Geometry in a real browser: tests/verify-notification-dock.mjs.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { flushSync } from 'svelte';
import { writable } from 'svelte/store';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message, isApiError: () => false }));
const authState = vi.hoisted(() => ({ status: null as Record<string, unknown> | null, subs: new Set<(v: unknown) => void>() }));
vi.mock('$lib/stores/auth.svelte', () => {
	const auth = {
		get current() { return { status: authState.status }; },
		subscribe(fn: (v: unknown) => void) { authState.subs.add(fn); fn({ status: authState.status }); return () => authState.subs.delete(fn); },
	};
	const isAdmin = { subscribe(fn: (v: unknown) => void) { fn(true); return () => {}; } };
	return { auth, isAdmin };
});
vi.mock('$lib/api/computerUse', () => ({ getComputerUseStatus: vi.fn(), setComputerUseEnabled: vi.fn(), stopComputerUse: vi.fn() }));
vi.mock('$lib/api/claude-cli', () => ({ dismissClaudeReloginNotice: vi.fn(async () => {}) }));
vi.mock('$lib/settings/navigation.svelte', () => ({ openSettings: vi.fn() }));
const voice = vi.hoisted(() => ({ active: null as any, layout: null as any, sessions: null as any, call: null as any }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: voice.active }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: voice.layout }));
vi.mock('$lib/stores/app-data.svelte', () => ({ sessions: voice.sessions }));
vi.mock('$lib/services/voiceCall', () => ({
	voiceCallState: voice.call, toggleMic: vi.fn(), toggleAgent: vi.fn(), hangup: vi.fn(), reconcileVoiceCall: vi.fn(),
	isCallSurfaceActive: (s: { sessionId?: string; status?: string }, id: string) => !!id && s.sessionId === id && s.status === 'live',
}));
const install = vi.hoisted(() => ({ installable: null as any }));
vi.mock('$lib/stores/pwa.svelte', () => ({ pwa: { promptInstall: vi.fn(async () => false) }, isInstallable: install.installable }));
vi.mock('$lib/stores/install-prompt-preference.svelte', () => ({ installPromptPreference: { isSuppressed: () => false, snooze: vi.fn(), neverShow: vi.fn() } }));
voice.active = writable({ sessionId: 'other' });
voice.layout = writable({ activeView: { type: 'session' } });
voice.sessions = writable([{ id: 'owner', title: 'Fix voice tools' }]);
voice.call = writable({ sessionId: 'owner', status: 'live', micMuted: false, agentMuted: false });
install.installable = writable(true);

const { default: NotificationDock } = await import('./NotificationDock.svelte');
const { default: BannerHost } = await import('./BannerHost.svelte');
const { default: SlotHarness } = await import('$lib/test-stubs/NotificationSlotHarness.svelte');
const { notificationDock, DOCK_ORDER } = await import('$lib/stores/notification-dock.svelte');
const { notify, notifications } = await import('$lib/stores/notifications');
const { computerUse } = await import('$lib/stores/computerUse.svelte');

const setAuth = (status: Record<string, unknown> | null) => {
	authState.status = status;
	for (const fn of authState.subs) fn({ status });
};
const inDock = (el: Element) => !!el.closest('[data-testid="notification-dock"]');

beforeEach(() => {
	api.get.mockReset(); api.post.mockReset();
	setAuth({ authenticated: true, is_admin: true, preview_access: false });
});
afterEach(() => {
	cleanup();
	for (const n of notifications.current.live) notify.dismissLive(n.id);
	vi.useRealTimers();
});

describe('notification dock', () => {
	it('draws a slot in place only while no dock is mounted', async () => {
		const loose = render(SlotHarness, { label: 'Saved' });
		expect(inDock(screen.getByText('Saved'))).toBe(false);
		loose.unmount();
		render(NotificationDock);
		render(SlotHarness, { label: 'Saved' });
		const card = await screen.findByText('Saved');
		expect(inDock(card)).toBe(true);
		expect(screen.getAllByText('Saved')).toHaveLength(1);
	});

	it('orders entries and live banners in one stack, and forgets an unmounted slot', async () => {
		render(NotificationDock);
		notify.banner('Archived 1 session');
		const install = render(SlotHarness, { label: 'Install', order: DOCK_ORDER.install });
		render(SlotHarness, { label: 'Offline', order: DOCK_ORDER.connection });
		await screen.findByText('Install');
		const texts = [...document.querySelectorAll('.dock-entry')].map((e) => e.textContent?.trim());
		expect(texts[0]).toBe('Offline');
		expect(texts[1]).toContain('Archived 1 session');
		expect(texts[2]).toBe('Install');
		install.unmount();
		await waitFor(() => expect(screen.queryByText('Install')).toBeNull());
		expect(notificationDock.entries).toHaveLength(1);
	});

	it('sizes the anchored dock to the chat column and hides the root fallback beside it', async () => {
		notify.banner('Archived 1 session');
		render(BannerHost);
		expect(screen.getByTestId('notification-dock').className).toContain('fallback');
		render(NotificationDock, { span: 812 });
		await waitFor(() => expect(screen.getAllByTestId('notification-dock')).toHaveLength(1));
		const dock = screen.getByTestId('notification-dock');
		expect(dock.className).toContain('anchored');
		expect(dock.style.getPropertyValue('--dock-span')).toBe('812px');
		expect(dock.getAttribute('role')).toBe('region');
	});

	it('runs a banner action from the dock and removes the banner', async () => {
		const undo = vi.fn();
		render(NotificationDock);
		notify.banner('Archived 1 session', { action: { label: 'Undo', onClick: undo } });
		await fireEvent.click(await screen.findByRole('button', { name: 'Undo' }));
		expect(undo).toHaveBeenCalledOnce();
		expect(screen.queryByText('Archived 1 session')).toBeNull();
	});
});

describe('every migrated notification renders through the dock', () => {
	beforeEach(() => { render(NotificationDock, { span: 900 }); });

	it('computer use', async () => {
		vi.spyOn(computerUse, 'start').mockImplementation(() => {});
		vi.spyOn(computerUse, 'stop').mockImplementation(() => {});
		Object.assign(computerUse, { active: true, displayLabel: 'Display 2', action: 'type', secondsSince: 0, busy: true,
			escapeAvailable: true, overlayAvailable: true, stopping: false, connectionLost: false, stopReason: null });
		render((await import('./ComputerUseBanner.svelte')).default);
		expect(inDock(await screen.findByRole('button', { name: 'Stop computer use' }))).toBe(true);
		Object.assign(computerUse, { active: false });
	});

	it('a voice call in another chat', async () => {
		render((await import('./voice/ActiveVoiceCallBar.svelte')).default, { onReturn: vi.fn() });
		expect(inDock(await screen.findByRole('button', { name: 'End voice call' }))).toBe(true);
	});

	it('locked saved credentials', async () => {
		setAuth({ authenticated: true, is_admin: true, credential_storage: { ready: false, state: 'recovery_required' } });
		render((await import('./CredentialStorageBanner.svelte')).default);
		expect(inDock(await screen.findByText('Saved credentials are locked on this install'))).toBe(true);
	});

	it('the Claude Code CLI sign-in notice', async () => {
		setAuth({ authenticated: true, is_admin: true, claude_cli: { relogin_notice: { message: 'Sign out and in again.' } } });
		render((await import('./ClaudeReloginBanner.svelte')).default);
		expect(inDock(await screen.findByText('Sign out and in again.'))).toBe(true);
	});

	it('no Claude sign-in notice while Claude is off', async () => {
		setAuth({
			authenticated: true,
			is_admin: true,
			claude_cli: { relogin_notice: { message: 'Sign out and in again.' } },
			agent_gate: { ready: true, mode: 'off', claude_credential_ready: false }
		});
		render((await import('./ClaudeReloginBanner.svelte')).default);
		await Promise.resolve();
		expect(screen.queryByText('Sign out and in again.')).toBeNull();
	});

	it('the install prompt', async () => {
		vi.useFakeTimers();
		render((await import('./InstallPrompt.svelte')).default);
		flushSync();
		await vi.advanceTimersByTimeAsync(3000);
		flushSync();
		expect(inDock(screen.getByRole('dialog', { name: 'Install AI Shuffle' }))).toBe(true);
	});

	it('the restart notice when work is still running', async () => {
		const worker = { device_id: 'local', device_name: 'Current worker', online: true, supported: true, allow_updates: true, phase: 'ready', version: 'abc123' };
		api.get.mockResolvedValue({ allow_updates: true, update_all_workers: false, workers: [worker] });
		api.post.mockResolvedValue({ results: [{ device_id: 'local', device_name: 'Current worker', accepted: false, reason: 'Busy',
			busy: [{ session_id: 's1', title: 'Fix login', tasks: ['Run tests'] }] }] });
		render((await import('./layout/sidebar/DesktopUpdateButton.svelte')).default);
		await fireEvent.click(await screen.findByRole('button', { name: 'Update and restart' }));
		const notice = await screen.findByRole('dialog', { name: 'Work is still running' });
		expect(inDock(notice)).toBe(true);
		expect(inDock(screen.getByRole('button', { name: 'Update and restart' }))).toBe(false);
	});
});

describe('dock position and drag regions (source)', () => {
	const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
	const dock = read('./NotificationDock.svelte');
	const css = dock.slice(dock.indexOf('<style>'));
	const rule = (selector: string) => {
		const at = css.indexOf(`${selector} {`);
		expect(at, selector).toBeGreaterThan(-1);
		return css.slice(at, css.indexOf('}', at));
	};

	it('is no-drag, and so is every card in it', () => {
		const container = rule('.notification-dock');
		expect(container).toMatch(/-webkit-app-region:\s*no-drag/);
		expect(container).toMatch(/\bapp-region:\s*no-drag/);
		const card = rule('.dock-entry > :global(*)');
		expect(card).toMatch(/-webkit-app-region:\s*no-drag/);
		expect(card).toMatch(/pointer-events:\s*auto/);
	});

	it('sits at the top of the main content, as wide as the chat column, with nothing moving it', () => {
		const anchored = rule('.notification-dock.anchored');
		expect(anchored).toMatch(/position:\s*absolute/);
		expect(anchored).toMatch(/top:\s*var\(--dock-gap\)/);
		expect(anchored).toMatch(/left:\s*0/);
		expect(anchored).toMatch(/width:\s*var\(--dock-span, 100%\)/);
		expect(css).not.toMatch(/transform/);
		// The pointer passes through everything except the cards themselves.
		expect(rule('.dock-stack')).toMatch(/pointer-events:\s*none/);
		expect(rule('.dock-entry')).toMatch(/pointer-events:\s*none/);
	});

	it('is mounted by MainPanel under the header, measured on the chat column, outside the inert surface', () => {
		const panel = read('./layout/MainPanel.svelte');
		const markup = panel.slice(panel.indexOf('<div class="main-panel">'));
		const header = markup.indexOf('<StudioTopBar');
		const content = markup.indexOf('<div class="main-content">');
		const surface = markup.indexOf('<div class="main-surface"');
		const mount = markup.indexOf('<NotificationDock span={chatColumnWidth} />');
		expect(header).toBeGreaterThan(-1);
		expect(content).toBeGreaterThan(header);
		expect(mount).toBeGreaterThan(markup.indexOf('</SessionSidePanel>'));
		expect(markup.slice(surface, markup.indexOf('>', surface))).toContain('bind:clientWidth={chatColumnWidth}');
		expect(markup.slice(mount)).toMatch(/^<NotificationDock span=\{chatColumnWidth\} \/>\s*<\/div>\s*<\/div>/);
	});

	it('leaves no notice positioning itself over the page', () => {
		// The voice call bar floats only after the user drags it out of the dock;
		// those two rules are the only fixed positioning it may have.
		const userMoved = (src: string) => src.replace(/\t\.call-bounds \{[^}]*\}/, '').replace(/\t\.call-float \{[^}]*\}/, '');
		for (const file of ['./ComputerUseBanner.svelte', './voice/ActiveVoiceCallBar.svelte', './InstallPrompt.svelte',
			'./CredentialStorageBanner.svelte', './ClaudeReloginBanner.svelte', './layout/ChatView.svelte']) {
			const src = read(file);
			expect(src, file).toContain('<NotificationSlot');
			if (file !== './layout/ChatView.svelte') expect(userMoved(src), file).not.toMatch(/position:\s*fixed|class="fixed/);
		}
		expect(read('./voice/ActiveVoiceCallBar.svelte')).toMatch(/\{#if position\}[\s\S]*class="call-float"[\s\S]*\{:else\}\s*<NotificationSlot order=\{DOCK_ORDER\.voiceCall\}>/);
		const update = read('./layout/sidebar/DesktopUpdateButton.svelte');
		expect(update).toContain('<NotificationSlot order={DOCK_ORDER.restart}>');
		expect(update.slice(update.indexOf('.update-panel {'))).not.toMatch(/^[^}]*position:\s*fixed/);
	});

	it('stops the top bar dragging while the floating sidebar covers it', () => {
		const app = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../../app.css'), 'utf8');
		expect(app).toMatch(/html\.desktop \.app-body:has\(> \.sidebar-slot\.floating\) \.topbar \{\s*-webkit-app-region: no-drag;\s*app-region: no-drag;/);
	});
});
