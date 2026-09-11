import { afterEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const css = readFileSync(resolve(process.cwd(), 'src/mobile.css'), 'utf8');
// Exercise the actual global selectors: Svelte's scoped component tests do not
// load mobile.css, which previously fixed every .branch-dropdown to the header.
const fixedMenuSelectors = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
	.filter(([, selector, body]) => selector.includes('.branch-dropdown') && /position:\s*fixed\s*;/.test(body))
	.map(([, selector]) => selector.replace(/\/\*[\s\S]*?\*\//g, '').trim());

afterEach(() => {
	document.body.innerHTML = '';
	delete document.documentElement.dataset.platform;
});

describe('mobile menu positioning', () => {
	it('pins only the Git header branch menu, not the composer runner or branch menus', () => {
		document.documentElement.dataset.platform = 'mobile';
		document.body.innerHTML = `
			<div class="git-actions"><div class="branch-dropdown" id="header"></div></div>
			<div class="execution-mode-bar sheet-mode">
				<div class="mode-selector"><div class="mode-dropdown branch-dropdown" id="runner"></div></div>
				<div class="branch-selector"><div class="branch-dropdown" id="worktree"></div></div>
			</div>
			<div class="branch-selector sheet-mode"><div class="branch-dropdown" id="local-branch"></div></div>
		`;
		expect(fixedMenuSelectors.length).toBeGreaterThan(0);
		const pinned = (id: string) => fixedMenuSelectors.some(selector => document.getElementById(id)!.matches(selector));
		expect(pinned('header')).toBe(true);
		for (const id of ['runner', 'worktree', 'local-branch']) expect(pinned(id)).toBe(false);
	});

	it('does not apply the mobile header positioning to the desktop shell', () => {
		document.documentElement.dataset.platform = 'desktop';
		document.body.innerHTML = '<div class="git-actions"><div class="branch-dropdown"></div></div>';
		const menu = document.querySelector('.branch-dropdown')!;
		expect(fixedMenuSelectors.some(selector => menu.matches(selector))).toBe(false);
	});
});
