/**
 * The app-wide nudge on the receiving worker (#674): shown to the owner only
 * while peer terminals are on and a device waits to link; Review opens the Terminal
 * page; nothing is polled with the feature off.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({ targets: vi.fn(), deviceLinks: vi.fn() }));
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: api };
});
const admin = vi.hoisted(() => ({ value: true }));
vi.mock('$lib/stores/auth.svelte', async () => {
	const { readable } = await import('svelte/store');
	return { isAdmin: readable(admin.value) };
});
const nav = vi.hoisted(() => ({ open: vi.fn() }));
vi.mock('$lib/stores/layout.svelte', async () => {
	const { readable } = await import('svelte/store');
	return { layout: { subscribe: readable({ activeView: { type: 'session' } }).subscribe, openFeaturePage: nav.open } };
});

import PeerTerminalRequestBanner from './PeerTerminalRequestBanner.svelte';
import type { TerminalTargets } from '$lib/api/terminals';
import { terminals } from '$lib/stores/terminals.svelte';
import { peerTerminals } from '$lib/stores/peer-terminals.svelte';

const targets = (peers: boolean): TerminalTargets => ({
	worker: { id: 'w', name: 'Desk', platform: 'linux' }, supported: true, unsupported_reason: null,
	targets: [{ kind: 'worker', worker_id: 'w', name: 'Desk', available: true },
		...(peers ? [{ kind: 'worker', worker_id: 'dev_a', name: 'Laptop', available: true }] : [])],
	shells: [{ name: 'bash', default: true }], default_shell: 'bash', default_folder: null, limit: 8, open: 0,
});

beforeEach(() => { terminals.reset(); peerTerminals.reset(); Object.values(api).forEach((fn) => fn.mockReset()); nav.open.mockReset(); });
afterEach(() => { cleanup(); peerTerminals.reset(); });

it('asks the owner to review a waiting request and opens the Terminal page', async () => {
	api.targets.mockResolvedValue(targets(true));
	api.deviceLinks.mockResolvedValue({ links: [], requests: [{ request_id: 'r1', label: 'Chrome on Linux', worker_id: 'dev_a', worker_name: 'Laptop', expires_in: 500, age_seconds: 10 }] });
	const ui = render(PeerTerminalRequestBanner);
	expect(await ui.findByText('Chrome on Linux wants to link for terminals here')).toBeTruthy();
	await fireEvent.click(ui.getByRole('button', { name: 'Review' }));
	expect(nav.open).toHaveBeenCalledWith('terminal');
	await fireEvent.click(ui.getByRole('button', { name: 'Dismiss' }));
	expect(ui.queryByText('Chrome on Linux wants to link for terminals here')).toBeNull();
});

it('never polls for requests while peer terminals are off', async () => {
	api.targets.mockResolvedValue(targets(false));
	const ui = render(PeerTerminalRequestBanner);
	await waitFor(() => expect(terminals.capability.status).toBe('ready'));
	expect(api.deviceLinks).not.toHaveBeenCalled();
	expect(ui.container.textContent).toBe('');
});
