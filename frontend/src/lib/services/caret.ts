/**
 * The caret in an editable element as a plain-text offset, so it can be put
 * back after focus left (a picker's search field took it) or the editor's DOM
 * was rebuilt with the same text.
 */
export function caretOffset(root: HTMLElement): number | null {
	const selection = root.ownerDocument.getSelection();
	if (!selection?.rangeCount) return null;
	const range = selection.getRangeAt(0);
	if (!root.contains(range.endContainer)) return null;
	const before = root.ownerDocument.createRange();
	before.selectNodeContents(root);
	before.setEnd(range.endContainer, range.endOffset);
	return before.toString().length;
}

/** Put a collapsed caret at a `caretOffset` value; at the end when unknown or past the text. */
export function placeCaret(root: HTMLElement, offset: number | null) {
	const doc = root.ownerDocument;
	const selection = doc.getSelection();
	if (!selection) return;
	const range = doc.createRange();
	range.selectNodeContents(root);
	range.collapse(false);
	if (offset !== null) {
		const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
		let left = offset;
		for (let node = walker.nextNode(); node; node = walker.nextNode()) {
			const length = node.textContent?.length ?? 0;
			if (left <= length) {
				range.setStart(node, left);
				range.collapse(true);
				break;
			}
			left -= length;
		}
	}
	selection.removeAllRanges();
	selection.addRange(range);
}
