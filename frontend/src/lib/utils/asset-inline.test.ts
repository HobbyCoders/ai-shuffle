import { describe, expect, it } from 'vitest';
import { assetsInlineLimit } from '../../../asset-inline.js';

describe('build asset inlining', () => {
	it('never inlines a font, which the CSP font-src would block as a data: URL', () => {
		const font = '/x/node_modules/@fontsource-variable/jetbrains-mono/files/jetbrains-mono-latin-ext-wght-normal.woff2';
		expect(assetsInlineLimit(font)).toBe(false);
		expect(assetsInlineLimit('/x/a.woff')).toBe(false);
		expect(assetsInlineLimit('/x/a.ttf?v=2')).toBe(false);
	});

	it('leaves every other asset to Vite\'s default limit', () => {
		expect(assetsInlineLimit('/x/icon.svg')).toBeUndefined();
		expect(assetsInlineLimit('/x/logo.png')).toBeUndefined();
	});
});
