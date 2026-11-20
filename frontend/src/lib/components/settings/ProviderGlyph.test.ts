import { cleanup, render } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';

import ProviderGlyph, { providerGlyphKind, type ProviderGlyphKind } from './ProviderGlyph.svelte';
import antigravityLogo from './provider-artwork/antigravity-color.svg';
import geminiLogo from './provider-artwork/gemini-color.svg';
import minimaxLogo from './provider-artwork/minimax-color.svg';

afterEach(() => cleanup());

describe('ProviderGlyph', () => {
	it.each([
		['antigravity', antigravityLogo],
		['google', geminiLogo],
		['minimax', minimaxLogo]
	] as const)('renders the bundled %s image without a text initial', (kind, asset) => {
		const { container } = render(ProviderGlyph, { kind, size: 19 });
		const logo = container.querySelector(`img[data-provider-logo="${kind}"]`);
		expect(logo?.getAttribute('src')).toBe(asset);
		expect(logo?.getAttribute('width')).toBe('19');
		expect(logo?.getAttribute('height')).toBe('19');
		expect(logo?.getAttribute('alt')).toBe('');
		expect(container.querySelector('text')).toBeNull();
	});

	it.each([
		['__native__', 'claude'],
		['__claude__', 'claude'],
		['claude', 'claude'],
		['anthropic', 'anthropic'],
		['chatgpt', 'openai'],
		['codex', 'openai'],
		['openai', 'openai'],
		['grok', 'grok'],
		['xai', 'grok'],
		['openrouter', 'openrouter'],
		['kimi', 'kimi'],
		['minimax', 'minimax'],
		['opencode', 'opencode'],
		['google', 'google'],
		['antigravity', 'antigravity'],
		['fal', 'fal'],
		['fal-ai', 'fal'],
		['custom', 'custom'],
		['unknown', 'custom'],
		[null, 'custom'],
		[undefined, 'custom']
	] satisfies [string | null | undefined, ProviderGlyphKind][])('maps %s to the %s glyph', (type, mark) => {
		expect(providerGlyphKind(type)).toBe(mark);
	});

	it.each(['claude', 'anthropic', 'openai', 'grok', 'openrouter', 'kimi', 'opencode', 'fal'] as const)(
		'renders the %s vector mark in the current theme colour',
		(kind) => {
			const { container } = render(ProviderGlyph, { kind });
			const logo = container.querySelector(`svg[data-provider-logo="${kind}"]`);
			expect(logo?.getAttribute('fill')).toBe('currentColor');
			expect(logo?.querySelector('rect, text')).toBeNull();
			const paths = [...(logo?.querySelectorAll('path[d]') ?? [])];
			expect(paths.length).toBeGreaterThan(0);

			// Compare the drawn geometry, not the input kind echoed on the SVG.
			const fallback = render(ProviderGlyph, { kind: 'custom' }).container;
			const cubePaths = [...fallback.querySelectorAll('path[d]')].map((path) => path.getAttribute('d'));
			expect(cubePaths.length).toBeGreaterThan(0);
			for (const path of paths) {
				const drawing = path.getAttribute('d');
				expect(drawing).toBeTruthy();
				expect(cubePaths).not.toContain(drawing);
			}
		}
	);

	it.each(['gateway', 'custom'])('keeps a generic endpoint cube for %s providers', (providerType) => {
		const { container } = render(ProviderGlyph, { kind: providerGlyphKind(providerType) });
		expect(container.querySelector('svg[data-provider-logo="custom"] g[stroke="currentColor"]')).toBeTruthy();
		expect(container.querySelector('img, text')).toBeNull();
	});
});
