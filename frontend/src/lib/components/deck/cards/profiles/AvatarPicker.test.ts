/** Avatar shape (narrow identity PATCH) and colour (agent field) in the Identity card. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ updateAgentIdentity: vi.fn() }));
vi.mock('$lib/api/assistant', async (original) => ({
	...(await original<typeof import('$lib/api/assistant')>()),
	updateAgentIdentity: mocks.updateAgentIdentity
}));

import AvatarPicker from './AvatarPicker.svelte';
import { assistantState, agentIdentities } from '$lib/stores/assistant';
import { AVATARS } from '$lib/api/assistant';

beforeEach(() => {
	vi.clearAllMocks();
	assistantState.set(null);
	agentIdentities.set([{ id: 'agent', name: 'Researcher', avatar_id: 'robot' }]);
	mocks.updateAgentIdentity.mockImplementation(async (id, patch) => ({ id, name: 'Researcher', ...patch }));
});
afterEach(cleanup);

describe('shape', () => {
	it('offers every shipped shape and marks the current one', () => {
		render(AvatarPicker, { agentId: 'agent' });
		for (const avatar of AVATARS) expect(screen.getByRole('button', { name: `${avatar} avatar` })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'robot avatar' }).getAttribute('aria-pressed')).toBe('true');
	});

	it('saves with a narrow identity patch, not a whole-agent save', async () => {
		render(AvatarPicker, { agentId: 'agent' });
		await fireEvent.click(screen.getByRole('button', { name: 'sprout avatar' }));
		await waitFor(() => expect(mocks.updateAgentIdentity).toHaveBeenCalledWith('agent', { avatar_id: 'sprout' }));
		expect((await screen.findByRole('status')).textContent).toContain('Saved');
		expect(screen.getByRole('button', { name: 'sprout avatar' }).getAttribute('aria-pressed')).toBe('true');
	});

	it('keeps built-in avatars read-only', async () => {
		render(AvatarPicker, { agentId: 'agent', builtin: true });
		const button = screen.getByRole('button', { name: 'spark avatar' }) as HTMLButtonElement;
		expect(button.disabled).toBe(true);
		await fireEvent.click(button);
		expect(mocks.updateAgentIdentity).not.toHaveBeenCalled();
	});

	it('shows a failed save with a retry, without claiming success', async () => {
		mocks.updateAgentIdentity.mockRejectedValueOnce({ detail: 'Could not update identity' });
		render(AvatarPicker, { agentId: 'agent' });
		await fireEvent.click(screen.getByRole('button', { name: 'pixel avatar' }));
		expect((await screen.findByRole('alert')).textContent).toContain('Could not update identity');
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		await waitFor(() => expect(mocks.updateAgentIdentity).toHaveBeenCalledTimes(2));
		expect(mocks.updateAgentIdentity).toHaveBeenLastCalledWith('agent', { avatar_id: 'pixel' });
	});
});

describe('colour', () => {
	it('is hidden when the agent API has no avatar_color', () => {
		render(AvatarPicker, { agentId: 'agent', colorSupported: false });
		expect(screen.queryByRole('radiogroup', { name: 'Colour' })).toBeNull();
	});

	it('is hidden for built-ins', () => {
		render(AvatarPicker, { agentId: 'agent', builtin: true, colorSupported: true });
		expect(screen.queryByRole('radiogroup', { name: 'Colour' })).toBeNull();
	});

	it('reports the picked colour and marks the current one', async () => {
		const onColor = vi.fn();
		render(AvatarPicker, { agentId: 'agent', colorSupported: true, color: 'blue', onColor });
		const group = screen.getByRole('radiogroup', { name: 'Colour' });
		expect(group.querySelectorAll('[role="radio"]')).toHaveLength(5);
		expect(screen.getByRole('radio', { name: 'Blue' }).getAttribute('aria-checked')).toBe('true');
		await fireEvent.click(screen.getByRole('radio', { name: 'Amber' }));
		expect(onColor).toHaveBeenCalledWith('warning');
		await fireEvent.keyDown(screen.getByRole('radio', { name: 'Blue' }), { key: 'ArrowRight' });
		expect(onColor).toHaveBeenLastCalledWith('rose');
	});
});
