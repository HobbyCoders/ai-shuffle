/**
 * ChatView's per-lane empty state.
 *
 * Three things have to stay true and are all easy to break by accident:
 *  1. The cleared-session hero wins over the lane heroes. A cleared session
 *     shows its own title in EVERY lane — reordering those branches would make
 *     a cleared chat-lane session read as a brand-new one, the exact regression
 *     #558 fixed for the code lane.
 *  2. The composer is withheld only for the voice lane's UNBOUND surface. Drop
 *     the `sessionId` half of that condition and an existing voice session loses
 *     its composer (and with it the resume-call button); drop the gate entirely
 *     and the new-voice screen grows a text send with no session behind it.
 *  3. The bottom spacer follows the composer, or the hero stops being centred.
 *
 * Mounting ChatView pulls in the whole session/store graph (see
 * chatview-modal-stop-dock.test.ts, which makes the same call), so these pin the
 * source rather than the rendered output.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

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
	it('withholds the composer only for an UNBOUND voice surface', () => {
		expect(codeOnly).toMatch(
			/const showComposer = \$derived\(!\(surfaceMode === 'voice' && !\$activeSession\.sessionId\)\)/
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
		expect(codeOnly).toMatch(/<VoiceCallPanel sessionId=\{\$activeSession\.sessionId\} \/>/);
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
