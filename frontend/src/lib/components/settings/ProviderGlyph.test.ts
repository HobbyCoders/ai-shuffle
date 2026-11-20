import { cleanup, render } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';

import ProviderGlyph, { providerGlyphKind, type ProviderGlyphKind } from './ProviderGlyph.svelte';

afterEach(() => cleanup());

const BRAND_GLYPHS = [
	'claude',
	'anthropic',
	'openai',
	'grok',
	'openrouter',
	'kimi',
	'minimax',
	'opencode',
	'google',
	'antigravity',
	'fal'
] as const satisfies readonly Exclude<ProviderGlyphKind, 'custom'>[];

const ALL_GLYPHS = [...BRAND_GLYPHS, 'custom'] as const satisfies readonly ProviderGlyphKind[];

describe('ProviderGlyph', () => {
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

	it.each(BRAND_GLYPHS)(
		'renders the %s vector mark as monochrome currentColor SVG without img fallback',
		(kind) => {
			const { container } = render(ProviderGlyph, { kind, size: 19 });
			expect(container.querySelector('img')).toBeNull();
			const logo = container.querySelector(`svg[data-provider-logo="${kind}"]`);
			expect(logo).toBeTruthy();
			expect(logo?.getAttribute('viewBox')).toBe('0 0 24 24');
			expect(logo?.getAttribute('width')).toBe('19');
			expect(logo?.getAttribute('height')).toBe('19');
			expect(logo?.getAttribute('fill')).toBe('currentColor');
			expect(logo?.getAttribute('fill-rule')).toBe('evenodd');
			expect(logo?.getAttribute('clip-rule')).toBe('evenodd');
			expect(logo?.querySelector('rect, text')).toBeNull();
			expect(logo?.querySelector('[fill^="#"], [stroke^="#"], linearGradient, filter')).toBeNull();
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

	it.each(ALL_GLYPHS)('never renders an img element for %s at any app icon size', (kind) => {
		for (const size of [12, 15, 16, 18, 19, 20]) {
			const { container, unmount } = render(ProviderGlyph, { kind, size });
			const logo = container.querySelector(`svg[data-provider-logo="${kind}"]`);
			expect(logo).toBeTruthy();
			expect(logo?.getAttribute('width')).toBe(String(size));
			expect(logo?.getAttribute('height')).toBe(String(size));
			expect(logo?.getAttribute('fill')).toBe('currentColor');
			expect(container.querySelector('img')).toBeNull();
			unmount();
		}
	});

	it.each(['gateway', 'custom'])('keeps a generic endpoint cube for %s providers', (providerType) => {
		const { container } = render(ProviderGlyph, { kind: providerGlyphKind(providerType) });
		expect(container.querySelector('svg[data-provider-logo="custom"] g[stroke="currentColor"]')).toBeTruthy();
		expect(container.querySelector('img, text')).toBeNull();
	});
});
