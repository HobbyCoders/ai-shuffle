import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
	reconcileMobileComposerExpanded,
	shouldBlockMobileComposerToolbarPointer,
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
	mobileViewport: true,
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

	it('never collapses on a desktop viewport, even when the chat is compact', () => {
		// `compact` is true for any active (non-hero) chat, including desktop.
		expect(shouldCollapseMobileComposer({ ...idleCompactComposer, mobileViewport: false })).toBe(false);
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

describe('mobile composer toolbar pointer guard', () => {
	it('blocks hits while the composer is collapsed', () => {
		expect(shouldBlockMobileComposerToolbarPointer({
			collapsed: true,
			expandPointerActive: false,
			mobileViewport: true
		})).toBe(true);
	});

	it('blocks hits for the expanding pointer even after the dock is shown', () => {
		expect(shouldBlockMobileComposerToolbarPointer({
			collapsed: false,
			expandPointerActive: true,
			mobileViewport: true
		})).toBe(true);
	});

	it('allows hits once expanded and the expanding pointer has settled', () => {
		expect(shouldBlockMobileComposerToolbarPointer({
			collapsed: false,
			expandPointerActive: false,
			mobileViewport: true
		})).toBe(false);
	});

	it('never blocks toolbar hits outside the mobile composer viewport', () => {
		expect(shouldBlockMobileComposerToolbarPointer({
			collapsed: true,
			expandPointerActive: true,
			mobileViewport: false
		})).toBe(false);
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
		expect(ruleBody('.mobile-collapsed :global(.controls-dock)')).toMatch(/pointer-events:\s*none;/);
		expect(chatInput).toContain('class="send-cluster"');
		expect(chatInput).toMatch(/\.send-cluster\s*\{[^}]*align-self:\s*center;/);
		expect(mobileStyles).not.toMatch(/\.send-cluster\s*\{[^}]*display:\s*none;/);
		expect(mobileStyles).not.toContain('.controls-right');
	});

	it('gates collapse and toolbar-inert on the mobile viewport, not compact-alone', () => {
		expect(chatInput).toMatch(/shouldCollapseMobileComposer\(\{[\s\S]*?mobileViewport/);
		expect(chatInput).toMatch(/shouldBlockMobileComposerToolbarPointer\(\{[\s\S]*?mobileViewport/);
		expect(chatInput).toContain('<svelte:window onresize={syncMobileViewport} />');
	});

	it('keeps the dock inert for the tap that expands the collapsed composer', () => {
		// Expanding on pointerdown reflows the toolbar under the finger. The
		// wrapper class and inert flag cover that click; display:none alone
		// does not, because it lifts before click is dispatched.
		expect(ruleBody('.mobile-toolbar-blocked :global(.controls-dock)')).toMatch(
			/pointer-events:\s*none;/
		);
		expect(chatInput).toContain('class:mobile-toolbar-blocked={toolbarPointerBlocked}');
		expect(chatInput).toContain('inertToolbar={toolbarPointerBlocked}');
		expect(chatInput).toContain('onpointerdown={handleComposerPointerDown}');
		expect(chatInput).toContain('armExpandPointerGuard()');
	});

	it('renders the context chip row only in the code lane', () => {
		// Chat and voice sessions bind no repository, worktree or agent, so the
		// whole chip row goes rather than showing selectors that cannot apply.
		expect(chatInput).toMatch(
			/\{#if composerMode === 'code'\}\s*<div class="context-pill-row">/
		);
	});

	it('no longer carries a send-later button to hide', () => {
		expect(chatInput).not.toContain('send-later-btn');
	});
});

describe('ChatInput attachments per lane', () => {
	it('sends chat-lane uploads to the projectless workspace endpoint', () => {
		// A chat session has no project to put the file under, so its uploads go
		// to one shared workspace subdirectory that the lane's Read is scoped to.
		expect(chatInput).toContain("const CHAT_UPLOADS_DIRNAME = 'chat-uploads'");
		expect(chatInput).toContain("api.uploadFile('/workspace/upload'");
	});

	it('routes the picker and the drop handler through one upload helper', () => {
		// Two call sites, one target decision. Letting them each pick an endpoint
		// is how the button and the drop counterpart drift apart.
		const callSites = chatInput.match(/await uploadComposerFile\(file\)/g) ?? [];
		expect(callSites).toHaveLength(2);
	});

	it('gates attach, the drag overlay and the drop on the same lane flag', () => {
		// Three entry points; miss one and the composer offers a picker whose drop
		// counterpart is dead (or vice versa). Only voice has no upload path.
		expect(chatInput).toMatch(/const attachSupported = \$derived\(composerMode !== 'voice'\)/);
		const gates = chatInput.match(/if \(!attachSupported\) return;/g) ?? [];
		expect(gates).toHaveLength(2);
		expect(chatInput).not.toMatch(/if \(composerMode !== 'code'\) return;/);
	});
});
