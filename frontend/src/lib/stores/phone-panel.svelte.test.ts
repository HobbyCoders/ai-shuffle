import { beforeEach, describe, expect, it, vi } from 'vitest';

const fetchStatus = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/emulator', async (orig) => ({
	...(await orig<typeof import('$lib/api/emulator')>()),
	fetchEmulatorStatus: fetchStatus,
}));
import { phonePanel } from './phone-panel.svelte';
import { sidePanel, sessionPanelKey } from './side-panel.svelte';

beforeEach(() => { phonePanel.reset(); fetchStatus.mockReset(); });

describe('phone panel toggle', () => {
	it.each([
		['an answer of any kind', () => Promise.resolve({}), 'available'],
		['a worker without the API (404)', () => Promise.reject({ status: 404 }), 'hidden'],
		['a refused viewer (403)', () => Promise.reject({ status: 403 }), 'hidden'],
		['a server error (still worth explaining)', () => Promise.reject({ status: 500 }), 'available'],
	])('after %s the toggle is %s', async (_label, impl, expected) => {
		fetchStatus.mockImplementation(impl);
		await phonePanel.ensureCapability();
		expect(phonePanel.capability).toBe(expected);
		await phonePanel.ensureCapability();
		expect(fetchStatus).toHaveBeenCalledOnce();
	});

	it('opens and closes the side panel for that chat only', () => {
		phonePanel.toggle('a');
		expect(sidePanel.forScope(sessionPanelKey('a')).active).toBe('phone');
		expect(phonePanel.isOpen('a')).toBe(true);
		expect(phonePanel.isOpen('b')).toBe(false);
		phonePanel.toggle('a');
		expect(sidePanel.forScope(sessionPanelKey('a')).active).toBeNull();
	});
});
