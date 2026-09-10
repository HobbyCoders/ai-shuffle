export const COPY_ERROR_MESSAGE = 'Could not copy. Select the text and copy it manually.';

/** Copy on the device displaying the UI, including HTTP/LAN pages and WebViews.
 * Call directly from the click handler so the browser retains user activation.
 */
export async function copyText(text: string): Promise<void> {
	try {
		if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
			await navigator.clipboard.writeText(text);
			return;
		}
	} catch {
		// Embedded browsers can expose the API but reject writes.
	}
	if (typeof document === 'undefined' || !document.body) {
		throw new Error(COPY_ERROR_MESSAGE);
	}

	const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
	const selection = document.getSelection();
	const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
	const inputSelection = active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement
		? { start: active.selectionStart, end: active.selectionEnd, direction: active.selectionDirection }
		: null;
	const textarea = document.createElement('textarea');
	textarea.value = text;
	textarea.readOnly = true;
	textarea.tabIndex = -1;
	// Keep it selectable on iOS without opening a keyboard, zooming, or scrolling.
	textarea.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;padding:0;border:0;opacity:0;font-size:16px;';
	// Stay inside an open modal's focus trap / native dialog's non-inert subtree.
	const host = active?.closest('dialog, [role="dialog"], [role="alertdialog"]') ?? document.body;
	try {
		host.appendChild(textarea);
		textarea.focus({ preventScroll: true });
		textarea.select();
		textarea.setSelectionRange(0, textarea.value.length);
		if (!document.execCommand('copy')) throw new Error(COPY_ERROR_MESSAGE);
	} finally {
		textarea.remove();
		active?.focus({ preventScroll: true });
		if (inputSelection && inputSelection.start !== null && inputSelection.end !== null) {
			(active as HTMLInputElement | HTMLTextAreaElement).setSelectionRange(
				inputSelection.start, inputSelection.end, inputSelection.direction ?? undefined
			);
		}
		if (selection) {
			selection.removeAllRanges();
			for (const range of ranges) selection.addRange(range);
		}
	}
}
