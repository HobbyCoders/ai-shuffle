/**
 * #1807: a Vault request or one-shot approval waiting in the OPEN
 * chat is shown in the chat (above the composer), not only in the header
 * panel, and Review opens the header's access panel where the private inputs live.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import VaultPrompt from './VaultPrompt.svelte';
import { vaultSummary, type VaultSummary } from '$lib/stores/vault';

function summary(over: Partial<VaultSummary> = {}): VaultSummary {
	return { sessionId: 's1', pending: 0, active: 0, saved: 0, error: false, open: vi.fn(), items: [], ...over };
}

afterEach(() => {
	vaultSummary.set(null);
	cleanup();
});

describe('VaultPrompt', () => {
	it('appears in the open chat when a request is raised, and Review opens the dialog', async () => {
		render(VaultPrompt, { props: { sessionId: 's1' } });
		expect(screen.queryByRole('region', { name: /Vault request waiting/i })).toBeNull();

		const open = vi.fn();
		vaultSummary.set(summary({
			pending: 1, open,
			items: [{ id: 'r1', kind: 'machine', detail: 'Check the ServerWorker container logs' }]
		}));
		await tick();

		const region = screen.getByRole('region', { name: /Vault request waiting/i });
		expect(region.textContent).toContain('Vault access requested');
		expect(region.textContent).toContain('Needs you');
		expect(region.textContent).toContain('SSH machine');
		expect(region.textContent).toContain('Check the ServerWorker container logs');
		await fireEvent.click(screen.getByRole('button', { name: 'Review' }));
		expect(open).toHaveBeenCalledTimes(1);

		vaultSummary.set(summary({ pending: 0 }));
		await tick();
		expect(screen.queryByRole('region', { name: /Vault request waiting/i })).toBeNull();
	});

	it('names a one-shot approval and caps the list', () => {
		vaultSummary.set(summary({
			pending: 5,
			items: [
				{ id: 'a1', kind: 'action', detail: '$ ls /work' },
				{ id: 'a2', kind: 'action', detail: 'GET /zones' },
				{ id: 'a3', kind: 'action', detail: 'Write notes.md' },
				{ id: 'a4', kind: 'action', detail: '$ df -h' },
				{ id: 'a5', kind: 'action', detail: '$ uptime' }
			]
		}));
		render(VaultPrompt, { props: { sessionId: 's1' } });
		const region = screen.getByRole('region', { name: /Vault request waiting/i });
		expect(region.textContent).toContain('Approval waiting for you');
		expect(region.textContent).toContain('$ ls /work');
		expect(region.textContent).not.toContain('$ df -h');
		expect(region.textContent).toContain('and 2 more');
	});

	it('shows nothing for another session', () => {
		vaultSummary.set(summary({ sessionId: 'other', pending: 1, items: [{ id: 'r1', kind: 'folder', detail: 'Read logs' }] }));
		render(VaultPrompt, { props: { sessionId: 's1' } });
		expect(screen.queryByRole('region', { name: /Vault request waiting/i })).toBeNull();
	});
});
