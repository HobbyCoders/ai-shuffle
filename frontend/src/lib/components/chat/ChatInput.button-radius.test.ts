import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// The composer's buttons take their corners from the theme (one shared
// --composer-btn-radius), so a square theme never shows round buttons inside
// a square message box. Pinned at the source: no mount needed to read CSS.
const read = (path: string) => readFileSync(resolve(process.cwd(), path), 'utf8');
const chatInput = read('src/lib/components/chat/ChatInput.svelte');
const callButton = read('src/lib/components/chat/VoiceCallButton.svelte');
const mobileCss = read('../mobile/src/mobile.css');

const style = chatInput.slice(chatInput.indexOf('<style>'));
/** Declarations of the first rule whose selector starts with `selector {`. */
function rule(css: string, selector: string): string {
	const start = css.indexOf(`${selector} {`);
	expect(start, `${selector} rule`).toBeGreaterThan(-1);
	return css.slice(start, css.indexOf('}', start));
}

describe('composer button corners follow the theme radius', () => {
	it('defines one shared radius on the composer box, capped at a full circle', () => {
		const island = rule(style, '\t.chat-island');
		expect(island).toMatch(/--composer-btn-radius:\s*min\(calc\(var\(--radius, 10px\) \* 16 \/ 7\), 16px\);/);
	});

	it('uses the shared radius for every desktop composer button', () => {
		expect(rule(style, '\t.composer-attach')).toContain('border-radius: var(--composer-btn-radius);');
		expect(rule(style, '\t.send-btn')).toContain('border-radius: var(--composer-btn-radius);');
		expect(rule(style, '\t.input-area .send-cluster :global(.stop-btn)')).toContain('border-radius: var(--composer-btn-radius);');
		// Dictate in every state (the recording pill too), so its glow matches.
		expect(style).toMatch(
			/\.chat-island \.input-area \.send-cluster :global\(\.voice-btn-wrapper\),\s*\.chat-island \.input-area \.send-cluster :global\(\.voice-btn-wrapper \.voice-btn\) \{\s*border-radius: var\(--composer-btn-radius\);/
		);
		expect(rule(callButton, '\t.call-circle')).toContain('border-radius: var(--composer-btn-radius, 50%);');
	});

	it('keeps no hard-coded circle on the composer buttons', () => {
		const buttons = style.slice(style.indexOf('/* ===== THE CIRCLES'), style.indexOf('@keyframes fadeIn'));
		expect(buttons).not.toContain('border-radius: 50%');
		expect(rule(callButton, '\t.call-circle')).not.toContain('border-radius: 50%');
	});

	it('scales the same rule to the phone composer and its 40px buttons', () => {
		const MOBILE = "html:root[data-platform='mobile']";
		expect(rule(mobileCss, `${MOBILE} .chat-island`)).toMatch(
			/--composer-btn-radius:\s*min\(calc\(var\(--radius, 10px\) \* 2\.5\), 20px\);/
		);
		const phone = mobileCss.slice(
			mobileCss.indexOf(`${MOBILE} .chat-island {`),
			mobileCss.indexOf(`${MOBILE} .chat-island.wrapped .input-area`)
		);
		expect(phone).not.toContain('border-radius: 50%');
		expect(phone.match(/border-radius: var\(--composer-btn-radius\)/g)).toHaveLength(3);
	});
});
