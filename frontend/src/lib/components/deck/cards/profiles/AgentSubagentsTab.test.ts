/** The Subagents tab's roster control: All agents | Selected only | None, and the mode helpers behind it. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import AgentSubagentsTab from './AgentSubagentsTab.svelte';
import { invokeModeOf, invokeModePatch, subagentsFact, type AgentInvokeMode, type RosterAgent } from './agentForm';

afterEach(cleanup);

const AGENTS: RosterAgent[] = [
	{ id: 'Explore', name: 'Explore', description: 'Read-only search', is_builtin: true },
	{ id: 'coder', name: 'Coder', description: 'Writes code', is_builtin: false }
];

function setup(mode: AgentInvokeMode, enabled: string[] = [], agents = AGENTS) {
	const onMode = vi.fn();
	const onToggle = vi.fn();
	const onSetEnabled = vi.fn();
	render(AgentSubagentsTab, {
		agents,
		mode,
		enabled,
		hasDefault: true,
		avatarOf: () => 'robot',
		colorOf: () => null,
		onMode,
		onToggle,
		onSetEnabled
	});
	return { onMode, onToggle, onSetEnabled };
}

const choice = (name: string) => screen.getByRole('button', { name });
const pressed = () =>
	screen
		.getAllByRole('button')
		.filter((b) => b.getAttribute('aria-pressed') === 'true')
		.map((b) => b.textContent?.trim());

describe('the roster control', () => {
	it('offers three choices in order', () => {
		setup('all');
		const group = screen.getByRole('group', { name: 'Who it can call' });
		expect([...group.querySelectorAll('button')].map((b) => b.textContent?.trim())).toEqual([
			'All agents',
			'Selected only',
			'None'
		]);
	});

	it.each([
		['all', 'All agents'],
		['custom', 'Selected only'],
		['none', 'None']
	] as const)('shows %s as "%s"', (mode, label) => {
		setup(mode, ['coder']);
		expect(pressed()).toEqual([label]);
	});

	it('reports a pick of None', async () => {
		const { onMode } = setup('custom', ['coder']);
		await fireEvent.click(choice('None'));
		expect(onMode).toHaveBeenCalledWith('none');
	});

	it('reports a pick of Selected only from None', async () => {
		const { onMode } = setup('none');
		await fireEvent.click(choice('Selected only'));
		expect(onMode).toHaveBeenCalledWith('custom');
	});
});

describe('the agents list', () => {
	it('All agents: every switch on and locked', () => {
		setup('all');
		const switches = screen.getAllByRole('switch');
		expect(switches).toHaveLength(2);
		expect(switches.every((s) => s.getAttribute('aria-checked') === 'true')).toBe(true);
		expect(switches.every((s) => (s as HTMLButtonElement).disabled)).toBe(true);
		expect(screen.getByText('All 2')).toBeTruthy();
	});

	it('Selected only: switches follow the selection and Select all / Clear show', async () => {
		const { onToggle, onSetEnabled } = setup('custom', ['coder']);
		expect(screen.getByText('1 of 2')).toBeTruthy();
		await fireEvent.click(screen.getByRole('switch', { name: 'Allow Explore' }));
		expect(onToggle).toHaveBeenCalledWith('Explore');
		await fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
		expect(onSetEnabled).toHaveBeenCalledWith([]);
		expect(screen.queryByText(/Nothing selected/)).toBeNull();
	});

	it('Selected only with nothing selected says how to turn subagents off', () => {
		setup('custom', []);
		expect(screen.getByText('0 of 2')).toBeTruthy();
		expect(screen.getByText('Nothing selected. To turn subagents off, pick None.')).toBeTruthy();
	});

	it('None: no switches, no Select all, one line saying why', () => {
		setup('none');
		expect(screen.queryAllByRole('switch')).toHaveLength(0);
		expect(screen.queryByRole('button', { name: 'Select all' })).toBeNull();
		expect(screen.getByText("This agent can't hand work to other agents.")).toBeTruthy();
		expect(screen.getByText('Task and Swarm are off.')).toBeTruthy();
		// The group count reads None, not a number.
		expect(screen.getByText('None', { selector: '.n' })).toBeTruthy();
	});
});

describe('mode helpers', () => {
	it('reads stored modes and infers legacy ones', () => {
		expect(invokeModeOf('none', [])).toBe('none');
		expect(invokeModeOf('custom', [])).toBe('custom');
		expect(invokeModeOf('all', ['coder'])).toBe('all');
		// Legacy rows: no mode, a non-empty list meant "these", empty meant "all".
		expect(invokeModeOf(undefined, ['coder'])).toBe('custom');
		expect(invokeModeOf(null, [])).toBe('all');
		expect(invokeModeOf('bogus', undefined)).toBe('all');
	});

	it('None clears the selection; the other picks leave it alone', () => {
		expect(invokeModePatch('none')).toEqual({ agent_invoke_mode: 'none', enabled_agents: [] });
		expect(invokeModePatch('custom')).toEqual({ agent_invoke_mode: 'custom' });
		expect(invokeModePatch('all')).toEqual({ agent_invoke_mode: 'all' });
	});

	it('words the header fact', () => {
		expect(subagentsFact('none', 5, 0)).toBe('None');
		expect(subagentsFact('all', 5, 5)).toBe('All 5');
		expect(subagentsFact('custom', 5, 0)).toBe('0 of 5');
		expect(subagentsFact('all', 0, 0)).toBe('None');
	});
});
