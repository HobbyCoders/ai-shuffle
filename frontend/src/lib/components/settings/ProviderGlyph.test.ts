import { cleanup, render } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';

import ProviderGlyph, { providerGlyphKind, type ProviderGlyphKind } from './ProviderGlyph.svelte';

afterEach(() => cleanup());

describe('ProviderGlyph', () => {
	it.each(['kimi', 'minimax', 'opencode'] as const)('renders a monochrome neutral initial for %s', (kind) => {
		const { container } = render(ProviderGlyph, { kind });
		expect(container.querySelector('svg')?.getAttribute('fill')).toBe('currentColor');
		expect(container.querySelector('text')?.textContent).toBe(kind === 'kimi' ? 'K' : kind === 'minimax' ? 'M' : 'O');
	});
	it.each([
		['__native__', 'claude'],
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
		['fal', 'custom'],
		['fal-ai', 'custom'],
		['custom', 'custom']
	] satisfies [string, ProviderGlyphKind][])('maps %s to the %s company mark', (type, mark) => {
		expect(providerGlyphKind(type)).toBe(mark);
	});

	it.each(['claude', 'anthropic', 'openai', 'grok', 'openrouter'] satisfies ProviderGlyphKind[])(
		'renders the %s brand logo',
		(kind) => {
			const { container } = render(ProviderGlyph, { props: { kind } });
			const logo = container.querySelector<SVGElement>(`[data-provider-logo="${kind}"]`);
			expect(logo).toBeTruthy();
			expect(logo?.querySelector('path')?.getAttribute('d')?.length).toBeGreaterThan(50);
		}
	);
});
