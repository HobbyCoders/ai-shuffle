/**
 * R6: the highlighter is dynamically imported. These tests pin the contract the
 * (unchanged) `chat-markdown.ts` caller depends on: `highlightToInnerHtml` stays
 * synchronous, returns `null` until the grammar is resident (caller falls back
 * to escaped plain text), and highlights once it is.
 */
import { describe, expect, it, vi } from 'vitest';
import {
	clearHighlightCache,
	highlightToInnerHtml,
	isHighlightReady,
	onHighlighterReady,
	resolveShikiLang,
	warmHighlighter
} from './shiki';

describe('resolveShikiLang', () => {
	it('maps aliases to grammar ids and rejects unknown fences', () => {
		expect(resolveShikiLang('ts')).toBe('typescript');
		expect(resolveShikiLang('  Python ')).toBe('python');
		expect(resolveShikiLang('zsh')).toBe('bash');
		expect(resolveShikiLang('brainfuck')).toBeNull();
		expect(resolveShikiLang(null)).toBeNull();
		expect(resolveShikiLang(undefined)).toBeNull();
	});
});

describe('highlightToInnerHtml', () => {
	it('returns null (→ plain text) before the grammar has loaded, without throwing', () => {
		expect(highlightToInnerHtml('const a = 1', null)).toBeNull();
		expect(isHighlightReady('typescript')).toBe(false);
		expect(highlightToInnerHtml('const a = 1', 'typescript')).toBeNull();
	});

	it('highlights once the grammar is resident, and caches the result', async () => {
		const ready = vi.fn();
		const off = onHighlighterReady(ready);

		warmHighlighter(['typescript']);
		await vi.waitFor(() => expect(isHighlightReady('typescript')).toBe(true), { timeout: 20_000 });

		const html = highlightToInnerHtml('const a = 1', 'typescript');
		expect(html).toBeTruthy();
		expect(html).toContain('<span');
		// Themes are emitted as CSS variables so one string serves both themes.
		expect(html).toContain('--shiki-');
		expect(ready).toHaveBeenCalled();

		// Second call is an LRU cache hit — identical string.
		expect(highlightToInnerHtml('const a = 1', 'typescript')).toBe(html);
		clearHighlightCache();
		expect(highlightToInnerHtml('const a = 1', 'typescript')).toBe(html);

		off();
	}, 30_000);

	it('an unsupported language never loads a grammar', () => {
		expect(highlightToInnerHtml('x', 'cobol')).toBeNull();
		expect(isHighlightReady('cobol')).toBe(false);
	});
});
