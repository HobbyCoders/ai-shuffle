/** The Models tab against faked subagent-default replies: default, pinned, no default yet, and a worker without the endpoint (404 → null). */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import AgentModelsTab from './AgentModelsTab.svelte';
import type { SubagentDefault } from '$lib/api/subagentDefault';

afterEach(cleanup);

const DEFAULT: SubagentDefault = { model: 'sonnet', allowed: ['sonnet', 'haiku'] };

function setup(providerModels: Record<string, string>, ...rest: [SubagentDefault | null | undefined] | []) {
	const subagentDefault = rest.length ? rest[0] : DEFAULT;
	const onModelsChange = vi.fn();
	const onDefaultModel = vi.fn();
	const onAllowed = vi.fn();
	render(AgentModelsTab, { providerModels, subagentDefault, onModelsChange, onDefaultModel, onAllowed });
	return { onModelsChange, onDefaultModel, onAllowed };
}

const choice = (name: RegExp) => screen.getByRole('radio', { name });

describe('with the subagent default', () => {
	it('selects "Use the subagent default" when the agent pins no model', () => {
		setup({});
		expect(choice(/Use the subagent default/).getAttribute('aria-checked')).toBe('true');
		expect(choice(/Pin a model/).getAttribute('aria-checked')).toBe('false');
		expect(screen.getByText('Subagent default')).toBeTruthy();
		// No per-provider pickers until the agent is pinned.
		expect(screen.queryByText('Callers on native Claude.')).toBeNull();
	});

	it('selects "Pin" and shows the per-provider pickers for a pinned agent', () => {
		setup({ __native__: 'opus' });
		expect(choice(/Pin a model/).getAttribute('aria-checked')).toBe('true');
		expect(screen.getByText('Callers on native Claude.')).toBeTruthy();
	});

	it('switching to the default clears the pins but keeps the voice leg', async () => {
		const { onModelsChange } = setup({ __native__: 'opus', __voice__: 'sonnet' });
		await fireEvent.click(choice(/Use the subagent default/));
		expect(onModelsChange).toHaveBeenCalledWith({ __voice__: 'sonnet' });
	});

	it('picking "Pin" with nothing pinned opens the pickers without saving anything', async () => {
		const { onModelsChange } = setup({});
		await fireEvent.click(choice(/Pin a model/));
		expect(screen.getByText('Callers on native Claude.')).toBeTruthy();
		expect(onModelsChange).not.toHaveBeenCalled();
	});

	it('moves between the two choices with the arrow keys', async () => {
		const { onModelsChange } = setup({ __native__: 'opus' });
		await fireEvent.keyDown(choice(/Pin a model/), { key: 'ArrowUp' });
		expect(onModelsChange).toHaveBeenCalledWith({});
	});

	it('lists the models agents may choose from and removes one', async () => {
		const { onAllowed } = setup({});
		const group = screen.getByRole('heading', { name: /Models agents may choose from/ }).closest('section')!;
		expect(within(group as HTMLElement).getByText('Default')).toBeTruthy();
		await fireEvent.click(within(group as HTMLElement).getByRole('button', { name: /Remove haiku/i }));
		expect(onAllowed).toHaveBeenCalledWith(['sonnet']);
	});

	it('says when no default is set yet', () => {
		setup({}, { model: null, allowed: [] });
		expect(screen.getByText('No subagent default yet')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Choose' })).toBeTruthy();
		expect(screen.getByText('Choose a default first.')).toBeTruthy();
	});

	it('opens a picker under the default when Change is pressed', async () => {
		setup({});
		const change = screen.getByRole('button', { name: 'Change' });
		await fireEvent.click(change);
		expect(change.getAttribute('aria-expanded')).toBe('true');
		expect(screen.getByRole('button', { name: 'Done' })).toBeTruthy();
	});
});

describe('without the endpoint (404)', () => {
	it('hides the default card and the choice, and keeps the per-provider pickers', () => {
		setup({ __native__: 'opus' }, null);
		expect(screen.queryByText('Subagent default')).toBeNull();
		expect(screen.queryByRole('radiogroup', { name: 'Model for this agent' })).toBeNull();
		expect(screen.queryByText(/Models agents may choose from/)).toBeNull();
		expect(screen.getByText('Callers on native Claude.')).toBeTruthy();
		expect(screen.getByText('When voice hands off.')).toBeTruthy();
	});

	it('shows a quiet loading line until the reply arrives', () => {
		setup({}, undefined);
		expect(screen.getByRole('status').textContent).toMatch(/Loading models/);
	});
});
