/**
 * The floating modal Stop dock is GONE — for every modal, not just questions.
 *
 * #789 (c6082dd2) docked a second StopButton inside `.modal-overlay` at
 * z-index 2 so the composer's own Stop — trapped under the backdrop by
 * ChatInput's `.input-wrapper` stacking context — stayed clickable while a
 * prompt was open. It read as a stray control punching through the dialog, so
 * the whole workaround is reverted: no floating Stop renders while a permission
 * permission prompt is up. Questions now live above the composer without a
 * backdrop, so the composer's normal Stop remains reachable.
 *
 * Mounting ChatView pulls in the entire session/store graph, and the dock's
 * defining property is that it renders NOTHING — so these tests pin the source
 * itself: the element, its styles, and the import that existed only for it must
 * all be absent from ChatView, while ChatInput's real Stop button stays put.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Vitest runs from the `frontend/` root (its config lives there), and
// `import.meta.url` is a Vite dev-server URL rather than a file: URL inside a
// transformed module — so resolve off cwd.
const src = (rel: string) => readFileSync(resolve(process.cwd(), 'src/lib/components', rel), 'utf8');

const chatView = src('layout/ChatView.svelte');
const chatInput = src('chat/ChatInput.svelte');

// The dock's own comment is allowed to name it; only real markup/CSS counts.
const codeOnly = chatView
	.replace(/<!--[\s\S]*?-->/g, '')
	.replace(/\/\*[\s\S]*?\*\//g, '')
	.replace(/^\s*\/\/.*$/gm, '');

describe('ChatView — floating modal Stop dock removed entirely', () => {
	it('renders no .modal-stop-dock element', () => {
		expect(codeOnly).not.toMatch(/class="modal-stop-dock"/);
	});

	it('carries no .modal-stop-dock styles', () => {
		expect(codeOnly).not.toMatch(/\.modal-stop-dock/);
	});

	it('no longer imports StopButton (it existed only for the dock)', () => {
		expect(codeOnly).not.toMatch(/StopButton/);
	});

	it('keeps no dock gate — neither the permission nor the question case', () => {
		// The `!hasPendingQuestions` gate was the question-only half-fix; with the
		// dock gone there is nothing left to gate.
		expect(codeOnly).not.toMatch(/hasPendingQuestions/);
	});

	it('keeps permissions in the overlay and questions beside the persistent composer', () => {
		expect(codeOnly).toMatch(/<PermissionQueue/);
		expect(codeOnly).toMatch(/<QuestionTray[\s\S]*?<ChatInput/);
		const overlay = codeOnly.slice(codeOnly.indexOf('class="modal-overlay"'));
		expect(overlay).not.toMatch(/<QuestionTray|<UserQuestion/);
		expect(codeOnly).toContain('{#if !showEmptyHero && showPermissionModal}');
	});
});

describe('ChatInput — the composer Stop button is untouched', () => {
	it('still imports and renders StopButton', () => {
		expect(chatInput).toMatch(/import StopButton from '\.\/StopButton\.svelte';/);
		expect(chatInput).toMatch(/<StopButton/);
	});
});
