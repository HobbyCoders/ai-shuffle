import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { CliConversation } from '$lib/api/cli-history';

const listCliConversations = vi.fn<() => Promise<CliConversation[]>>();
const importCliConversation = vi.fn<(c: CliConversation) => Promise<{ session_id: string; status: string }>>();
vi.mock('$lib/api/cli-history', () => ({
	listCliConversations: () => listCliConversations(),
	importCliConversation: (c: CliConversation) => importCliConversation(c)
}));

import CliHistoryImport from './CliHistoryImport.svelte';

const convo = (id: string, title: string): CliConversation => ({
	jsonl_path: `/home/u/.claude/projects/-work-app/${id}.jsonl`,
	sdk_session_id: id,
	title,
	message_count: 4,
	modified_at: 1_790_000_000,
	cwd: '/work/app',
	project_id: 'proj-app'
});

beforeEach(() => {
	listCliConversations.mockReset();
	importCliConversation.mockReset();
});
afterEach(cleanup);

describe('CliHistoryImport', () => {
	it('renders nothing when there is nothing to import', async () => {
		listCliConversations.mockResolvedValue([]);
		const { container } = render(CliHistoryImport);
		await waitFor(() => expect(listCliConversations).toHaveBeenCalled());
		await Promise.resolve();
		expect(container.textContent?.trim()).toBe('');
	});

	it('stays hidden when the listing is refused', async () => {
		listCliConversations.mockRejectedValue({ status: 403, detail: 'Admin only' });
		const { container } = render(CliHistoryImport);
		await waitFor(() => expect(listCliConversations).toHaveBeenCalled());
		await Promise.resolve();
		expect(container.querySelector('section')).toBeNull();
	});

	it('imports the selected conversations and keeps a failed one for retry', async () => {
		const a = convo('a', 'Fix the login bug');
		const b = convo('b', 'Write tests');
		const c = convo('c', 'Refactor parser');
		listCliConversations.mockResolvedValue([a, b, c]);
		importCliConversation.mockImplementation(async (conversation) => {
			if (conversation.sdk_session_id === 'c') throw { status: 500, detail: 'disk full' };
			return { session_id: `ses-${conversation.sdk_session_id}`, status: 'success' };
		});
		const onimported = vi.fn();
		render(CliHistoryImport, { props: { onimported } });

		// Everything starts selected; the user leaves one out.
		await fireEvent.click(await screen.findByRole('checkbox', { name: /Write tests/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Import 2 conversations' }));

		await waitFor(() => expect(onimported).toHaveBeenCalledWith(1));
		expect(importCliConversation.mock.calls.map(([x]) => x.sdk_session_id)).toEqual(['a', 'c']);
		expect(screen.getByRole('status').textContent).toMatch(/Imported 1 conversation/);
		expect(screen.getByRole('alert').textContent).toMatch(/1 could not be imported: disk full/);
		// The imported row is gone; the failure stays selected, the skipped one stays unselected.
		expect(screen.queryByRole('checkbox', { name: /Fix the login bug/ })).toBeNull();
		expect((screen.getByRole('checkbox', { name: /Refactor parser/ }) as HTMLInputElement).checked).toBe(true);
		expect((screen.getByRole('checkbox', { name: /Write tests/ }) as HTMLInputElement).checked).toBe(false);
	});
});
