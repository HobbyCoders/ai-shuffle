import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import DocsContent from './DocsContent.svelte';

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('DocsContent code copy', () => {
	it('keeps native keyboard semantics and handles a keyboard-originated click once', async () => {
		const writeText = vi.fn().mockResolvedValue(undefined);
		Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
		const { getByRole, container } = render(DocsContent, { content: '```js\nconst value = 1;\n```' });
		const button = getByRole('button', { name: 'Copy' }) as HTMLButtonElement;
		expect(button.type).toBe('button');
		expect(button.tabIndex).toBe(0);
		button.focus();
		expect(document.activeElement).toBe(button);
		// jsdom does not synthesize native activation from key events. Dispatch
		// its resulting click; real Enter/Space activation is checked in browser.
		await fireEvent.click(button, { detail: 0 });
		expect(writeText).toHaveBeenCalledExactlyOnceWith('const value = 1;');
		await fireEvent.click(container.querySelector('code')!);
		expect(writeText).toHaveBeenCalledOnce();
	});

	it('copies through a delegated listener because the sanitizer strips inline handlers', async () => {
		const writeText = vi.fn().mockResolvedValue(undefined);
		Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
		const { container } = render(DocsContent, { content: '```js\nconst value = 1;\n```' });
		const button = container.querySelector('.code-copy-btn') as HTMLButtonElement;
		expect(button).not.toBeNull();
		expect(button.hasAttribute('onclick')).toBe(false);
		await fireEvent.click(button);
		expect(writeText).toHaveBeenCalledWith(expect.stringContaining('const value = 1;'));
		expect(button.textContent).toBe('Copied!');
	});
});
