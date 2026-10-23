/**
 * ChatView's per-lane empty state.
 *
 * Three things have to stay true and are all easy to break by accident:
 *  1. The cleared-session hero wins over the lane heroes: a cleared session
 *     shows its own title in EVERY lane. Reordering would make
 *     a cleared chat-lane session read as a brand-new one, the exact regression
 *     #558 fixed for the code lane.
 *  2. Every lane keeps the same mounted composer and its draft.
 *  3. Only desktop balances the hero and input; mobile scrolls natural content.
 *
 * Mounting ChatView pulls in the whole session/store graph (see
 * chatview-modal-stop-dock.test.ts, which makes the same call), so these pin the
 * source rather than the rendered output.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { compile } from 'svelte/compiler';

// Vitest runs from `frontend/`, and `import.meta.url` is a Vite dev-server URL
// inside a transformed module — so resolve off cwd.
const src = (rel: string) =>
	readFileSync(resolve(process.cwd(), 'src/lib/components/layout', rel), 'utf8');

const chatView = src('ChatView.svelte');

/** Comments legitimately name the lanes and the components; only code counts. */
const stripComments = (source: string) =>
	source
		.replace(/<!--[\s\S]*?-->/g, '')
		.replace(/\/\*[\s\S]*?\*\//g, '')
		.replace(/^\s*\/\/.*$/gm, '');

const codeOnly = stripComments(chatView);

describe('ChatView — the new-session hero (#371 step 4)', () => {
	it('opens a new session on the agent card, with no lane heroes', () => {
		expect(codeOnly).toMatch(/<AgentCard\s*\/>/);
		expect(codeOnly).not.toMatch(/ChatModeHero|surfaceMode/);
	});

	it('puts the optional project under the composer, only on the new-session screen', () => {
		expect(codeOnly).toMatch(/\{#if showAgentCard && !archivedSessionId\}\s*<div class="project-slot-wrap">\s*<ProjectSlot \/>/);
		expect(codeOnly).toContain("const showAgentCard = $derived(showEmptyHero && chatSurface === 'new' && !unavailable)");
		expect(codeOnly).toContain('agentCard={showAgentCard}');
	});

	it('never offers a standalone new voice session', () => {
		// Voice is a call on the current conversation (the card's Talk button).
		expect(codeOnly).not.toMatch(/VoiceModeHero|startNewVoiceSession/);
	});

	it('has no personalized assistant-home hero any more (#371 step 5)', () => {
		expect(codeOnly).not.toMatch(/primaryAssistant|assistantState|home_session_id/);
	});

	it('keeps the cleared-session hero ahead of the agent card', () => {
		const cleared = codeOnly.search(/\{(?:#if|:else if) isClearedSession\}/);
		const card = codeOnly.indexOf('<AgentCard');
		expect(cleared).toBeGreaterThan(-1);
		expect(card).toBeGreaterThan(cleared);
	});
});

describe('ChatView — the composer gate', () => {
	it('keeps the composer for an unbound voice surface', () => {
		expect(codeOnly).toMatch(
			/const showComposer = true/
		);
	});

	it('gates the input slot on it', () => {
		expect(codeOnly).toMatch(/\{#if showComposer\}\s*<div class="input-slot"/);
	});

	it('drops the balancing spacer whenever the composer is absent', () => {
		expect(codeOnly).toMatch(/\{#if showEmptyHero && showComposer\}/);
		expect(codeOnly).toMatch(/class:hero-only=\{!showComposer\}/);
	});

	it('still hands the composer slot to the call dock during a live call', () => {
		expect(codeOnly).toMatch(/<VoiceCallPanel sessionId=\{\$activeSession\.sessionId\} compact \/>/);
	});
});

describe('ChatView — stable empty layout', () => {
	// The mobile empty-surface rules (scrolling hero, sticky composer, the
	// data-panel-flow fallback) are the mobile app's: mobile/src/mobile.css,
	// covered by mobile/src/shell/chatview-mobile-empty-layout.test.ts. The web
	// build carries none of them (frontend/scripts/check-no-mobile-ui.mjs).
	it('carries no mobile-shell CSS', () => {
		const { css } = compile(chatView, { filename: 'ChatView.svelte' });
		expect(css?.code ?? '').not.toContain('data-platform');
	});
	it('never compacts or hides the hero based on height or controls', () => {
		expect(codeOnly).not.toMatch(/short-surface|keyboard-surface|mobile-controls-open|controls-panel\.models|logo-mark/);
		expect(codeOnly).toContain('bind:clientHeight={surfaceHeight}');
		expect(codeOnly).toContain('style:--chat-surface-height=');
	});
	it('keeps the composer wiring the mobile layout relies on', () => {
		expect(codeOnly).toContain("focused.scrollIntoView({ block: 'nearest' })");
		expect(codeOnly.match(/<ChatInput\s/g)).toHaveLength(1);
		expect(codeOnly).toContain('{onBrowserToggle} {onBrowserPreload} {browserOpen}');
	});
});

describe('ChatView — chat.open timing', () => {
	it('finishes the sample from a primitive settled id, not on every activeSession tick', () => {
		// A raw $activeSession read inside the effect re-runs it (cancelling and
		// rescheduling its frames) on each streaming update.
		const effect = codeOnly.match(/\$effect\(\(\) => \{\s*if \(settledChatId\)[^}]*\}\);/);
		expect(effect?.[0]).toContain("finishJourneyAfterPaint('chat.open', settledChatId)");
		expect(effect?.[0]).not.toContain('$activeSession');
		expect(codeOnly).toMatch(/const settledChatId = \$derived\.by\(/);
	});
});
