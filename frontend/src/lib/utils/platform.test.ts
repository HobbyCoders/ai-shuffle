/**
 * Locks in the platform contract: the shell is a build decision. The worker
 * web UI is desktop-only — no user-agent sniffing, no `?ui=` override, no
 * localStorage key — and the mobile shell exists only in the `mobile/` bundle.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { SHELL_ENV_KEY, UI_PLATFORM_ATTRIBUTE, buildShell, resolveUiPlatform } from './platform';

describe('buildShell', () => {
	it('is desktop unless the build flag says mobile', () => {
		expect(buildShell({})).toBe('desktop');
		expect(buildShell(undefined)).toBe('desktop');
		expect(buildShell({ [SHELL_ENV_KEY]: 'desktop' })).toBe('desktop');
		expect(buildShell({ [SHELL_ENV_KEY]: 'tablet' })).toBe('desktop');
		expect(buildShell({ [SHELL_ENV_KEY]: 'mobile' })).toBe('mobile');
	});

	it('is desktop in this (web) build', () => {
		expect(buildShell()).toBe('desktop');
	});
});

describe('resolveUiPlatform', () => {
	beforeEach(() => {
		document.documentElement.removeAttribute(UI_PLATFORM_ATTRIBUTE);
		localStorage.clear();
	});

	it('stamps the build shell on <html>', () => {
		expect(resolveUiPlatform()).toBe('desktop');
		expect(document.documentElement.getAttribute(UI_PLATFORM_ATTRIBUTE)).toBe('desktop');
	});

	it('ignores a stale attribute, the URL and storage', () => {
		document.documentElement.setAttribute(UI_PLATFORM_ATTRIBUTE, 'mobile');
		localStorage.setItem('ai-shuffle-ui-platform', 'mobile');
		history.replaceState(null, '', '/?ui=mobile');
		expect(resolveUiPlatform()).toBe('desktop');
		expect(document.documentElement.getAttribute(UI_PLATFORM_ATTRIBUTE)).toBe('desktop');
		history.replaceState(null, '', '/');
	});
});

describe('app.html', () => {
	const html = readFileSync(resolve(process.cwd(), 'src/app.html'), 'utf8');

	it('stamps the desktop shell statically, before the app body', () => {
		expect(html).toMatch(/<html[^>]*\sdata-platform="desktop"/);
		expect(html.indexOf(UI_PLATFORM_ATTRIBUTE)).toBeLessThan(html.indexOf('%sveltekit.body%'));
	});

	it('carries no user-agent or query-string shell detection', () => {
		expect(html).not.toContain('AIShuffleAndroid');
		expect(html).not.toContain('ai-shuffle-ui-platform');
		expect(html).not.toMatch(/get\(['"]ui['"]\)/);
	});
});
