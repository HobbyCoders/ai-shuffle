import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const rewind = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', async (importOriginal) => ({ ...(await importOriginal<object>()), executeRewind: rewind }));
const nav = vi.hoisted(() => ({ openSettings: vi.fn() }));
vi.mock('$lib/settings/navigation.svelte', () => nav);
import { aiContentPrefill, supportCaps } from '$lib/stores/support-report.svelte';
import MessageActions from './MessageActions.svelte';

beforeEach(() => { rewind.mockReset(); vi.spyOn(console, 'error').mockImplementation(() => {}); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const base = { sessionId: 's1', messageContent: '**Fix** the `build`', messageRole: 'user' as const };
const rewindButton = () => screen.queryByTitle('Rewind to before this message');
const dialog = () => screen.queryByRole('dialog');

describe('MessageActions gating', () => {
	it.each(['original words', ''])('restores a not-sent row and uploads without invoking transcript rewind semantics: %s', async (prompt) => {
		rewind.mockResolvedValue({success:true, unsent_restored:true, chat_rewound:false,
			restored_prompt:prompt, restored_upload_paths:['/uploads/proof.png']});
		const onRewindComplete = vi.fn();
		render(MessageActions, {...base, notSentMessageId:'unsent-id', onRewindComplete});
		await fireEvent.click(screen.getByTitle('Restore unsent draft'));
		expect(dialog()!.textContent).toContain('Sent history is unchanged');
		await fireEvent.click(screen.getByRole('button', {name:'Rewind'}));
		await waitFor(() => expect(onRewindComplete).toHaveBeenCalledWith(undefined, prompt, ['/uploads/proof.png']));
		expect(rewind).toHaveBeenCalledWith('s1', '', false, 'user', false, null, null, 'unsent-id');
	});
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

describe('MessageActions: Report message', () => {
	const assistant = { ...base, messageRole: 'assistant' as const, messageContent: 'A **hateful** reply' };
	const overflow = () => screen.queryByRole('button', { name: 'More actions' });
	const reportItem = () => screen.queryByRole('menuitem', { name: 'Report message' });
	const nativeApp = (platform?: string) => {
		window.__SHUFFLE_MOBILE__ = {
			apiOrigin: 'https://worker.example',
			token: 'shc_test',
			...(platform ? { native: { platform, bridge: 1, capabilities: [] } } : {})
		};
	};
	beforeEach(() => {
		nav.openSettings.mockClear();
		supportCaps.status = 'ready';
		supportCaps.current = { basic_report: false, can_report_ai_content: true } as any;
	});
	afterEach(() => { delete window.__SHUFFLE_MOBILE__; aiContentPrefill.take(); supportCaps.reset(); });

	it('offers no report entry and no overflow menu on the web UI and desktop app', () => {
		render(MessageActions, assistant);
		expect(overflow()).toBeNull();
		expect(reportItem()).toBeNull();
		expect(screen.queryByRole('button', { name: /report/i })).toBeNull();
		expect(screen.queryByTitle(/report/i)).toBeNull();
	});

	it('offers none in the iOS app either', () => {
		nativeApp('ios');
		render(MessageActions, assistant);
		expect(overflow()).toBeNull();
	});

	it('tucks Report message into the overflow menu of a settled reply in the Android app', async () => {
		nativeApp('android');
		const view = render(MessageActions, assistant);
		expect(screen.queryByRole('button', { name: /report/i })).toBeNull();
		const trigger = overflow()!;
		expect(trigger.getAttribute('aria-haspopup')).toBe('menu');
		expect(trigger.getAttribute('aria-expanded')).toBe('false');
		expect(reportItem()).toBeNull();

		await fireEvent.click(trigger);
		expect(trigger.getAttribute('aria-expanded')).toBe('true');
		const menu = screen.getByRole('menu', { name: 'Message actions' });
		expect(menu.parentElement).toBe(document.body);
		await waitFor(() => expect(document.activeElement).toBe(reportItem()));

		await fireEvent.click(reportItem()!);
		expect(reportItem()).toBeNull();
		expect(aiContentPrefill.take()).toEqual({ excerpt: 'A **hateful** reply', sessionId: 's1' });
		expect(nav.openSettings).toHaveBeenCalledWith({ category: 'system', anchor: 'system.reportProblem' });

		await view.rerender({ isStreaming: true });
		expect(overflow()).toBeNull();
	});

	it('closes on Escape (focus back on the trigger) and on a press outside', async () => {
		nativeApp('android');
		render(MessageActions, assistant);
		const trigger = overflow()!;
		await fireEvent.click(trigger);
		await waitFor(() => expect(document.activeElement).toBe(reportItem()));
		await fireEvent.keyDown(reportItem()!, { key: 'Escape' });
		expect(reportItem()).toBeNull();
		expect(document.activeElement).toBe(trigger);

		await fireEvent.click(trigger);
		expect(reportItem()).toBeTruthy();
		await fireEvent.pointerDown(document.body);
		expect(reportItem()).toBeNull();
		expect(nav.openSettings).not.toHaveBeenCalled();
	});

	it('also serves an Android app from before the bridge named its platform', () => {
		nativeApp();
		render(MessageActions, assistant);
		expect(overflow()).toBeTruthy();
	});

	it('is not offered on the user\'s own messages', () => {
		nativeApp('android');
		render(MessageActions, base);
		expect(overflow()).toBeNull();
	});
});
