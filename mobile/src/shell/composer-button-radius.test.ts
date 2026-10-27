import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// The phone composer's buttons follow the theme's corners like the desktop
// ones (frontend ChatInput.button-radius.test.ts), scaled to 40px buttons.
const mobileCss = readFileSync(resolve(process.cwd(), 'src/mobile.css'), 'utf8');
const MOBILE = "html:root[data-platform='mobile']";

function rule(css: string, selector: string): string {
	const start = css.indexOf(`${selector} {`);
	expect(start, `${selector} rule`).toBeGreaterThan(-1);
	return css.slice(start, css.indexOf('}', start));
}

describe('phone composer button corners follow the theme radius', () => {
	it('scales the shared rule to the phone composer and its 40px buttons', () => {
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
