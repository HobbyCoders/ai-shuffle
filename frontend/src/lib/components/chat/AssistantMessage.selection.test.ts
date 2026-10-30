import { cleanup, render } from '@testing-library/svelte';
import { tick } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AssistantMessage from './AssistantMessage.svelte';

afterEach(() => { document.getSelection()?.removeAllRanges(); cleanup(); });

function textNodeContaining(root: Element, needle: string): Text {
	const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
	for (let node = walker.nextNode(); node; node = walker.nextNode()) {
		if (node.textContent?.includes(needle)) return node as Text;
	}
	throw new Error(`no text node containing ${needle}`);
}

describe('selection inside a streaming reply (#870)', () => {
	it('keeps the selected text nodes while deltas arrive, then shows the newest text once cleared', async () => {
		const first = 'First paragraph to select from.\n\nSecond paragraph still';
		const view = render(AssistantMessage, { content: first, streaming: true });
		const prose = view.container.querySelector('.abody') as HTMLElement;
		await vi.waitFor(() => expect(prose.textContent).toContain('Second paragraph still'));

		// Select across the stable prefix into the growing tail.
		const anchor = textNodeContaining(prose, 'First paragraph');
		const focus = textNodeContaining(prose, 'Second paragraph');
		const range = document.createRange();
		range.setStart(anchor, 6);
		range.setEnd(focus, 6);
		const selection = document.getSelection()!;
		selection.removeAllRanges();
		selection.addRange(range);
		const selected = selection.toString();
		document.dispatchEvent(new Event('selectionchange'));
		await tick();

		// Append deltas, including a new paragraph boundary that advances the prefix.
		let content = first;
		for (const delta of [' arriving', ' more.\n\nThird', ' paragraph lands.']) {
			content += delta;
			await view.rerender({ content, streaming: true });
		}
		await new Promise((resolve) => setTimeout(resolve, 50));
		await tick();

		expect(anchor.isConnected && focus.isConnected).toBe(true);
		expect(selection.rangeCount).toBe(1);
		expect(selection.anchorNode).toBe(anchor);
		expect(selection.focusNode).toBe(focus);
		expect(selection.toString()).toBe(selected);

		// Clearing the selection releases the hold: the newest text renders.
		selection.removeAllRanges();
		document.dispatchEvent(new Event('selectionchange'));
		await vi.waitFor(() => expect(prose.textContent).toContain('Third paragraph lands.'));
	});

	it('does not hold a reply whose selection is elsewhere', async () => {
		const outside = document.createElement('p');
		outside.textContent = 'Another turn';
		document.body.appendChild(outside);
		const view = render(AssistantMessage, { content: 'Streaming start', streaming: true });
		const prose = view.container.querySelector('.abody') as HTMLElement;
		await vi.waitFor(() => expect(prose.textContent).toContain('Streaming start'));
		const range = document.createRange();
		range.selectNodeContents(outside);
		document.getSelection()!.addRange(range);
		document.dispatchEvent(new Event('selectionchange'));
		await view.rerender({ content: 'Streaming start and more', streaming: true });
		await vi.waitFor(() => expect(prose.textContent).toContain('and more'));
		outside.remove();
	});
});
