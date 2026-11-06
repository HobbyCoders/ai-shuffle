import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const mobileCss = readFileSync(resolve(process.cwd(), 'src/mobile.css'), 'utf8');
const card = readFileSync(resolve(process.cwd(), '../frontend/src/lib/components/layout/SessionCard.svelte'), 'utf8');

describe('shared session swipe styles', () => {
	it('does not override the shared card geometry in the mobile shell', () => {
		expect(mobileCss).not.toMatch(/\.(?:scard|scard-frame|card-actions)\b/);
	});

	it('keeps vertical scroll native and paints the inbox lanes behind the row', () => {
		expect(card).toMatch(/\.scard-frame\s*\{[^}]*touch-action: pan-y/);
		// Archive is retired. Only a left swipe reveals Settle / Move to Active.
		expect(card).not.toContain('class="lane l archive"');
		expect(card).toContain('<span>Settle</span>');
		expect(card).toContain('<span>Move to Active</span>');
		expect(card).toContain('class="lane r toggle"');
		expect(card).toMatch(/\.lane\.toggle\s*\{[^}]*var\(--primary\)/);
		expect(card).not.toMatch(/\.lane\.archive\s*\{/);
		expect(card).toContain('data-session-card={session.id}');
	});
});
