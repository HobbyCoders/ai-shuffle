import { afterEach, describe, expect, it, vi } from 'vitest';
import { openTerminalLink, terminalLinkUrl, terminalSnippet } from './to-chat';

afterEach(() => vi.unstubAllGlobals());

describe('terminalSnippet', () => {
	it('fences the selection as text, trimming padding and blank edge lines', () => {
		expect(terminalSnippet('\n\n$ npm test   \r\n  FAIL  src/a.test.ts  \n\n')).toBe('```text\n$ npm test\n  FAIL  src/a.test.ts\n```');
	});

	it('keeps blank lines inside the output', () => {
		expect(terminalSnippet('a\n\nb')).toBe('```text\na\n\nb\n```');
	});

	it('uses a longer fence than any backtick run inside', () => {
		expect(terminalSnippet('```js\nx\n````')).toBe('`````text\n```js\nx\n````\n`````');
	});

	it('returns nothing for whitespace', () => {
		expect(terminalSnippet('  \n \n')).toBe('');
		expect(terminalSnippet('')).toBe('');
	});
});

describe('terminalLinkUrl', () => {
	it.each(['http://localhost:5173/', 'https://example.com/a?b=c#d', 'http://127.0.0.1:8080'])('opens %s', (url) => {
		expect(terminalLinkUrl(url)).toBe(new URL(url).href);
	});

	it.each([
		'javascript:alert(1)',
		'file:///etc/passwd',
		'vscode://file/x',
		'data:text/html,hi',
		'https://user:pass@example.com/',
		'not a url'
	])('refuses %s', (text) => {
		expect(terminalLinkUrl(text)).toBeNull();
	});

	it('opens a new tab without an opener', () => {
		const open = vi.fn();
		vi.stubGlobal('open', open);
		expect(openTerminalLink('https://example.com')).toBe(true);
		expect(open).toHaveBeenCalledWith('https://example.com/', '_blank', 'noopener,noreferrer');
		expect(openTerminalLink('javascript:alert(1)')).toBe(false);
		expect(open).toHaveBeenCalledTimes(1);
	});
});
