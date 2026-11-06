import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const rewind = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', async (importOriginal) => ({ ...(await importOriginal<object>()), executeRewind: rewind }));
const nav = vi.hoisted(() => ({ openSettings: vi.fn() }));
vi.mock('$lib/settings/navigation.svelte', () => nav);
import { aiContentPrefill, supportCaps } from '$lib/stores/support-report.svelte';
import { activeSession } from '$lib/stores/active-session';
import { readDraft } from '$lib/stores/session/draft-persistence';
import { takeUnfinishedRestores } from '$lib/stores/session/restored-draft';
import MessageActions from './MessageActions.svelte';

beforeEach(() => { sessionStorage.clear(); rewind.mockReset(); vi.spyOn(console, 'error').mockImplementation(() => {}); });
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
		await fireEvent.click(screen.getByRole('button', {name:'Restore to composer'}));
		expect(dialog()).toBeNull();
		expect(rewindButton()).toBeNull();
		await waitFor(() => expect(onRewindComplete).toHaveBeenCalledWith(undefined, prompt, ['/uploads/proof.png'], true));
		// The row leaves on its feed event; a second click in between must not
		// hand the same words to the composer again.
		expect((screen.getByRole('button', {name:'Restored'}) as HTMLButtonElement).disabled).toBe(true);
		expect(rewind).toHaveBeenCalledWith('s1', '', false, 'user', null, null, 'unsent-id');
	});
	it('keeps a not-sent row and says why when the restore fails', async () => {
		rewind.mockResolvedValue({success:false, error:'The unsent row is missing'});
		const onRewindComplete = vi.fn();
		render(MessageActions, {...base, notSentMessageId:'unsent-id', onRewindComplete});
		await fireEvent.click(screen.getByRole('button', {name:'Restore to composer'}));
		await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('The unsent row is missing'));
		expect(onRewindComplete).not.toHaveBeenCalled();
	});
	it('keeps restored words in their own chat when the owner changed chats while it was restoring', async () => {
		let answer!: (value: unknown) => void;
		rewind.mockReturnValue(new Promise(resolve => { answer = resolve; }));
		const onRewindComplete = vi.fn();
		const view = render(MessageActions, {...base, notSentMessageId:'unsent-id', onRewindComplete});
		await fireEvent.click(screen.getByRole('button', {name:'Restore to composer'}));
		await view.rerender({sessionId:'another-chat'});
		answer({success:true, unsent_restored:true, restored_prompt:'kept words', restored_upload_paths:[]});
		await waitFor(() => expect(readDraft('s1')).toContain('kept words'));
		expect(onRewindComplete).not.toHaveBeenCalled();
	});
	it('still hands over restored words when the row unmounted while it was restoring', async () => {
		let answer!: (value: unknown) => void;
		rewind.mockReturnValue(new Promise(resolve => { answer = resolve; }));
		const onRewindComplete = vi.fn();
		const view = render(MessageActions, {...base, notSentMessageId:'unsent-id', onRewindComplete});
		await fireEvent.click(screen.getByRole('button', {name:'Restore to composer'}));
		view.unmount();
		answer({success:true, unsent_restored:true, restored_prompt:'kept words', restored_upload_paths:[]});
		await waitFor(() => expect(onRewindComplete).toHaveBeenCalledWith(undefined, 'kept words', [], true));
	});
	it('notes the words before asking and clears the note once the server answers', async () => {
		let answer!: (value: unknown) => void;
		rewind.mockReturnValue(new Promise(resolve => { answer = resolve; }));
		render(MessageActions, {...base, notSentMessageId:'unsent-id', notSentUploadPaths:['/uploads/proof.png'], onRewindComplete: vi.fn()});
		await fireEvent.click(screen.getByRole('button', {name:'Restore to composer'}));
		expect(sessionStorage.getItem('restore-note:s1:unsent-id')).toContain('/uploads/proof.png');
		answer({success:true, unsent_restored:true, restored_prompt:'kept words', restored_upload_paths:[]});
		await waitFor(() => expect(sessionStorage.getItem('restore-note:s1:unsent-id')).toBeNull());
		expect(takeUnfinishedRestores('s1', new Set(), 'a-later-page')).toEqual([]);
	});
	it('drops the note as soon as a lost answer has handed the words over, so a reload cannot hand them back again', async () => {
		vi.useFakeTimers();
		try {
			rewind.mockRejectedValue(new TypeError('Failed to fetch'));
			const onRewindComplete = vi.fn();
			render(MessageActions, {...base, notSentMessageId:'unsent-id', onRewindComplete});
			await fireEvent.click(screen.getByRole('button', {name:'Restore to composer'}));
			await vi.advanceTimersByTimeAsync(0);
			expect(onRewindComplete).toHaveBeenCalledOnce();
			expect(sessionStorage.getItem('restore-note:s1:unsent-id')).toBeNull();
			expect(takeUnfinishedRestores('s1', new Set(), 'a-later-page')).toEqual([]);
			await vi.advanceTimersByTimeAsync(1000);
			expect(onRewindComplete).toHaveBeenCalledOnce();
		} finally { vi.useRealTimers(); }
	});
	it('saves the words to the chat\'s draft when no composer is on screen to take them', async () => {
		vi.useFakeTimers();
		try {
			activeSession.newChat();
			activeSession.current.sessionId = 's1';
			activeSession.setDraft('');
			rewind.mockRejectedValue(new TypeError('Failed to fetch'));
			const onRewindComplete = vi.fn((_warning?: string, prompt?: string, uploads?: string[]) => {
				activeSession.setPendingRestorePrompt(prompt ?? '', uploads);
			});
			render(MessageActions, {...base, notSentMessageId:'unsent-id', onRewindComplete});
			window.dispatchEvent(new Event('beforeunload'));
			await fireEvent.click(screen.getByRole('button', {name:'Restore to composer'}));
			await vi.advanceTimersByTimeAsync(1000);
			expect(onRewindComplete).toHaveBeenCalledOnce();
			expect(sessionStorage.getItem('restore-note:s1:unsent-id')).toBeNull();
			expect(activeSession.current.pendingRestorePrompt).toBeNull();
			const words = base.messageContent;
			expect(activeSession.current.draft).toContain(words);
			expect(readDraft('s1')).toContain(words);
		} finally {
			activeSession.newChat();
			vi.useRealTimers();
		}
	});
	it('waits to see whether the page really goes, and hands the words over once if it stays', async () => {
		vi.useFakeTimers();
		try {
			rewind.mockRejectedValue(new TypeError('Failed to fetch'));
			const onRewindComplete = vi.fn();
			render(MessageActions, {...base, notSentMessageId:'unsent-id', onRewindComplete});
			window.dispatchEvent(new Event('beforeunload'));
			await fireEvent.click(screen.getByRole('button', {name:'Restore to composer'}));
			await vi.advanceTimersByTimeAsync(0);
			expect(onRewindComplete).not.toHaveBeenCalled();
			expect(sessionStorage.getItem('restore-note:s1:unsent-id')).not.toBeNull();
			await vi.advanceTimersByTimeAsync(1000);
			expect(onRewindComplete).toHaveBeenCalledOnce();
			expect(sessionStorage.getItem('restore-note:s1:unsent-id')).toBeNull();
			expect(screen.getByRole('alert').textContent).toContain("Couldn't confirm");
		} finally { vi.useRealTimers(); }
	});
	it.each([
		['a dropped connection', new TypeError('Failed to fetch')],
		['a server error', {detail:'boom', status:502}],
	])('copies the words to the composer once when the outcome is unknown after %s', async (_label, failure) => {
		rewind.mockRejectedValue(failure);
		const onRewindComplete = vi.fn();
		render(MessageActions, {...base, notSentMessageId:'unsent-id', notSentUploadPaths:['/uploads/proof.png'], onRewindComplete});
		await fireEvent.click(screen.getByRole('button', {name:'Restore to composer'}));
		await waitFor(() => expect(onRewindComplete).toHaveBeenCalledTimes(1));
		expect(onRewindComplete).toHaveBeenCalledWith(undefined, base.messageContent, ['/uploads/proof.png'], true);
		expect(screen.getByRole('alert').textContent).toContain("Couldn't confirm");
		expect((screen.getByRole('button', {name:'Restored'}) as HTMLButtonElement).disabled).toBe(true);
	});
	it('leaves the row and the composer alone when the server refuses the restore', async () => {
		rewind.mockRejectedValue({detail:'Not allowed', status:403, message:'Not allowed'});
		const onRewindComplete = vi.fn();
		render(MessageActions, {...base, notSentMessageId:'unsent-id', onRewindComplete});
		await fireEvent.click(screen.getByRole('button', {name:'Restore to composer'}));
		await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
		expect(onRewindComplete).not.toHaveBeenCalled();
		expect((screen.getByRole('button', {name:'Restore to composer'}) as HTMLButtonElement).disabled).toBe(false);
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
	it('rewinds a user message by uuid, chat only, and returns its text to the composer', async () => {
		rewind.mockResolvedValue({ success: true, warning: 'Heads up' });
		const onRewindComplete = vi.fn();
		render(MessageActions, { ...base, messageUuid: 'u-1', onRewindComplete });
		await fireEvent.click(rewindButton()!);
		expect(dialog()!.textContent).toContain('This will remove this message and all messages after it.');
		expect(dialog()!.textContent).toContain('Fix the build');
		// File restore isn't supported, so the dialog offers no files option.
		expect(screen.queryByRole('radio')).toBeNull();
		expect(dialog()!.textContent).not.toContain('Chat & Code');
		await fireEvent.click(screen.getByRole('button', { name: 'Rewind' }));
		await waitFor(() => expect(onRewindComplete).toHaveBeenCalledWith('Heads up', '**Fix** the `build`'));
		expect(rewind).toHaveBeenCalledWith('s1', 'u-1', false, 'user');
		expect(dialog()).toBeNull();
	});

	it('falls back to content anchoring for a live prompt without a uuid', async () => {
		rewind.mockResolvedValue({ success: false, chat_rewound: true });
		const onRewindComplete = vi.fn();
		render(MessageActions, { ...base, userPromptOccurrence: 1, onRewindComplete });
		await fireEvent.click(rewindButton()!);
		await fireEvent.click(screen.getByRole('button', { name: 'Rewind' }));
		await waitFor(() => expect(onRewindComplete).toHaveBeenCalledWith(undefined, '**Fix** the `build`'));
		expect(rewind).toHaveBeenCalledWith('s1', '', false, 'user', 1, '**Fix** the `build`');
	});

	it('notes a sent prompt before asking, so a reload before the answer still gets it back (#1083)', async () => {
		let answer!: (value: unknown) => void;
		rewind.mockReturnValue(new Promise(resolve => { answer = resolve; }));
		render(MessageActions, { ...base, userPromptOccurrence: 0, onRewindComplete: vi.fn() });
		await fireEvent.click(rewindButton()!);
		await fireEvent.click(screen.getByRole('button', { name: 'Rewind' }));
		// A later page whose transcript no longer shows the prompt takes the words.
		const notes = takeUnfinishedRestores('s1', new Set(), 'a-later-page');
		expect(notes.map(n => n.prompt)).toEqual([base.messageContent]);
		answer({ success: true, chat_rewound: true });
		await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
	});

	it('keeps the sent prompt\'s note when the answer is lost, and drops it once answered', async () => {
		rewind.mockRejectedValueOnce(new TypeError('Failed to fetch'));
		render(MessageActions, { ...base, userPromptOccurrence: 0, onRewindComplete: vi.fn() });
		await fireEvent.click(rewindButton()!);
		await fireEvent.click(screen.getByRole('button', { name: 'Rewind' }));
		await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
		expect(Object.keys(sessionStorage).filter(k => k.startsWith('restore-note:s1:sent:'))).toHaveLength(1);

		sessionStorage.clear();
		rewind.mockResolvedValueOnce({ success: true, chat_rewound: true });
		await fireEvent.click(screen.getByRole('button', { name: 'Rewind' }));
		await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
		expect(Object.keys(sessionStorage).filter(k => k.startsWith('restore-note:'))).toEqual([]);
	});

	it('hands a rewound prompt\'s attachments back as uploads, not raw paths (#1084)', async () => {
		const path = '/work/chat/session-uploads/' + 'a'.repeat(64) + '/notes.txt';
		const prompt = `look at this @${path}`;
		rewind.mockResolvedValue({ success: true, chat_rewound: true });
		const onRewindComplete = vi.fn();
		render(MessageActions, { ...base, messageContent: prompt, userPromptOccurrence: 0, onRewindComplete });
		await fireEvent.click(rewindButton()!);
		await fireEvent.click(screen.getByRole('button', { name: 'Rewind' }));
		await waitFor(() => expect(onRewindComplete).toHaveBeenCalledWith(undefined, prompt, [path]));
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
