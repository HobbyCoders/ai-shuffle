import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
	shouldCollapseMobileComposer,
	type MobileComposerCollapseState
} from './mobile-composer-collapse';

const chatInput = readFileSync(
	resolve(process.cwd(), 'src/lib/components/chat/ChatInput.svelte'),
	'utf8'
);
const mobileStyles = chatInput.slice(
	chatInput.indexOf('@media (max-width: 639px)'),
	chatInput.indexOf('@media (prefers-reduced-motion: reduce)')
);

const idleCompactComposer: MobileComposerCollapseState = {
	compact: true,
	hasDraft: false,
	expanded: false,
	composerFocused: false,
	controlsOpen: false,
	commandAutocompleteOpen: false,
	fileAutocompleteOpen: false
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

describe('ChatInput mobile collapsed surface', () => {
	it('uses compact squared corners instead of the full-width pill radius', () => {
		expect(mobileStyles).toMatch(
			/\.mobile-collapsed \.chat-island\s*\{[\s\S]*?border-radius:\s*14px;/
		);
		expect(mobileStyles).not.toMatch(
			/\.mobile-collapsed \.chat-island\s*\{[\s\S]*?border-radius:\s*999px;/
		);
	});

	it('hides secondary controls while preserving the primary send and stop buttons', () => {
		expect(mobileStyles).toContain('.mobile-collapsed :global(.mobile-controls)');
		expect(mobileStyles).toContain(
			'.mobile-collapsed :global(.controls-right > button:not(.send-btn):not(.stop-btn))'
		);
		expect(mobileStyles).toContain(
			'.mobile-collapsed :global(.controls-right > .voice-btn-wrapper)'
		);
		expect(mobileStyles).toContain(
			'.mobile-collapsed :global(.controls-right > .send-later-btn)'
		);
	});
});
