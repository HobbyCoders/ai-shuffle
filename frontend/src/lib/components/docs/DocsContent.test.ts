import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import DocsContent from './DocsContent.svelte';

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('DocsContent code copy', () => {
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
