import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import AssistantMessage from './AssistantMessage.svelte';
import MessageActions from './MessageActions.svelte';
import DocsContent from '../docs/DocsContent.svelte';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const code = '<div>hello &amp; goodbye</div>\n  const my_value = "🌍";';
const markdown = '```html\n' + code + '\n```';

function mount(kind: string) {
	const view = kind === 'chat'
		? render(AssistantMessage, { content: markdown, streaming: false })
		: render(DocsContent, { content: markdown });
	return { ...view, button: view.container.querySelector('.code-copy-btn') as HTMLButtonElement };
}

describe.each(['chat', 'docs'])('%s code copy', (kind) => {
	it('waits for the write to succeed before showing copied', async () => {
		let complete!: () => void;
		const writeText = vi.fn(() => new Promise<void>((resolve) => { complete = resolve; }));
		vi.stubGlobal('navigator', { clipboard: { writeText } });
		const { button } = mount(kind);
		await fireEvent.click(button.querySelector('svg') ?? button);
		expect(writeText).toHaveBeenCalledWith(code);
		expect(button.classList.contains('is-copied')).toBe(false);
		expect(button.textContent).not.toBe('Copied!');
		complete();
		await waitFor(() => expect(kind === 'chat' ? button.classList.contains('is-copied') : button.textContent === 'Copied!').toBe(true));
	});

	it.each(['missing', 'blocked'])('copies exact code when clipboard API is %s', async (mode) => {
		vi.stubGlobal('navigator', mode === 'missing' ? {} : { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('blocked')) } });
		const exec = vi.fn(() => {
			expect((document.activeElement as HTMLTextAreaElement).value).toBe(code);
			return true;
		});
		Object.defineProperty(document, 'execCommand', { value: exec, configurable: true });
		const { button } = mount(kind);
		await fireEvent.click(button);
		expect(exec).toHaveBeenCalledWith('copy');
		await waitFor(() => expect(kind === 'chat' ? button.classList.contains('is-copied') : button.textContent === 'Copied!').toBe(true));
	});

	it('shows failure without claiming the code was copied', async () => {
		vi.stubGlobal('navigator', {});
		Object.defineProperty(document, 'execCommand', { value: vi.fn(() => false), configurable: true });
		const { button } = mount(kind);
		await fireEvent.click(button);
		await waitFor(() => expect(button.title).toContain('Could not copy'));
		expect(button.classList.contains('is-copied')).toBe(false);
		expect(button.textContent).not.toBe('Copied!');
	});
});

it('copies a whole chat message through the fallback', async () => {
	vi.stubGlobal('navigator', {});
	const exec = vi.fn(() => {
		expect((document.activeElement as HTMLTextAreaElement).value).toBe('Hello world');
		return true;
	});
	Object.defineProperty(document, 'execCommand', { value: exec, configurable: true });
	const { getByTitle } = render(MessageActions, { messageContent: '**Hello** world', messageRole: 'user' });
	await fireEvent.click(getByTitle('Copy'));
	expect(exec).toHaveBeenCalledWith('copy');
});
