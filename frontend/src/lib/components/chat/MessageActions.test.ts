import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const rewind = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', async (importOriginal) => ({ ...(await importOriginal<object>()), executeRewind: rewind }));
const nav = vi.hoisted(() => ({ openSettings: vi.fn() }));
vi.mock('$lib/settings/navigation.svelte', () => nav);
import { aiContentPrefill } from '$lib/stores/support-report.svelte';
import MessageActions from './MessageActions.svelte';

beforeEach(() => { rewind.mockReset(); vi.spyOn(console, 'error').mockImplementation(() => {}); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const base = { sessionId: 's1', messageContent: '**Fix** the `build`', messageRole: 'user' as const };
const rewindButton = () => screen.queryByTitle('Rewind to before this message');
const dialog = () => screen.queryByRole('dialog');

describe('MessageActions gating', () => {
	it('disables rewind for a steer with an explanation while preserving copy', async () => {
		render(MessageActions, { ...base, rewindEligible: false, messageUuid: 'not-a-checkpoint' });
		const button = screen.getByTitle('Mid-turn messages have no independent rewind checkpoint') as HTMLButtonElement;
		expect(button.disabled).toBe(true);
		expect(screen.getByTitle('Copy')).toBeTruthy();
		await fireEvent.click(button);
		expect(dialog()).toBeNull();
		expect(rewind).not.toHaveBeenCalled();
	});

	it('offers rewind only on a settled user message in a session, and read only when allowed and settled', async () => {
		const view = render(MessageActions, base);
		expect(rewindButton()).toBeTruthy();
		expect(screen.queryByTitle('Read aloud')).toBeNull();
		await view.rerender({ isStreaming: true });
		expect(rewindButton()).toBeNull();
		await view.rerender({ isStreaming: false, sessionId: null });
		expect(rewindButton()).toBeNull();
		await view.rerender({ sessionId: 's1', messageRole: 'assistant', canRead: true });
		expect(rewindButton()).toBeNull();
		expect(screen.getByTitle('Read aloud')).toBeTruthy();
		await view.rerender({ isReading: true });
		expect(screen.getByTitle('Stop reading')).toBeTruthy();
		await view.rerender({ isStreaming: true });
		expect(screen.queryByTitle('Stop reading')).toBeNull();
		await view.rerender({ visible: false });
		expect(screen.queryByTitle('Copy')).toBeNull();
	});

	it('hands the copy callback the text without markdown syntax', async () => {
		const onCopy = vi.fn();
		render(MessageActions, { ...base, messageContent: '# Plan\n\n- **Fix** the `build`\n- see [docs](https://x.test)', onCopy });
		await fireEvent.click(screen.getByTitle('Copy'));
		expect(onCopy).toHaveBeenCalledWith('Plan\n\nFix the build\nsee docs');
	});

	it('hands over the Markdown source verbatim when asked to copy markdown', async () => {
		const onCopy = vi.fn();
		const source = '# Plan\n\n- **Fix** the `build`';
		render(MessageActions, { ...base, messageRole: 'assistant', copyAs: 'markdown', messageContent: source, onCopy });
		await fireEvent.click(screen.getByRole('button', { name: 'Copy message' }));
		expect(onCopy).toHaveBeenCalledWith(source);
	});
});

describe('MessageActions rewind', () => {
	it('rewinds a user message by uuid, restoring files when chosen, and returns its text to the composer', async () => {
		rewind.mockResolvedValue({ success: true, warning: 'Files partly restored' });
		const onRewindComplete = vi.fn();
		render(MessageActions, { ...base, messageUuid: 'u-1', fileCheckpointingEnabled: true, onRewindComplete });
		await fireEvent.click(rewindButton()!);
		expect(dialog()!.textContent).toContain('This will remove this message and all messages after it.');
		expect(dialog()!.textContent).toContain('Fix the build');
		await fireEvent.click(screen.getByRole('radio', { name: /Chat & Code/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Rewind' }));
		await waitFor(() => expect(onRewindComplete).toHaveBeenCalledWith('Files partly restored', '**Fix** the `build`'));
		expect(rewind).toHaveBeenCalledWith('s1', 'u-1', false, 'user', true);
		expect(dialog()).toBeNull();
	});

	it('falls back to content anchoring for a live prompt without a uuid', async () => {
		rewind.mockResolvedValue({ success: false, chat_rewound: true });
		const onRewindComplete = vi.fn();
		render(MessageActions, { ...base, userPromptOccurrence: 1, onRewindComplete });
		await fireEvent.click(rewindButton()!);
		await fireEvent.click(screen.getByRole('button', { name: 'Rewind' }));
		await waitFor(() => expect(onRewindComplete).toHaveBeenCalledWith(undefined, '**Fix** the `build`'));
		expect(rewind).toHaveBeenCalledWith('s1', '', false, 'user', false, 1, '**Fix** the `build`');
	});

	it('refuses to guess without a checkpoint id, shows server failures, and cancel closes the dialog', async () => {
		const view = render(MessageActions, base);
		await fireEvent.click(rewindButton()!);
		await fireEvent.click(screen.getByRole('button', { name: 'Rewind' }));
		expect(dialog()!.textContent).toContain('this message is missing its checkpoint id');
		expect(rewind).not.toHaveBeenCalled();

		await view.rerender({ messageUuid: 'u-2' });
		rewind.mockResolvedValueOnce({ success: false, error: 'Checkpoint not found' });
		await fireEvent.click(screen.getByRole('button', { name: 'Rewind' }));
		await waitFor(() => expect(dialog()!.textContent).toContain('Checkpoint not found'));
		rewind.mockRejectedValueOnce(new Error('network down'));
		await fireEvent.click(screen.getByRole('button', { name: 'Rewind' }));
		await waitFor(() => expect(dialog()!.textContent).toContain('network down'));
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(dialog()).toBeNull();
	});
});

describe('MessageActions: Report AI content', () => {
	it('offers Report on a settled assistant reply and opens the report form with it filled in', async () => {
		const view = render(MessageActions, { ...base, messageRole: 'assistant', messageContent: 'A **hateful** reply' });
		const button = screen.getByRole('button', { name: 'Report AI content' });
		await fireEvent.click(button);
		expect(aiContentPrefill.take()).toEqual({ excerpt: 'A **hateful** reply', sessionId: 's1' });
		expect(nav.openSettings).toHaveBeenCalledWith({ category: 'system', anchor: 'system.reportProblem' });
		await view.rerender({ isStreaming: true });
		expect(screen.queryByRole('button', { name: 'Report AI content' })).toBeNull();
	});

	it('is not offered on the user\'s own messages', () => {
		render(MessageActions, base);
		expect(screen.queryByRole('button', { name: 'Report AI content' })).toBeNull();
	});
});
