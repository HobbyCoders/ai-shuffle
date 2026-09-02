import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
	reconcileMobileComposerExpanded,
	shouldCollapseMobileComposer,
	type MobileComposerCollapseState,
	type MobileComposerReconcileState
} from './mobile-composer-collapse';

const chatInput = readFileSync(
	resolve(process.cwd(), 'src/lib/components/chat/ChatInput.svelte'),
	'utf8'
);
const mobileStyles = chatInput.slice(
	chatInput.indexOf('@media (max-width: 1080px)'),
	chatInput.indexOf('@media (prefers-reduced-motion: reduce)')
);

/** The declarations of the first `selector { … }` rule inside the mobile block. */
function ruleBody(selector: string): string {
	const start = mobileStyles.indexOf(`${selector} {`);
	if (start === -1) return '';
	const end = mobileStyles.indexOf('}', start);
	return mobileStyles.slice(start, end);
}

const idleCompactComposer: MobileComposerCollapseState = {
	compact: true,
	hasDraft: false,
	expanded: false,
	composerFocused: false,
	controlsOpen: false,
	commandAutocompleteOpen: false,
	fileAutocompleteOpen: false
};

const idleReconcileState: MobileComposerReconcileState = {
	compact: true,
	hasDraft: false,
	composerFocused: false,
	controlsOpen: false,
	commandAutocompleteOpen: false,
	fileAutocompleteOpen: false,
	activeElementOwned: false
};

describe('mobile composer collapse predicate', () => {
	it('collapses only an idle compact composer', () => {
		expect(shouldCollapseMobileComposer(idleCompactComposer)).toBe(true);
		expect(shouldCollapseMobileComposer({ ...idleCompactComposer, compact: false })).toBe(false);
	});

	it('never collapses while draft text remains', () => {
		expect(shouldCollapseMobileComposer({ ...idleCompactComposer, hasDraft: true })).toBe(false);
	});

	it.each([
		['an island interaction has expanded it', { expanded: true }],
		['the editor is focused', { composerFocused: true }],
		['the controls sheet is open', { controlsOpen: true }],
		['command autocomplete is open', { commandAutocompleteOpen: true }],
		['file autocomplete is open', { fileAutocompleteOpen: true }]
	])('stays expanded when %s', (_label, state) => {
		expect(shouldCollapseMobileComposer({ ...idleCompactComposer, ...state })).toBe(false);
	});
});

describe('mobile composer deferred ownership reconciliation', () => {
	it('collapses an empty composer after blur or an outside pointer settles', () => {
		expect(reconcileMobileComposerExpanded(true, idleReconcileState)).toBe(false);
	});

	it('stays expanded when focus moves to an island control', () => {
		expect(reconcileMobileComposerExpanded(true, {
			...idleReconcileState,
			activeElementOwned: true
		})).toBe(true);
	});

	it.each([
		['controls sheet', { controlsOpen: true }],
		['command autocomplete', { commandAutocompleteOpen: true }],
		['file autocomplete', { fileAutocompleteOpen: true }],
		['draft', { hasDraft: true }],
		['editor focus', { composerFocused: true }]
	])('never collapses while %s owns the interaction', (_label, patch) => {
		expect(reconcileMobileComposerExpanded(true, { ...idleReconcileState, ...patch })).toBe(true);
	});

	it('collapses immediately after a successful send', () => {
		expect(reconcileMobileComposerExpanded(true, {
			...idleReconcileState,
			activeElementOwned: true,
			controlsOpen: true
		}, 'successful-send')).toBe(false);
	});
});

describe('ChatInput mobile collapsed surface', () => {
	it('uses compact squared corners instead of the full-width pill radius', () => {
		expect(mobileStyles).toMatch(
			/\.mobile-collapsed \.chat-island\s*\{[\s\S]*?border-radius:\s*14px;/
		);
		expect(mobileStyles).not.toMatch(
			/\.mobile-collapsed \.chat-island\s*\{[\s\S]*?border-radius:\s*999px;/
		);
	});

	it('hides the controls row while keeping the send and stop cluster on the input line', () => {
		// The send / stop cluster lives in the input area (right of the editor
		// row), not in the controls row, so the whole row can go when idle.
		expect(ruleBody('.mobile-collapsed :global(.controls-dock)')).toMatch(/display:\s*none;/);
		expect(chatInput).toContain('class="send-cluster"');
		expect(chatInput).toMatch(/\.send-cluster\s*\{[^}]*align-self:\s*center;/);
		expect(mobileStyles).not.toMatch(/\.send-cluster\s*\{[^}]*display:\s*none;/);
		expect(mobileStyles).not.toContain('.controls-right');
	});

	it('no longer carries a send-later button to hide', () => {
		expect(chatInput).not.toContain('send-later-btn');
	});
});
