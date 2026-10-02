import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const mobileCss = readFileSync(resolve(process.cwd(), 'src/mobile.css'), 'utf8');
const card = readFileSync(resolve(process.cwd(), '../frontend/src/lib/components/layout/SessionCard.svelte'), 'utf8');

describe('shared session swipe styles', () => {
	it('does not override the shared card geometry in the mobile shell', () => {
		expect(mobileCss).not.toMatch(/\.(?:scard|scard-frame|card-actions)\b/);
	});

	it('uses gesture layout state and the complete action width after release', () => {
		expect(card).toContain('class:swipe-layout={swipeLayout}');
		expect(card).toMatch(/\.scard-frame\.swipe-layout\.actions-open \.scard\s*\{\s*transform: translateX\(calc\(-1 \* var\(--reveal\)\)\)/);
		expect(card).toMatch(/\.scard-frame\.swipe-layout \.card-actions\s*\{[^}]*width: var\(--reveal\)/);
		expect(card).toMatch(/\.scard-frame\.swipe-layout \.card-actions > \*\s*\{[^}]*width: var\(--action-w, var\(--reveal\)\)/);
	});
});
