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

	it('describes web, attachments, and explicitly requested private access', () => {
		const content = stripComments(hero).toLowerCase().replace(/\s+/g, ' ');
		expect(content).toContain('search the web');
		expect(content).toContain('attachments');
		expect(content).toContain('ask for temporary access');
		expect(content).toContain('folder or ssh machine through a private card');
		expect(content).toContain('no repository or worktree is attached');
	});
});
