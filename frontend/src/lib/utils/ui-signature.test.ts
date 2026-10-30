import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { UI_MOUNT, uiComposer, uiFlag, uiSessionTag } from './ui-signature';

describe('ui-signature', () => {
	it('tags a bound session with a short stable hash, never the id', () => {
		const id = '7f3c2a10-8b44-4d1e-9a51-2f6c0b9d1e77';
		const tag = uiSessionTag(id);
		expect(tag).toMatch(/^s:[0-9a-z]{6}$/);
		expect(uiSessionTag(id)).toBe(tag);
		expect(tag).not.toContain(id.slice(0, 6));
		expect(uiSessionTag('7f3c2a10-8b44-4d1e-9a51-2f6c0b9d1e78')).not.toBe(tag);
		expect(uiSessionTag(null)).toBe('none');
		expect(uiSessionTag(undefined)).toBe('none');
		expect(uiSessionTag('')).toBe('none');
	});

	it('places the composer', () => {
		expect(uiComposer({ archived: false, emptyHero: true })).toBe('centre');
		expect(uiComposer({ archived: false, emptyHero: false })).toBe('bottom');
		expect(uiComposer({ archived: true, emptyHero: true })).toBe('hidden');
	});

	it('spells flags as strings and names the mount markers', () => {
		expect([uiFlag(true), uiFlag(0), uiFlag(undefined)]).toEqual(['true', 'false', 'false']);
		expect(Object.values(UI_MOUNT)).toEqual(['ChatView', 'ChatInput', 'NewSessionHero', 'MessageArea', 'SidePanel']);
	});
});

// The flight recorder (docs/internal/flight-recorder.md) reads these off the
// rendered page. Mounting ChatView or MessageArea pulls in the whole session
// graph, so the attributes are pinned at the source level, on the element
// each one describes.
describe('flight-recorder attributes stay on their elements', () => {
	const read = (path: string) => readFileSync(resolve(process.cwd(), 'src/lib/components', path), 'utf8');
	/** The opening tag that starts with `start`, up to the `>` that ends its last line. */
	function openingTag(source: string, start: string): string {
		const from = source.indexOf(start);
		expect(from, `${start} not found`).toBeGreaterThan(-1);
		const end = source.indexOf('>\n', from);
		expect(end).toBeGreaterThan(from);
		return source.slice(from, end + 1);
	}

	it('puts the whole signature on ChatView root, the state carrier', () => {
		const tag = openingTag(read('layout/ChatView.svelte'), '<div class="chat-view"');
		for (const attribute of [
			'data-ui-state={UI_STATE_CHAT}',
			'data-ui-mount={UI_MOUNT.chatView}',
			'data-ui-surface={paintedSurface}',
			'data-ui-session={!isEmptyState && !mirrorSync.waiting && transcriptSurface !== null ? uiSessionTag(transcriptSessionId) : uiSignature.session}',
			'data-ui-target-session={uiSignature.session}',
			'data-ui-composer={uiSignature.composer}',
			'data-ui-messages={!isEmptyState && !mirrorSync.waiting && transcriptMessageCount !== null ? String(transcriptMessageCount) : uiSignature.messages}',
			'data-ui-loading={uiSignature.loading}',
			'data-ui-streaming={uiSignature.streaming}'
		]) {
			expect(tag).toContain(attribute);
		}
		// Only hashed or enum-like values reach the page.
		expect(read('layout/ChatView.svelte')).toContain('session: uiSessionTag($activeSession.sessionId)');
	});

	it('marks each component root and every transcript row', () => {
		expect(openingTag(read('chat/ChatInput.svelte'), '<div\n\tclass="chat-input-wrapper"')).toContain('data-ui-mount={UI_MOUNT.chatInput}');
		expect(openingTag(read('layout/NewSessionHero.svelte'), '<section class="ns-hero"')).toContain('data-ui-mount={UI_MOUNT.newSessionHero}');
		expect(openingTag(read('layout/SessionSidePanel.svelte'), '<aside bind:this={element} class="side-panel"')).toContain('data-ui-mount={UI_MOUNT.sidePanel}');
		const messageArea = read('chat/MessageArea.svelte');
		expect(openingTag(messageArea, '<div class="transcript-shell"')).toContain('data-ui-mount={UI_MOUNT.messageArea}');
		const row = messageArea.slice(messageArea.indexOf('class="msg-virtualized"'), messageArea.indexOf('class="msg-virtualized"') + 400);
		expect(row).toContain("data-ui-row={item.kind === 'message' ? item.message.id || undefined : displayItemKey(item)}");
		// Rows inside work logs and system groups: the message id, never a positional key.
		expect(read('chat/BoundedTranscriptRows.svelte')).toContain('data-ui-row={rowId?.(item) || undefined}');
		expect(read('chat/WorkLogGroup.svelte').match(/rowId=\{[^}]*message\.id\}/g)).toHaveLength(3);
		expect(read('chat/SystemMessageGroup.svelte')).toContain('rowId={message => message.id}');
	});
});
