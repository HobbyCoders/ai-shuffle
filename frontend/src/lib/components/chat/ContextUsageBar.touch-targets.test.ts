/**
 * Hiding and showing a provider's usage on touch screens: the hide button,
 * the Show button and the closed "Hidden providers" row that leads to it
 * are each at least 44px tall (#1528, #1580). jsdom does no layout, so the
 * rule is read from the component's stylesheet.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, it } from 'vitest';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/ContextUsageBar.svelte'), 'utf8');
const css = source.slice(source.indexOf('<style>'));
const touch = (() => {
	const start = css.indexOf('@media (max-width: 768px), (hover: none), (pointer: coarse) {');
	expect(start).toBeGreaterThan(-1);
	return css.slice(start, css.indexOf('\n\t}', start));
})();

it.each([
	['.provider-hide', /\.provider-hide \{[^}]*min-height: 44px;/],
	['.provider-restore', /\.provider-restore \{[^}]*min-height: 44px;/],
	['the closed Hidden providers row', /\.hidden-providers summary \{[^}]*min-height: 44px;/]
])('%s is 44px tall on touch screens', (_name, rule) => {
	expect(touch).toMatch(rule);
});

it('the Hidden providers row keeps its disclosure marker and neutral colour', () => {
	expect(touch).not.toMatch(/\.hidden-providers summary \{[^}]*display:/);
	expect(css).toMatch(/\.hidden-providers \{[^}]*color: var\(--muted-foreground\);/);
});
