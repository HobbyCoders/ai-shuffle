/**
 * #811: text typed on the new-chat screen just before the composer is
 * replaced must follow it, even while the editor-to-store draft save is still
 * inside its 100 ms debounce. Svelte can mount the replacement before the old
 * composer's onDestroy saves, so the replacement must not seed itself from a
 * store that has not seen the text yet.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import '$lib/test-stubs/composer-dom';
import { readDraft, writeDraft } from '$lib/stores/session/draft-persistence';
import { bindSessionOwner } from '$lib/stores/session-owner';

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

const TEXT = 'typed right after boot';

beforeEach(async () => {
	localStorage.clear();
	sessionStorage.clear();
	history.replaceState({}, '', '/');
	activeSession.newChat();
	Object.assign(activeSession.current, { sessionId: null, mode: 'chat', profile: 'coding', error: null, worktreeError: null });
	activeSession.setDraft('');
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	activeSession.setDraft('');
	history.replaceState({}, '', '/');
	vi.restoreAllMocks();
});

type EditorHost = HTMLElement & { editor?: { commands: { insertContent(content: string | object[]): boolean; setTextSelection(selection: number | { from: number; to: number }): boolean; deleteSelection(): boolean }; getJSON(): object } };

async function typeInto(container: HTMLElement): Promise<void> {
	const dom = container.querySelector<EditorHost>('.tiptap')!;
	await waitFor(() => expect(dom.editor).toBeTruthy());
	dom.editor!.commands.insertContent(TEXT);
	expect(dom.textContent).toBe(TEXT);
	// Still inside the 100 ms debounce: the store has not seen the text.
	expect(activeSession.current.draft).not.toContain(TEXT);
}

it('a composer mounted in place of one with debounced text opens with that text', async () => {
	const first = render(ChatInput);
	await typeInto(first.container);

	// Boot's New Chat (AppPage handleNewSession) resets the store's draft, and
	// the replacement composer mounts before the old one is destroyed.
	activeSession.newChat();
	const second = render(ChatInput);
	first.unmount();

	const editor = second.container.querySelector<HTMLElement>('.tiptap')!;
	await waitFor(() => expect(editor.textContent).toBe(TEXT));
	expect(activeSession.current.draft).toContain(TEXT);
});

it.each(['plain', 'html', 'document', 'document-text'])('paints a linked %s draft before boot adopts its session, without saving it as a new chat', async (format) => {
	const lines = Array.from({ length: 180 }, (_, i) => `saved line ${i + 1}`).join('\n');
	const draft = format.startsWith('document')
		? JSON.stringify({ type: 'doc', content: (format === 'document' ? lines.split('\n') : [lines]).map(text => ({ type: 'paragraph', content: [{ type: 'text', text }] })) })
		: format === 'html' ? lines.split('\n').map(line => `<p>${line}</p>`).join('') : lines;
	writeDraft('opening-chat', draft);
	writeDraft(null, 'unrelated new-chat draft');
	history.replaceState({}, '', '/?session=opening-chat');
	Object.assign(activeSession.current, { sessionId: null, isRestoringSession: true });

	const view = render(ChatInput);
	// The first committed layout, before the observer's later animation frame.
	expect(view.container.querySelector('.chat-island.wrapped')).not.toBeNull();
	const editor = view.container.querySelector<EditorHost>('.tiptap')!;
	await waitFor(() => expect(editor.textContent).toContain('saved line 180'));
	expect(view.container.querySelector('.chat-island.wrapped')).not.toBeNull();
	await new Promise(resolve => setTimeout(resolve, 150));
	expect(readDraft(null)).toBe('unrelated new-chat draft');
	// Saving the owned document may normalize legacy plain/HTML drafts to JSON.
	expect(readDraft('opening-chat')).toBe(JSON.stringify(editor.editor!.getJSON()));
});

it('unmounting a linked composer before adoption does not overwrite the new-chat draft', async () => {
	writeDraft('opening-chat', 'existing chat draft');
	writeDraft(null, 'unrelated new-chat draft');
	history.replaceState({}, '', '/?session=opening-chat');
	Object.assign(activeSession.current, { sessionId: null, isRestoringSession: true });

	const view = render(ChatInput);
	await waitFor(() => expect(view.container.querySelector('.tiptap')?.textContent).toBe('existing chat draft'));
	const document = JSON.stringify(view.container.querySelector<EditorHost>('.tiptap')!.editor!.getJSON());
	view.unmount();

	expect(readDraft(null)).toBe('unrelated new-chat draft');
	expect(readDraft('opening-chat')).toBe(document);
	expect(activeSession.current.draft).toBe('');
});

it('teardown saves the current editor owner after this composer has switched sessions', async () => {
	const view = render(ChatInput);
	Object.assign(activeSession.current, { sessionId: 'another-chat' });
	activeSession.setDraft('');
	await tick();
	await typeInto(view.container);
	view.unmount();

	expect(activeSession.current.draft).toContain(TEXT);
	expect(readDraft('another-chat')).toContain(TEXT);
	expect(readDraft(null)).toBeNull();
});

it('a composer unmounted inside the draft debounce keeps its text for the next one', async () => {
	const first = render(ChatInput);
	await typeInto(first.container);
	first.unmount();

	expect(activeSession.current.draft).toContain(TEXT);
	const second = render(ChatInput);
	const editor = second.container.querySelector<HTMLElement>('.tiptap')!;
	await waitFor(() => expect(editor.textContent).toBe(TEXT));
});


async function editLinkedDraft(container: HTMLElement): Promise<string> {
	const dom = container.querySelector<EditorHost>('.tiptap')!;
	await waitFor(() => expect(dom.editor).toBeTruthy());
	dom.editor!.commands.insertContent([
		{ type: 'text', text: TEXT },
		{ type: 'fileChip', attrs: { path: 'notes.txt', fullPath: '/uploads/notes.txt', filename: 'notes.txt', type: 'file' } }
	]);
	expect(dom.querySelector('[data-file-chip]')).not.toBeNull();
	return JSON.stringify(dom.editor!.getJSON());
}

function linkedBoot(draft = 'existing chat draft') {
	writeDraft('opening-chat', draft);
	writeDraft('other-chat', 'other chat draft');
	writeDraft(null, 'unrelated new-chat draft');
	history.replaceState({}, '', '/?session=opening-chat');
	Object.assign(activeSession.current, { sessionId: null, isRestoringSession: true });
}

function expectOtherDraftsUntouched(storeDraft = '') {
	expect(readDraft(null)).toBe('unrelated new-chat draft');
	expect(readDraft('other-chat')).toBe('other chat draft');
	expect(activeSession.current.draft).toBe(storeDraft);
}

it('debounces typed rich text and file chips to the linked editor owner before adoption', async () => {
	linkedBoot();
	const view = render(ChatInput);
	const edited = await editLinkedDraft(view.container);
	await new Promise(resolve => setTimeout(resolve, 150));

	expect(readDraft('opening-chat')).toBe(edited);
	expectOtherDraftsUntouched();
});

it('flushes typed linked text and file chips on teardown and restores them on remount before adoption', async () => {
	linkedBoot();
	const first = render(ChatInput);
	const edited = await editLinkedDraft(first.container);
	// No debounce/timer flush: this exercises the real teardown save.
	first.unmount();
	expect(readDraft('opening-chat')).toBe(edited);
	expectOtherDraftsUntouched();
	const second = render(ChatInput);
	const dom = second.container.querySelector<EditorHost>('.tiptap')!;
	await waitFor(() => expect(dom.editor?.getJSON()).toEqual(JSON.parse(edited)));
	expectOtherDraftsUntouched();
});

it('flushes linked edits when the replacement mounts before the old composer is destroyed', async () => {
	linkedBoot();
	const first = render(ChatInput);
	const edited = await editLinkedDraft(first.container);
	await tick(); // registers the changed editor's pending-draft callback
	const second = render(ChatInput);
	first.unmount();
	const dom = second.container.querySelector<EditorHost>('.tiptap')!;
	await waitFor(() => expect(dom.editor?.getJSON()).toEqual(JSON.parse(edited)));
	expectOtherDraftsUntouched();
});

it('does not seed a linked editor from the unrelated null store draft', async () => {
	linkedBoot();
	activeSession.setDraft('unrelated in-memory new-chat draft');
	const view = render(ChatInput);
	await waitFor(() => expect(view.container.querySelector('.tiptap')?.textContent).toBe('existing chat draft'));
	const edited = await editLinkedDraft(view.container);
	view.unmount();
	expect(readDraft('opening-chat')).toBe(edited);
	expectOtherDraftsUntouched('unrelated in-memory new-chat draft');
});

it('keeps linked edits through adoption without migrating an unrelated new-chat draft', async () => {
	linkedBoot();
	const view = render(ChatInput);
	const edited = await editLinkedDraft(view.container);
	Object.assign(activeSession.current, { sessionId: 'opening-chat', isRestoringSession: false });
	activeSession.setDraft('existing chat draft'); // restore commits the previously persisted snapshot
	await tick();
	await new Promise(resolve => setTimeout(resolve, 150));
	expect(readDraft('opening-chat')).toBe(edited);
	expectOtherDraftsUntouched(edited);
	view.unmount();
	expectOtherDraftsUntouched(edited);
});

it('saves the linked owner on switch but never lets its stale timer overwrite the new owner', async () => {
	linkedBoot();
	const view = render(ChatInput);
	const edited = await editLinkedDraft(view.container);
	Object.assign(activeSession.current, { sessionId: 'other-chat', isRestoringSession: false });
	activeSession.setDraft('other chat draft');
	await tick();
	await new Promise(resolve => setTimeout(resolve, 150));
	expect(readDraft('opening-chat')).toBe(edited);
	expect(readDraft('other-chat')).not.toContain(TEXT);
	expect(activeSession.current.draft).not.toContain(TEXT);
	expect(view.container.querySelector('.tiptap')?.textContent).toBe('other chat draft');
	expect(readDraft(null)).toBe('unrelated new-chat draft');
});

it('still debounces ordinary null new-chat edits to the null store and persistence slot', async () => {
	const view = render(ChatInput);
	await typeInto(view.container);
	await new Promise(resolve => setTimeout(resolve, 150));
	expect(readDraft(null)).toContain(TEXT);
	expect(activeSession.current.draft).toContain(TEXT);
});

it('still migrates an ordinary new-chat draft when that composer adopts its created session', async () => {
	const view = render(ChatInput);
	await typeInto(view.container);
	const draft = JSON.stringify(view.container.querySelector<EditorHost>('.tiptap')!.editor!.getJSON());
	Object.assign(activeSession.current, { sessionId: 'created-chat', isRestoringSession: false });
	activeSession.setDraft(draft);
	await tick();
	await new Promise(resolve => setTimeout(resolve, 150));
	expect(readDraft('created-chat')).toBe(draft);
	expect(activeSession.current.draft).toBe(draft);
	expect(readDraft(null)).toBeNull();
});

it('retires boot-owner debounce and teardown writes across an account ownership boundary', async () => {
	linkedBoot();
	const view = render(ChatInput);
	await editLinkedDraft(view.container);
	bindSessionOwner('previous-account', 'next-account');
	writeDraft('opening-chat', 'next account chat draft');
	writeDraft(null, 'next account new-chat draft');
	await new Promise(resolve => setTimeout(resolve, 150));
	view.unmount();
	expect(readDraft('opening-chat')).toBe('next account chat draft');
	expect(readDraft(null)).toBe('next account new-chat draft');
	expect(activeSession.current.draft).toBe('');
});

it.each([
	{ initial: 'original saved draft', flush: 'debounce' },
	{ initial: 'original saved draft', flush: 'teardown' },
	{ initial: '', flush: 'debounce' },
	{ initial: '', flush: 'teardown' }
])('persists linked undo/clear back to "$initial" through $flush after an intermediate save', async ({ initial, flush }) => {
	linkedBoot(initial);
	const first = render(ChatInput);
	const dom = first.container.querySelector<EditorHost>('.tiptap')!;
	await waitFor(() => expect(dom.editor).toBeTruthy());
	const editor = dom.editor!;
	const insertionPoint = initial.length + 1;
	editor.commands.setTextSelection(insertionPoint);
	editor.commands.insertContent(TEXT);
	await new Promise(resolve => setTimeout(resolve, 150));
	expect(readDraft('opening-chat')).toContain(TEXT);
	expectOtherDraftsUntouched();

	// Delete only the added text, returning exactly to the initial text (or
	// clearing an initially empty draft) after its intermediate value was saved.
	editor.commands.setTextSelection({ from: insertionPoint, to: insertionPoint + TEXT.length });
	editor.commands.deleteSelection();
	expect(dom.textContent).toBe(initial);
	const finalDocument = JSON.stringify(editor.getJSON());
	if (flush === 'debounce') {
		await new Promise(resolve => setTimeout(resolve, 150));
		expect(readDraft('opening-chat')).toBe(finalDocument);
	}
	first.unmount();
	expect(readDraft('opening-chat')).toBe(finalDocument);
	expectOtherDraftsUntouched();

	const second = render(ChatInput);
	const restored = second.container.querySelector<EditorHost>('.tiptap')!;
	await waitFor(() => expect(restored.editor?.getJSON()).toEqual(JSON.parse(finalDocument)));
	expect(restored.textContent).toBe(initial);
	expectOtherDraftsUntouched();
});
