import { afterEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const css = readFileSync(resolve(process.cwd(), 'src/mobile.css'), 'utf8');
// Exercise the actual global selectors: Svelte's scoped component tests do not
// load mobile.css, which previously fixed every .branch-dropdown to the header.
// Strip comments before reading selectors so documentation cannot count as a rule.
const fixedMenuSelectors = [...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)]
	.filter(([, , body]) => /position:\s*fixed\s*;/.test(body))
	.map(([, selector]) => selector.trim());

afterEach(() => {
	document.body.innerHTML = '';
	delete document.documentElement.dataset.platform;
});

describe('mobile menu positioning', () => {
	it('keeps the inline Git editor, Git progress, composer runner and branch menus in flow', () => {
		document.documentElement.dataset.platform = 'mobile';
		document.body.innerHTML = `
			<div class="git-actions"><div class="git-panel">
				<section class="options-dropdown" id="git-editor"></section>
				<div class="git-progress" id="git-progress"></div>
			</div></div>
			<div class="execution-mode-bar sheet-mode">
				<div class="mode-selector"><div class="mode-dropdown branch-dropdown" id="runner"></div></div>
				<div class="branch-selector"><div class="branch-dropdown" id="worktree"></div></div>
			</div>
			<div class="branch-selector sheet-mode"><div class="branch-dropdown" id="local-branch"></div></div>
		`;
		expect(fixedMenuSelectors.length).toBeGreaterThan(0);
		const pinned = (id: string) => fixedMenuSelectors.some(selector => document.getElementById(id)!.matches(selector));
		for (const id of ['git-editor', 'git-progress', 'runner', 'worktree', 'local-branch']) expect(pinned(id)).toBe(false);
	});

	it('does not apply the mobile header positioning to the desktop shell', () => {
		document.documentElement.dataset.platform = 'desktop';
		document.body.innerHTML = '<div class="git-actions"><div class="options-dropdown"></div></div>';
		const menu = document.querySelector('.options-dropdown')!;
		expect(fixedMenuSelectors.some(selector => menu.matches(selector))).toBe(false);
	});
});
