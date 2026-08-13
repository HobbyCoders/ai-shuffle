/**
 * Behavior tests for the reworked SpotlightSearch palette.
 *
 * The rework removed the slash-Commands group and added server-side session
 * search (debounced) plus a client-filtered Agents group (loaded from
 * /agents). Profiles and Subagents were unified into a single "Agents" group
 * when subagents stopped being a distinct concept. These mount the real
 * component with its data dependencies mocked and assert:
 *  - the Commands group is gone entirely (no "Commands" heading, ever)
 *  - the Agents group renders and is selectable
 *  - the debounced session search fires exactly once for a burst of keystrokes
 *  - keyboard ArrowDown navigation crosses group boundaries (Chats → Agents →
 *    Quick Actions) using a single global index
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';

// jsdom lacks Element.scrollIntoView; keyboard nav calls it.
if (!('scrollIntoView' in Element.prototype)) {
	(Element.prototype as unknown as { scrollIntoView: () => void }).scrollIntoView = () => {};
}

const mocks = vi.hoisted(() => {
	return {
		apiGet: vi.fn(),
		searchSessionsPaginated: vi.fn()
	};
});

vi.mock('$lib/api/sessions', () => ({
	searchSessionsPaginated: mocks.searchSessionsPaginated
}));

vi.mock('$lib/api/client', () => ({
	api: { get: mocks.apiGet }
}));

vi.mock('$lib/utils/sanitize', () => ({
	sanitizeHtml: (s: string) => s
}));

import SpotlightSearch from './SpotlightSearch.svelte';
import type { Session } from '$lib/api/client';

// Only the fields the palette reads; cast to Session for the prop contract.
const SESSIONS = [
	{ id: 'sess-1', title: 'First chat', updated_at: new Date().toISOString() },
	{ id: 'sess-2', title: 'Second chat', updated_at: new Date().toISOString() }
] as unknown as Session[];

function baseProps(overrides: Record<string, unknown> = {}) {
	return {
		visible: true,
		sessions: SESSIONS,
		currentProjectId: undefined,
		onClose: vi.fn(),
		onSelectSession: vi.fn(),
		onSelectAgent: vi.fn(),
		onNewChat: vi.fn(),
		onOpenSettings: vi.fn(),
		...overrides
	};
}

beforeEach(() => {
	mocks.apiGet.mockReset();
	mocks.searchSessionsPaginated.mockReset();
	mocks.apiGet.mockResolvedValue([
		{ id: 'agent-1', name: 'Explorer', description: 'Searches the codebase' },
		{ id: 'agent-2', name: 'Researcher', description: 'Deep research agent' }
	]);
	mocks.searchSessionsPaginated.mockResolvedValue({
		items: [],
		total: 0,
		offset: 0,
		limit: 10,
		has_more: false
	});
});

afterEach(() => {
	cleanup();
	vi.useRealTimers();
});

describe('SpotlightSearch — groups', () => {
	it('never renders a Commands group and shows a single Agents group', async () => {
		render(SpotlightSearch, { props: baseProps() });
		// Agents are loaded async via api.get('/agents').
		await waitFor(() => expect(screen.getByText('Explorer')).toBeTruthy());

		expect(screen.queryByText('Commands')).toBeNull();
		expect(screen.queryByText('Profiles')).toBeNull();
		expect(screen.queryByText('Subagents')).toBeNull();
		expect(screen.getByText('Chats')).toBeTruthy();
		expect(screen.getByText('Agents')).toBeTruthy();
		expect(screen.getByText('Quick Actions')).toBeTruthy();
		// Agents render from the /agents fetch.
		expect(screen.getByText('Researcher')).toBeTruthy();
	});

	it('selecting an agent fires onSelectAgent with its id', async () => {
		const onSelectAgent = vi.fn();
		render(SpotlightSearch, { props: baseProps({ onSelectAgent }) });
		await waitFor(() => expect(screen.getByText('Explorer')).toBeTruthy());
		await fireEvent.click(screen.getByText('Explorer'));
		expect(onSelectAgent).toHaveBeenCalledWith('agent-1');
	});
});

describe('SpotlightSearch — debounced session search', () => {
	it('fires exactly once for a burst of keystrokes, with the final query', async () => {
		vi.useFakeTimers();
		const { container } = render(SpotlightSearch, { props: baseProps() });
		const input = container.querySelector('input') as HTMLInputElement;

		// Three rapid keystrokes within the debounce window.
		input.value = 'a';
		await fireEvent.input(input);
		input.value = 'ab';
		await fireEvent.input(input);
		input.value = 'abc';
		await fireEvent.input(input);

		expect(mocks.searchSessionsPaginated).not.toHaveBeenCalled();

		await vi.advanceTimersByTimeAsync(300);

		expect(mocks.searchSessionsPaginated).toHaveBeenCalledTimes(1);
		expect(mocks.searchSessionsPaginated).toHaveBeenCalledWith('abc', {
			projectId: undefined,
			limit: 10
		});
	});
});

describe('SpotlightSearch — keyboard navigation across groups', () => {
	it('ArrowDown advances the global index across group boundaries', async () => {
		const { container } = render(SpotlightSearch, { props: baseProps() });
		await waitFor(() => expect(screen.getByText('Explorer')).toBeTruthy());

		// Layout (empty query): 2 recent chats, 2 agents, 2 quick actions.
		// index 0 selected by default.
		const selectedIndexOf = () => {
			const el = container.querySelector('.spotlight-item.selected') as HTMLElement | null;
			return el ? Number(el.getAttribute('data-index')) : -1;
		};

		expect(selectedIndexOf()).toBe(0);

		const win = window;
		for (let i = 1; i <= 3; i++) {
			await fireEvent.keyDown(win, { key: 'ArrowDown' });
			await tick();
			expect(selectedIndexOf()).toBe(i);
		}
		// index 2 is the first agent row — proves navigation crossed Chats → Agents.
		const rowAt2 = container.querySelector('[data-index="2"]') as HTMLElement;
		expect(rowAt2.textContent).toContain('Explorer');
	});
});
