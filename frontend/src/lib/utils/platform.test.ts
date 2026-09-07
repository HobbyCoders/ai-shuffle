/**
 * Locks in the platform contract: the mobile shell is the Android app's,
 * chosen by user-agent token or explicit override, never by viewport width.
 * Also checks the ES5 inline script in app.html stays in sync with this module.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
	ANDROID_UA_PRODUCT,
	UI_PLATFORM_ATTRIBUTE,
	UI_PLATFORM_STORAGE_KEY,
	detectUiPlatform,
	resolveUiPlatform
} from './platform';

const CHROME_UA =
	'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36';
const PHONE_BROWSER_UA =
	'Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36';
const APP_UA = `${PHONE_BROWSER_UA} ${ANDROID_UA_PRODUCT}/0.8.2`;

describe('detectUiPlatform', () => {
	it('is desktop for any plain browser, including a phone browser', () => {
		expect(detectUiPlatform({ userAgent: CHROME_UA }).platform).toBe('desktop');
		expect(detectUiPlatform({ userAgent: PHONE_BROWSER_UA }).platform).toBe('desktop');
		expect(detectUiPlatform({}).platform).toBe('desktop');
	});

	it('is mobile inside the Android app (user-agent token)', () => {
		expect(detectUiPlatform({ userAgent: APP_UA })).toEqual({ platform: 'mobile' });
	});

	it('does not match the product name without the version slash', () => {
		expect(detectUiPlatform({ userAgent: `${CHROME_UA} ${ANDROID_UA_PRODUCT}` }).platform).toBe(
			'desktop'
		);
	});

	it('honours and persists a ?ui= override over the user agent', () => {
		expect(detectUiPlatform({ userAgent: CHROME_UA, search: '?ui=mobile' })).toEqual({
			platform: 'mobile',
			persist: 'mobile'
		});
		expect(detectUiPlatform({ userAgent: APP_UA, search: 'foo=1&ui=desktop' })).toEqual({
			platform: 'desktop',
			persist: 'desktop'
		});
	});

	it('uses a stored override when the URL has none', () => {
		expect(detectUiPlatform({ userAgent: CHROME_UA, stored: 'mobile' })).toEqual({
			platform: 'mobile'
		});
		expect(detectUiPlatform({ userAgent: APP_UA, stored: 'desktop' })).toEqual({
			platform: 'desktop'
		});
	});

	it('ignores junk overrides', () => {
		expect(detectUiPlatform({ userAgent: CHROME_UA, search: '?ui=tablet' })).toEqual({
			platform: 'desktop'
		});
		expect(detectUiPlatform({ userAgent: APP_UA, stored: 'nope' })).toEqual({
			platform: 'mobile'
		});
	});

	it('?ui=auto clears the stored override and falls back to detection', () => {
		expect(detectUiPlatform({ userAgent: CHROME_UA, search: '?ui=auto', stored: 'mobile' })).toEqual(
			{ platform: 'desktop', persist: null }
		);
	});
});

describe('resolveUiPlatform', () => {
	beforeEach(() => {
		document.documentElement.removeAttribute(UI_PLATFORM_ATTRIBUTE);
		localStorage.clear();
	});

	it('prefers the attribute stamped before paint', () => {
		document.documentElement.setAttribute(UI_PLATFORM_ATTRIBUTE, 'mobile');
		expect(resolveUiPlatform()).toBe('mobile');
	});

	it('detects and stamps the attribute when nothing was stamped', () => {
		expect(resolveUiPlatform()).toBe('desktop');
		expect(document.documentElement.getAttribute(UI_PLATFORM_ATTRIBUTE)).toBe('desktop');
	});

	it('applies a stored override', () => {
		localStorage.setItem(UI_PLATFORM_STORAGE_KEY, 'mobile');
		expect(resolveUiPlatform()).toBe('mobile');
		expect(document.documentElement.getAttribute(UI_PLATFORM_ATTRIBUTE)).toBe('mobile');
	});
});

describe('app.html inline platform script', () => {
	const html = readFileSync(resolve(process.cwd(), 'src/app.html'), 'utf8');

	it('stamps the same attribute, storage key and user-agent token as this module', () => {
		expect(html).toContain(`'${UI_PLATFORM_ATTRIBUTE}'`);
		expect(html).toContain(`'${UI_PLATFORM_STORAGE_KEY}'`);
		expect(html).toContain(`${ANDROID_UA_PRODUCT}\\/`);
	});

	it('runs before the app body so the first paint already has the attribute', () => {
		expect(html.indexOf(UI_PLATFORM_ATTRIBUTE)).toBeLessThan(html.indexOf('%sveltekit.body%'));
	});
});
