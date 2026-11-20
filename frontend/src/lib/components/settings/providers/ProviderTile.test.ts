import { cleanup, render } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import minimaxLogo from '../provider-artwork/minimax-color.svg';
import ProviderTile from './ProviderTile.svelte';

afterEach(() => cleanup());

describe('ProviderTile', () => {
	it.each(['claude', 'openrouter', 'kimi', 'custom'] as const)('puts the %s mark in the one list tile at 16px', (glyph) => {
		const { container } = render(ProviderTile, { provider: { glyph }, bare: true });
		const tile = container.querySelector('.pt');
		expect(tile).toBeTruthy();
		expect(tile?.querySelector('svg')?.getAttribute('width')).toBe('16');
	});
	it('puts the 16px MiniMax logo directly in one list tile without a second box or monogram', () => {
		const { container } = render(ProviderTile, { provider: { glyph: 'minimax' } });
		expect(container.querySelectorAll('.pt')).toHaveLength(1);
		const logo = container.querySelector('.pt > img[data-provider-logo="minimax"]');
		expect(logo?.getAttribute('src')).toBe(minimaxLogo);
		expect(logo?.getAttribute('width')).toBe('16');
		expect(logo?.getAttribute('height')).toBe('16');
		expect(container.querySelector('.pt rect, .pt text')).toBeNull();
	});
	it('keeps the status dot on the tile corner', () => {
		const { container } = render(ProviderTile, { provider: { glyph: 'openai' }, status: 'warn' });
		expect(container.querySelector('.pt .corner .st-dot.warn')).toBeTruthy();
	});
});
