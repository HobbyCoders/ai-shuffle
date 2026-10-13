/** Rendered export controls: private backup and opt-in sharing stay independent. */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/svelte';
import ChatExportActions from './ChatExportActions.svelte';
import { exportSession } from '$lib/api/sessions';
import { notify } from '$lib/stores/notifications';
vi.mock('$lib/api/sessions', () => ({ exportSession: vi.fn() }));
vi.mock('$lib/stores/notifications', () => ({ notify: vi.fn() }));
const backup = () => screen.getByRole('button', { name: 'Download private backup (.jsonl)' });
const sharing = () => screen.getByRole('button', { name: 'Export for sharing (.md)' });
beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

for (const menu of [false, true]) {
	describe(menu ? 'narrow-menu controls' : 'header controls', () => {
		it('labels raw backups private and warns about limited sharing filtering', () => {
			render(ChatExportActions, { sessionId: 'synthetic-session', menu });
			expect(backup().title).toMatch(/may contain secrets.*private.*do not share/i);
			expect(sharing().title).toMatch(/recognized secrets removed; review content before sharing/i);
			if (menu) {
				expect(screen.getByText(/Raw history; may contain secrets/)).toBeTruthy();
				expect(screen.getByText(/Recognized secrets removed; review before sharing/)).toBeTruthy();
			}
		});
		it('keeps the existing raw JSONL request unchanged', async () => {
			render(ChatExportActions, { sessionId: 'synthetic-session', menu });
			await fireEvent.click(backup());
			expect(exportSession).toHaveBeenCalledExactlyOnceWith('synthetic-session', 'jsonl');
		});
		it('requests only opt-in sharing Markdown and reminds the owner to review', async () => {
			render(ChatExportActions, { sessionId: 'synthetic-session', menu });
			await fireEvent.click(sharing());
			expect(exportSession).toHaveBeenCalledExactlyOnceWith('synthetic-session', 'markdown', { shareSafe: true });
			expect(notify).toHaveBeenCalledWith(expect.stringMatching(/review content before sharing/), { severity: 'success' });
		});
		it('disables both choices while downloading, then restores them', async () => {
			let finish!: () => void;
			vi.mocked(exportSession).mockReturnValue(new Promise<void>((resolve) => { finish = resolve; }));
			render(ChatExportActions, { sessionId: 'synthetic-session', menu });
			await fireEvent.click(sharing());
			expect(sharing().getAttribute('aria-busy')).toBe('true');
			expect((sharing() as HTMLButtonElement).disabled).toBe(true);
			expect((backup() as HTMLButtonElement).disabled).toBe(true);
			finish();
			await waitFor(() => expect((backup() as HTMLButtonElement).disabled).toBe(false));
		});
		it('reports a sharing failure and never downloads a raw backup instead', async () => {
			vi.mocked(exportSession).mockRejectedValue(new Error('synthetic failure'));
			render(ChatExportActions, { sessionId: 'synthetic-session', menu });
			await fireEvent.click(sharing());
			expect(exportSession).toHaveBeenCalledTimes(1);
			expect(notify).toHaveBeenCalledWith(expect.stringMatching(/Private backup was not downloaded/), { severity: 'error' });
			expect((sharing() as HTMLButtonElement).disabled).toBe(false);
		});
		it('uses the usual API error detail without fallback', async () => {
			vi.mocked(exportSession).mockRejectedValue({ detail: 'Cannot safely export malformed session JSONL' });
			render(ChatExportActions, { sessionId: 'synthetic-session', menu });
			await fireEvent.click(sharing());
			expect(notify).toHaveBeenCalledWith('Cannot safely export malformed session JSONL', { severity: 'error' });
			expect(exportSession).toHaveBeenCalledTimes(1);
		});
		it('disables both choices without a session', () => {
			render(ChatExportActions, { sessionId: '', menu });
			expect((backup() as HTMLButtonElement).disabled).toBe(true);
			expect((sharing() as HTMLButtonElement).disabled).toBe(true);
		});
	});
}

it('closes the existing menu on start without adding confirmation', async () => {
	const onStart = vi.fn();
	render(ChatExportActions, { sessionId: 'synthetic-session', menu: true, onStart });
	await fireEvent.click(sharing());
	expect(onStart).toHaveBeenCalledOnce();
	expect(screen.queryByRole('dialog')).toBeNull();
});
