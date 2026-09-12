import { Editor } from '@tiptap/core';
import { get, writable } from 'svelte/store';
import { createLiveDictation } from '$lib/components/chat/tiptap/liveDictation';
import { activeSession } from '$lib/stores/active-session';
import { writeDraft } from '$lib/stores/session/draft-persistence';
import { sessionOwnerEpoch, sessionOwnerBoundary } from '$lib/stores/session-owner';
import { cancelVoiceInput, globalSttState, toggleVoiceInput } from './globalStt';

// Capture belongs to a draft, not to the mounted composer. A detached editor
// retains file chips, user edits and dictation character positions during navigation.
export const composerDictation = writable<{ sessionId: string | null; attached: boolean } | null>(null);
let run: ReturnType<typeof createRun> | null = null;
let ownerEpoch = sessionOwnerEpoch();
sessionOwnerBoundary.subscribe(() => {
	const next = sessionOwnerEpoch();
	if (next !== ownerEpoch) {
		ownerEpoch = next;
		if (run) cancelVoiceInput({ discard: false });
	}
});

function createRun(editor: Editor, sessionId: string | null) {
	const epoch = sessionOwnerEpoch();
	const dictation = createLiveDictation(editor);
	let detached = false;
	let settled = false;
	const current = () => !settled && epoch === sessionOwnerEpoch();
	function persist() {
		if (!current() || editor.isDestroyed) return;
		const draft = JSON.stringify(editor.getJSON());
		activeSession.saveDraftForSession(sessionId, draft);
		writeDraft(sessionId, draft);
		if (detached && get(activeSession).sessionId === sessionId) activeSession.setDraft(draft);
	}
	function finish(discard = false) {
		if (settled) return;
		if (discard && current()) dictation.discard();
		persist();
		settled = true;
		dictation.destroy();
		if (detached) editor.destroy();
		if (run === controller) { run = null; composerDictation.set(null); }
	}
	const controller = {
		sessionId,
		write(text: string) { if (current()) { dictation.write(text); persist(); } },
		finish,
		detach(leaving: Editor) {
			if (settled || detached || editor !== leaving || editor.isDestroyed) return;
			const copy = new Editor({ extensions: editor.options.extensions, content: editor.getJSON() });
			copy.commands.setTextSelection(editor.state.selection.from);
			dictation.moveTo(copy);
			editor = copy;
			detached = true;
			persist();
			composerDictation.set({ sessionId, attached: false });
		},
		attach(next: Editor) {
			if (!current() || !detached || next.isDestroyed) return;
			next.commands.setContent(editor.getJSON());
			dictation.moveTo(next);
			editor.destroy();
			editor = next;
			detached = false;
			composerDictation.set({ sessionId, attached: true });
		}
	};
	return controller;
}

export function startComposerDictation(editor: Editor, sessionId: string | null) {
	const state = get(globalSttState);
	if (editor.isDestroyed || state.isPreparing || state.isRecording || state.isTranscribing) return;
	run?.finish();
	const owner = createRun(editor, sessionId);
	run = owner;
	composerDictation.set({ sessionId, attached: true });
	toggleVoiceInput(owner.write, {
		onText: owner.write,
		onCommit: () => owner.finish(),
		onDiscard: () => owner.finish(true)
	});
}

export function detachComposerDictation(editor: Editor | null | undefined) {
	if (editor) run?.detach(editor);
}

export function attachComposerDictation(editor: Editor | null | undefined, sessionId: string | null) {
	if (editor && run?.sessionId === sessionId) run.attach(editor);
}
