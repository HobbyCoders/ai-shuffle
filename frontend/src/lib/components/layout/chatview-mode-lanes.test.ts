/**
 * ChatView's per-lane empty state.
 *
 * Three things have to stay true and are all easy to break by accident:
 *  1. The cleared-session hero wins over the lane heroes. A cleared session
 *     shows its own title in EVERY lane — reordering those branches would make
 *     a cleared chat-lane session read as a brand-new one, the exact regression
 *     #558 fixed for the code lane.
 *  2. All three lanes keep the same mounted composer and its draft.
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

describe('ChatView — lane heroes', () => {
	it('reads the lane off the active session, not a separate switcher store', () => {
		expect(codeOnly).toMatch(/const surfaceMode = \$derived\(\$activeSession\.mode\)/);
	});

	it('mounts a hero per lane', () => {
		expect(codeOnly).toMatch(/<ChatModeHero\s*\/>/);
		expect(codeOnly).toMatch(/<VoiceModeHero\s*\/>/);
		expect(codeOnly).toMatch(/<WorkspaceSelector/);
	});

	it('keeps the cleared-session hero ahead of the lane branches', () => {
		const cleared = codeOnly.indexOf('{#if isClearedSession}');
		const chatLane = codeOnly.indexOf("{:else if surfaceMode === 'chat'}");
		const voiceLane = codeOnly.indexOf("{:else if surfaceMode === 'voice'}");
		expect(cleared).toBeGreaterThan(-1);
		expect(chatLane).toBeGreaterThan(cleared);
		expect(voiceLane).toBeGreaterThan(cleared);
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

describe('ChatView — stable mobile empty layout', () => {
	it('retains the action-applied panel-flow fallback in compiled CSS', () => {
		const { css, warnings } = compile(chatView, { filename: 'ChatView.svelte' });
		expect(css?.code).toMatch(/[^{}]*\[data-panel-flow\][^{}]*\{\s*position: static;/);
		expect(warnings.filter((warning) =>
			warning.code === 'css_unused_selector' && warning.message.includes('data-panel-flow')
		)).toEqual([]);
	});
	it('never compacts or hides the hero based on height or controls', () => {
		expect(codeOnly).not.toMatch(/short-surface|keyboard-surface|mobile-controls-open|controls-panel\.models|logo-mark/);
		expect(codeOnly).toContain('bind:clientHeight={surfaceHeight}');
		expect(codeOnly).toContain('style:--chat-surface-height=');
	});
	it('scrolls the empty surface with a nonshrinking top-aligned invitation', () => {
		expect(codeOnly).toMatch(/\.chat-view\.is-empty\s*\{\s*overflow-y: auto;/);
		expect(codeOnly).toMatch(/\.is-empty \.empty-hero\s*\{[^}]*flex: 0 0 auto;[^}]*justify-content: flex-start;[^}]*padding-top: 1rem;/);
		expect(codeOnly).toMatch(/\.is-empty \.empty-bottom-spacer\s*\{ display: none;/);
	});
	it('keeps a sticky, unclipped composer with a normal-flow overflow fallback', () => {
		expect(codeOnly).toMatch(/\.input-slot\.empty-input-layout\s*\{[^}]*position: sticky;[^}]*bottom: 0;/);
		expect(codeOnly).toMatch(/\.empty-input-layout\.composer-overflow\s*\{ position: static;/);
		expect(codeOnly).toMatch(/\.empty-input-layout:global\(\[data-panel-flow\]\)\s*\{ position: static;/);
		expect(codeOnly).toContain("focused.scrollIntoView({ block: 'nearest' })");
		expect(codeOnly.match(/<ChatInput\s/g)).toHaveLength(1);
		expect(codeOnly).toContain('{onBrowserToggle} {onBrowserPreload} {browserOpen}');
	});
});

describe('VoiceModeHero — the only action on its surface', () => {
	const hero = src('VoiceModeHero.svelte');

	it('delegates to the shared start-a-voice-session service', () => {
		expect(hero).toMatch(/import \{ startNewVoiceSession \} from '\$lib\/services\/startVoiceSession'/);
	});

	it('gates on the voice setting and routes to Settings when it is off', () => {
		expect(hero).toMatch(/isVoiceEnabled\(\)/);
		expect(hero).toMatch(/layout\.openFeaturePage\('settings'\)/);
	});

	it('disables its button while the call is being set up', () => {
		expect(hero).toMatch(/disabled=\{starting\}/);
	});

	it('does not re-report errors the service already toasts', () => {
		expect(hero).not.toMatch(/notify\./);
	});
});

describe('ChatModeHero — projectless by construction', () => {
	const hero = src('ChatModeHero.svelte');

	it('offers no workspace picker', () => {
		expect(stripComments(hero)).not.toMatch(/GroupedSelector|WorkspaceSelector|onSelectProject/);
	});

	it('says what the lane can reach', () => {
		expect(hero.toLowerCase()).toMatch(/web search and fetch/);
	});
});
