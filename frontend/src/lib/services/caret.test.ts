/**
 * caretOffset / placeCaret: the message box's caret as a plain-text offset,
 * put back after a picker took focus or the editor rebuilt its paragraphs.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { caretOffset, placeCaret } from './caret';

function box(html: string) {
	const root = document.createElement('div');
	root.innerHTML = html;
	document.body.append(root);
	return root;
}
function select(node: Node, offset: number) {
	const range = document.createRange();
	range.setStart(node, offset);
	range.collapse(true);
	document.getSelection()!.removeAllRanges();
	document.getSelection()!.addRange(range);
}
const at = () => {
	const sel = document.getSelection()!;
	return [sel.anchorNode?.textContent, sel.anchorOffset, sel.isCollapsed];
};

describe('caret', () => {
	afterEach(() => { document.body.innerHTML = ''; document.getSelection()?.removeAllRanges(); });

	it('reads the offset across paragraphs and puts it back in a rebuilt copy', () => {
		const root = box('<p>ab</p><p>cde</p>');
		select(root.querySelectorAll('p')[1].firstChild!, 1);
		expect(caretOffset(root)).toBe(3);
		// The editor re-rendered: same text, new nodes.
		root.innerHTML = '<p>ab</p><p>cde</p>';
		placeCaret(root, 3);
		expect(at()).toEqual(['cde', 1, true]);
	});

	it('has no offset when the selection is outside the box', () => {
		const root = box('<p>ab</p>');
		const other = box('<p>zz</p>');
		select(other.querySelector('p')!.firstChild!, 1);
		expect(caretOffset(root)).toBeNull();
	});

	it('falls back to the end when the offset is unknown or past the text', () => {
		const root = box('<p>ab</p><p>cd</p>');
		placeCaret(root, null);
		expect(caretOffset(root)).toBe(4);
		placeCaret(root, 99);
		expect(caretOffset(root)).toBe(4);
		placeCaret(root, 0);
		expect(at()).toEqual(['ab', 0, true]);
	});
});
