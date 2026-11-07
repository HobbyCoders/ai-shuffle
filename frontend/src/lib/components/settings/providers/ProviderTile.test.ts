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
	it('draws a monogram as an uppercase letter without a second box inside the tile', () => {
		const { container } = render(ProviderTile, { provider: { glyph: 'minimax' } });
		expect(container.querySelector('.pt rect')).toBeNull();
		expect(container.querySelector('.pt text')?.textContent).toBe('M');
	});
	it('keeps the status dot on the tile corner', () => {
		const { container } = render(ProviderTile, { provider: { glyph: 'openai' }, status: 'warn' });
		expect(container.querySelector('.pt .corner .st-dot.warn')).toBeTruthy();
	});
});
