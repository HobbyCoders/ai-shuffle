// #912: a newline-heavy message that arrives as typed input (an IME commit, a
// dictation or automation fill) or as a paste must land in ONE editor
// transaction with the caret scrolled into view. Left to the browser, Chrome
// applied a 1,000-blank-line insertText as ~1,000 paragraph splits and
// `input` events, freezing the tab for 11.7 s at 4x CPU.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { Transaction } from '@tiptap/pm/state';
import TiptapEditor from './TiptapEditor.svelte';
import { LARGE_PASTE_BYTES } from './tiptap/large-paste';

afterEach(cleanup);

const ISSUE_TEXT = 'Linear reminder regression' + '\n'.repeat(1000) + '<system-reminder>\n# Memory\nunfinished';

async function mount(props: Record<string, unknown> = {}) {
	const onTextChange = vi.fn();
	const { component } = render(TiptapEditor, { props: { onTextChange, ...props } });
	await tick();
	const editor = component.getEditor()!;
	const transactions: Transaction[] = [];
	editor.on('transaction', ({ transaction }) => { if (transaction.docChanged) transactions.push(transaction); });
	return { component, editor, dom: editor.view.dom as HTMLElement, onTextChange, transactions };
}

function typedInput(dom: HTMLElement, data: string, init: Partial<InputEventInit> = {}) {
	const event = new InputEvent('beforeinput', { inputType: 'insertText', data, bubbles: true, cancelable: true, ...init });
	dom.dispatchEvent(event);
	return event;
}

function paste(dom: HTMLElement, text: string) {
	const event = new Event('paste', { bubbles: true, cancelable: true }) as ClipboardEvent;
	Object.defineProperty(event, 'clipboardData', { value: { getData: (type: string) => (type === 'text/plain' ? text : '') } });
	dom.dispatchEvent(event);
	return event;
}

describe('multi-line text input in the composer (#912)', () => {
	it('takes a newline-heavy typed insert as one transaction, not one per line', async () => {
		const { component, dom, onTextChange, transactions } = await mount();
		const event = typedInput(dom, ISSUE_TEXT);
		await tick();

		expect(event.defaultPrevented).toBe(true);
		expect(component.getText()).toBe(ISSUE_TEXT);
		expect(transactions).toHaveLength(1);
		expect(transactions[0].scrolledIntoView).toBe(true);
		expect(onTextChange).toHaveBeenCalledTimes(1);
		expect(onTextChange).toHaveBeenLastCalledWith(ISSUE_TEXT);
	});

	it('normalises CRLF line breaks', async () => {
		const { component, dom } = await mount();
		typedInput(dom, 'one\r\ntwo\rthree');
		expect(component.getText()).toBe('one\ntwo\nthree');
	});

	it('replaces the selection (a fill selects everything first)', async () => {
		const { component, editor, dom, transactions } = await mount();
		component.setContent('old draft');
		editor.commands.selectAll();
		transactions.length = 0;
		typedInput(dom, 'new\n\ndraft');
		expect(component.getText()).toBe('new\n\ndraft');
		expect(transactions).toHaveLength(1);
	});

	it('leaves single-line typing and IME composition to the browser', async () => {
		const { dom, transactions } = await mount();
		expect(typedInput(dom, 'a').defaultPrevented).toBe(false);
		expect(typedInput(dom, 'two\nlines', { isComposing: true }).defaultPrevented).toBe(false);
		expect(typedInput(dom, 'two\nlines', { inputType: 'insertCompositionText' }).defaultPrevented).toBe(false);
		expect(transactions).toHaveLength(0);
	});

	it('offers a large multi-line insert as an attachment, like a paste', async () => {
		const onLargePaste = vi.fn(() => true);
		const { component, dom, transactions } = await mount({ onLargePaste });
		const big = 'log line\n'.repeat(Math.ceil(LARGE_PASTE_BYTES / 9));
		expect(typedInput(dom, big).defaultPrevented).toBe(true);
		expect(onLargePaste).toHaveBeenCalledWith(big);
		expect(transactions).toHaveLength(0);
		expect(component.getText()).toBe('');
	});

	it('scrolls the caret into view after a newline-heavy paste', async () => {
		const { component, dom, transactions } = await mount();
		expect(paste(dom, ISSUE_TEXT).defaultPrevented).toBe(true);
		expect(component.getText()).toBe(ISSUE_TEXT);
		expect(transactions).toHaveLength(1);
		expect(transactions[0].scrolledIntoView).toBe(true);
	});
});
