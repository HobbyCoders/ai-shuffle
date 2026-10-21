import { beforeEach, describe, expect, it, vi } from 'vitest';

const fetchStatus = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/emulator', async (orig) => ({
	...(await orig<typeof import('$lib/api/emulator')>()),
	fetchEmulatorStatus: fetchStatus,
}));
import { phonePanel, isEmulatorTool } from './phone-panel.svelte';
import { sidePanel, sessionPanelKey } from './side-panel.svelte';

beforeEach(() => { phonePanel.reset(); fetchStatus.mockReset(); });

describe('phone panel', () => {
	it.each([
		['an answer of any kind', () => Promise.resolve({}), 'available'],
		['a worker without the API (404)', () => Promise.reject({ status: 404 }), 'hidden'],
		['a refused viewer (403)', () => Promise.reject({ status: 403 }), 'hidden'],
		['a server error (still worth explaining)', () => Promise.reject({ status: 500 }), 'available'],
	])('after %s the phone is %s', async (_label, impl, expected) => {
		fetchStatus.mockImplementation(impl);
		await phonePanel.ensureCapability();
		expect(phonePanel.capability).toBe(expected);
		await phonePanel.ensureCapability();
		expect(fetchStatus).toHaveBeenCalledOnce();
	});

	it('opens and closes the side panel for that chat only', () => {
		phonePanel.open('a');
		expect(sidePanel.forScope(sessionPanelKey('a')).active).toBe('phone');
		expect(phonePanel.isOpen('a')).toBe(true);
		expect(phonePanel.isOpen('b')).toBe(false);
		phonePanel.close('a');
		expect(sidePanel.forScope(sessionPanelKey('a')).active).toBeNull();
		expect(phonePanel.userClosedFor('a')).toBe(true);
		expect(phonePanel.userClosedFor('b')).toBe(false);
	});

	it('does not count closing another panel as closing the phone', () => {
		sidePanel.open(sessionPanelKey('c'), 'terminal');
		phonePanel.close('c');
		expect(sidePanel.forScope(sessionPanelKey('c')).active).toBe('terminal');
		expect(phonePanel.userClosedFor('c')).toBe(false);
	});

	it('probes before an agent-driven open when nothing has asked yet', async () => {
		fetchStatus.mockResolvedValue({});
		await phonePanel.autoOpenFor('d');
		expect(fetchStatus).toHaveBeenCalledOnce();
		expect(phonePanel.isOpen('d')).toBe(true);
	});

	it('offers "Show phone" only with the emulator API and only for chats on this worker', async () => {
		expect(phonePanel.offeredFor('e')).toBe(false);
		fetchStatus.mockResolvedValue({});
		await phonePanel.ensureCapability();
		expect(phonePanel.offeredFor('e')).toBe(true);
		expect(phonePanel.offeredFor(null)).toBe(false);
		phonePanel.noteRemote('e', true);
		expect(phonePanel.offeredFor('e')).toBe(false);
		await phonePanel.autoOpenFor('e');
		expect(phonePanel.isOpen('e')).toBe(false);
	});

	it.each([
		['Emulator', true],
		['EmulatorScreenshot', true],
		['mcp__builtins__Emulator', true],
		['mcp__fluid_bundled__mcp__builtins__EmulatorScreenshot', true],
		['mcp__fluid_bundled__Emulator', true],
		['mcp__other__Emulator', false],
		['mcp__browser__browser_navigate', false],
		['EmulatorX', false],
		[undefined, false],
	])('isEmulatorTool(%s) is %s', (name, expected) => {
		expect(isEmulatorTool(name)).toBe(expected);
	});
});
