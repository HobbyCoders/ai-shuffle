/** One rule for keeping words typed during a rewind apart from the restored message. */
import { get } from 'svelte/store';
import { restoreMessageDocument } from '$lib/components/chat/tiptap/serialization';
import { activeSession } from '$lib/stores/active-session';
import { sessionEphemeralCache } from '$lib/stores/session-cache';
import { notify } from '$lib/stores/notifications';
import { hasComposerDraft } from './composer-suggestion';
import { flushPendingDraft, readDraft } from './draft-persistence';
import { draftPreview, promptStash } from './prompt-stash';
import { addRestoredWordsToDraft } from './restored-draft';

export function composerWordsOf(draft: string | null | undefined): string {
	return hasComposerDraft(draft) ? draftPreview(draft!, Number.MAX_SAFE_INTEGER) : '';
}

export function chatDraftNow(sessionId: string): string {
	flushPendingDraft();
	const active = get(activeSession);
	return active.sessionId === sessionId
		? active.draft ?? ''
		: sessionEphemeralCache.peekDraft(sessionId) || readDraft(sessionId) || '';
}

export function composerBaseline(sessionId: string): string | null {
	return get(activeSession).sessionId === sessionId ? composerWordsOf(chatDraftNow(sessionId)) : null;
}

export function rewoundWordsKeptApart(
	sessionId: string, baseline: string | null | undefined, prompt: string,
	uploads: string[] = [], draftNow = chatDraftNow(sessionId), references: import('$lib/api/rewind').UploadReference[] = [],
): boolean {
	const now = composerWordsOf(draftNow);
	if (baseline == null || !now || now === baseline || (!prompt && uploads.length === 0)) return false;
	const pushed = promptStash.push(JSON.stringify(restoreMessageDocument(prompt, uploads, references)));
	if (!pushed?.durable) {
		notify.banner('Your rewound message was added after what you typed. Check the composer before you send.', {
			severity: 'warning', persist: false, duration_ms: 12000,
		});
		return false;
	}
	const entry = pushed.entry;
	const banner = notify.banner('Your rewound message is in the prompt stash, so it was kept apart from what you typed.', {
		severity: 'info', persist: false, duration_ms: 12000,
		action: {
			label: 'Add to composer',
			onClick: () => {
				notify.dismissLive(banner);
				if (!promptStash.take(entry.id)) return;
				if (get(activeSession).sessionId === sessionId) activeSession.setPendingRestorePrompt(prompt, uploads, references);
				else addRestoredWordsToDraft(sessionId, prompt, uploads, undefined, references);
			},
		},
	});
	return true;
}
