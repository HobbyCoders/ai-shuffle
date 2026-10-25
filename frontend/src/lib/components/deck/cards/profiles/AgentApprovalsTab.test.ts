/**
 * The Approvals tab: lists an agent's saved approvals and removes one or all
 * of them through the owner-only permission-rules API.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ get: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, delete: mocks.delete } }));

import AgentApprovalsTab from './AgentApprovalsTab.svelte';

const allowBash = {
	id: 'rule-1',
	profile_id: 'agent-1',
	tool_name: 'Bash',
	tool_pattern: null,
	decision: 'allow',
	created_at: '2026-10-20T14:00:00Z'
};
const denyFetch = {
	id: 'rule-2',
	profile_id: 'agent-1',
	tool_name: 'WebFetch',
	tool_pattern: 'https://example.com/*',
	decision: 'deny',
	created_at: '2026-10-18T09:00:00Z'
};

beforeEach(() => {
	mocks.get.mockReset();
	mocks.delete.mockReset();
});

afterEach(() => cleanup());

function rows() {
	return within(screen.getByRole('list', { name: 'Saved approvals' })).getAllByRole('listitem');
}

async function confirmRemove(row: HTMLElement) {
	await fireEvent.click(within(row).getByRole('button', { name: 'Remove' }));
	expect(within(row).getByText('Remove this rule?')).toBeTruthy();
	await fireEvent.click(within(row).getByRole('button', { name: 'Remove' }));
}

describe('AgentApprovalsTab', () => {
	it('lists each rule with its tool, what it covers, the decision and the day saved', async () => {
		mocks.get.mockResolvedValue([allowBash, denyFetch]);
		render(AgentApprovalsTab, { props: { profileId: 'agent-1' } });

		await waitFor(() => expect(rows()).toHaveLength(2));
		expect(mocks.get).toHaveBeenCalledWith('/agents/agent-1/permission-rules');
		const [first, second] = rows();
		expect(within(first).getByText('Bash')).toBeTruthy();
		expect(within(first).getByText('Every call')).toBeTruthy();
		expect(within(first).getByText('Allow')).toBeTruthy();
		expect(within(first).getByText(/^Saved /)).toBeTruthy();
		expect(within(second).getByText('WebFetch')).toBeTruthy();
		expect(within(second).getByText('https://example.com/*')).toBeTruthy();
		expect(within(second).getByText('Deny')).toBeTruthy();
	});

	it('shows a skeleton while loading', async () => {
		mocks.get.mockReturnValue(new Promise(() => {}));
		render(AgentApprovalsTab, { props: { profileId: 'agent-1' } });
		expect(await screen.findByRole('status', { name: 'Loading saved approvals' })).toBeTruthy();
	});

	it('shows an empty state when the agent has none', async () => {
		mocks.get.mockResolvedValue([]);
		render(AgentApprovalsTab, { props: { profileId: 'agent-1' } });
		expect(await screen.findByText('No saved approvals')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Remove all' })).toBeNull();
	});

	it('asks first, then removes one rule and drops its row', async () => {
		mocks.get.mockResolvedValue([allowBash, denyFetch]);
		mocks.delete.mockResolvedValue(undefined);
		render(AgentApprovalsTab, { props: { profileId: 'agent-1' } });
		await waitFor(() => expect(rows()).toHaveLength(2));

		await fireEvent.click(within(rows()[0]).getByRole('button', { name: 'Remove' }));
		// Nothing is sent until the inline confirm is accepted.
		expect(mocks.delete).not.toHaveBeenCalled();
		await fireEvent.click(within(rows()[0]).getByRole('button', { name: 'Remove' }));

		expect(mocks.delete).toHaveBeenCalledWith('/agents/agent-1/permission-rules/rule-1');
		await waitFor(() => expect(rows()).toHaveLength(1));
		expect(within(rows()[0]).getByText('WebFetch')).toBeTruthy();
	});

	it('Cancel keeps the rule', async () => {
		mocks.get.mockResolvedValue([allowBash]);
		render(AgentApprovalsTab, { props: { profileId: 'agent-1' } });
		await waitFor(() => expect(rows()).toHaveLength(1));

		await fireEvent.click(within(rows()[0]).getByRole('button', { name: 'Remove' }));
		await fireEvent.click(within(rows()[0]).getByRole('button', { name: 'Cancel' }));
		expect(mocks.delete).not.toHaveBeenCalled();
		expect(rows()).toHaveLength(1);
	});

	it('keeps the row and says why when a remove fails', async () => {
		mocks.get.mockResolvedValue([allowBash]);
		mocks.delete.mockRejectedValue({ status: 500, detail: 'Database is busy' });
		render(AgentApprovalsTab, { props: { profileId: 'agent-1' } });
		await waitFor(() => expect(rows()).toHaveLength(1));

		await confirmRemove(rows()[0]);
		expect(await within(rows()[0]).findByRole('alert')).toHaveProperty('textContent', 'Database is busy');
	});

	it('drops a rule that is already gone (removed on another worker)', async () => {
		mocks.get.mockResolvedValue([allowBash, denyFetch]);
		mocks.delete.mockRejectedValue({ status: 404, detail: 'Permission rule not found' });
		render(AgentApprovalsTab, { props: { profileId: 'agent-1' } });
		await waitFor(() => expect(rows()).toHaveLength(2));

		await confirmRemove(rows()[0]);
		await waitFor(() => expect(rows()).toHaveLength(1));
	});

	it('Remove all asks with the count, then clears the list', async () => {
		mocks.get.mockResolvedValue([allowBash, denyFetch]);
		mocks.delete.mockResolvedValue({ deleted_count: 2 });
		render(AgentApprovalsTab, { props: { profileId: 'agent-1' } });
		await waitFor(() => expect(rows()).toHaveLength(2));

		await fireEvent.click(screen.getByRole('button', { name: 'Remove all' }));
		expect(screen.getByText('Remove all 2?')).toBeTruthy();
		expect(mocks.delete).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Remove all' }));

		expect(mocks.delete).toHaveBeenCalledWith('/agents/agent-1/permission-rules');
		expect(await screen.findByText('No saved approvals')).toBeTruthy();
	});

	it('shows a failed Remove all plainly and keeps the rules', async () => {
		mocks.get.mockResolvedValue([allowBash, denyFetch]);
		mocks.delete.mockRejectedValue({ status: 500, detail: 'Sync is paused' });
		render(AgentApprovalsTab, { props: { profileId: 'agent-1' } });
		await waitFor(() => expect(rows()).toHaveLength(2));

		await fireEvent.click(screen.getByRole('button', { name: 'Remove all' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Remove all' }));
		expect(await screen.findByText('Sync is paused')).toBeTruthy();
		expect(rows()).toHaveLength(2);
	});

	it('shows a load failure with a retry', async () => {
		mocks.get.mockRejectedValueOnce({ status: 403, detail: 'Owner access required' }).mockResolvedValueOnce([allowBash]);
		render(AgentApprovalsTab, { props: { profileId: 'agent-1' } });

		const alert = await screen.findByRole('alert');
		expect(alert.textContent).toContain('Only the owner can see or change saved approvals.');
		await fireEvent.click(within(alert).getByRole('button', { name: /Try again/ }));
		await waitFor(() => expect(rows()).toHaveLength(1));
	});

	it('asks to save a draft first and calls nothing', () => {
		render(AgentApprovalsTab, { props: { profileId: null } });
		expect(screen.getByText('Save this agent to see its saved approvals.')).toBeTruthy();
		expect(mocks.get).not.toHaveBeenCalled();
	});
});
