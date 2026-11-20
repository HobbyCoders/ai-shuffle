import { cleanup, render } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import ProviderTile from './ProviderTile.svelte';

afterEach(() => cleanup());

describe('ProviderTile', () => {
	it.each(['claude', 'openrouter', 'kimi', 'custom'] as const)('puts the %s mark in the one list tile at 16px', (glyph) => {
		const { container } = render(ProviderTile, { provider: { glyph }, bare: true });
		const tile = container.querySelector('.pt');
		expect(tile).toBeTruthy();
		expect(tile?.querySelector('svg')?.getAttribute('width')).toBe('16');
	});
	it.each(['minimax', 'google', 'antigravity'] as const)(
		'puts the 16px monochrome %s SVG mark directly in one list tile without a second box or monogram',
		(glyph) => {
			const { container } = render(ProviderTile, { provider: { glyph } });
			expect(container.querySelectorAll('.pt')).toHaveLength(1);
			const logo = container.querySelector(`.pt > svg[data-provider-logo="${glyph}"]`);
			expect(logo).toBeTruthy();
			expect(logo?.getAttribute('fill')).toBe('currentColor');
			expect(logo?.getAttribute('width')).toBe('16');
			expect(logo?.getAttribute('height')).toBe('16');
			expect(logo?.querySelector('path[d]')).toBeTruthy();
			expect(container.querySelector('.pt img, .pt rect, .pt text')).toBeNull();
		}
	);
	it('keeps the status dot on the tile corner', () => {
		const { container } = render(ProviderTile, { provider: { glyph: 'openai' }, status: 'warn' });
		expect(container.querySelector('.pt .corner .st-dot.warn')).toBeTruthy();
	});
});
