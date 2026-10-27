/**
 * The composer's button layout rules (ChatInput).
 *
 * Talk turns into Send: the slot right of the dictate circle shows the Talk
 * circle while there is nothing to send and a call can start here, and the
 * filled Send circle once there is sendable content (text or attachments).
 * While session work runs, Stop follows as the last circle, so Stop stays
 * reachable and a draft can still be sent (it queues): mic · Talk · Stop with
 * no draft, mic · Send · Stop with one. A worktree being created, or a send
 * waiting for the project catalog, shows a spinner in the slot instead.
 *
 * Wrap: while the text fits on one line, attach sits left of it and the
 * circles right of it. Once it needs a second line (or holds a line break),
 * the text takes the full width and attach and the circles move to the two
 * ends of the controls row below. It is measured at the width the text has
 * in the single-line layout, whichever layout is showing, so the composer
 * never flips back and forth at the boundary. While wrapped, that width is
 * estimated from the last single-line measurement, moved by any change in the
 * box's width and in the circles' width (Stop appearing, the mic widening
 * while it listens). The circles keep one size in both layouts so the
 * estimate stays exact.
 */

export type ComposerCircle = 'talk' | 'send' | 'stop' | 'busy';

/** The circles after dictate, left to right. */
export function composerCircles(input: {
	/** A call can start here (VoiceCallButton shows: voice on, no call running here). */
	callAvailable: boolean;
	/** Sendable content: text or attached files. */
	hasContent: boolean;
	/** Session work is live, so Stop must be reachable. */
	working: boolean;
	/** A worktree is being created, or a send waits for the project catalog. */
	busy: boolean;
}): ComposerCircle[] {
	if (input.busy) return ['busy'];
	if (input.hasContent) return input.working ? ['send', 'stop'] : ['send'];
	if (input.callAvailable) return input.working ? ['talk', 'stop'] : ['talk'];
	// Nothing to send and no call: an idle composer shows the resting Send;
	// a working one only needs Stop.
	return input.working ? ['stop'] : ['send'];
}

/**
 * Whether the text needs the wrapped layout, given how many lines it takes
 * at the single-line width. Empty text never wraps (the placeholder is cut
 * to one line).
 */
export function needsWrap(text: string, linesAtSingleWidth: number): boolean {
	if (!text.trim()) return false;
	// A line break after some text puts the caret on a second line.
	if (text.includes('\n')) return true;
	return linesAtSingleWidth > 1;
}

/** Widths taken in the single-line layout. */
export interface SingleLineBase {
	/** The text's own width. */
	text: number;
	/** The composer box's width. */
	island: number;
	/** The circles' width (dictate, Talk or Send, Stop). */
	circles: number;
}

/**
 * The width the text would have in the single-line layout now: the base
 * measurement, wider by any growth of the box and narrower by any growth of
 * the circles beside it.
 */
export function singleLineTextWidth(base: SingleLineBase, now: { island: number; circles: number }): number {
	return base.text + (now.island - base.island) - (now.circles - base.circles);
}

const COPIED_STYLES = [
	'fontFamily',
	'fontSize',
	'fontWeight',
	'fontStyle',
	'letterSpacing',
	'wordSpacing',
	'lineHeight',
	'tabSize',
	'textTransform'
] as const;

export interface WrapWatcherOptions {
	getText: () => string;
	/** The editable element (ProseMirror); its text box is what is measured. */
	getEditor: () => HTMLElement | null;
	/** The composer box; its width changes move the single-line width with it. */
	getIsland: () => HTMLElement | null;
	/** The circles right of the text; their width changes move it the other way. */
	getCircles?: () => HTMLElement | null;
	isWrapped: () => boolean;
	setWrapped: (wrapped: boolean) => void;
	/** Lines the text takes at a width. Defaults to a hidden copy of the editor's text. */
	countLines?: (text: string, editor: HTMLElement, width: number) => number;
}

/**
 * Watches the composer and flips the wrapped layout. `schedule()` after the
 * text or the circles change; `observe(island, circles)` follows width
 * changes. One measurement per frame at most.
 */
export function createWrapWatcher(options: WrapWatcherOptions) {
	let frame = 0;
	let measurer: HTMLDivElement | null = null;
	/** The last single-line measurement; null until the first one. */
	let base: SingleLineBase | null = null;

	function contentWidth(el: HTMLElement): number {
		const css = getComputedStyle(el);
		return el.clientWidth - (parseFloat(css.paddingLeft) || 0) - (parseFloat(css.paddingRight) || 0);
	}

	function lineCount(text: string, editor: HTMLElement, width: number): number {
		if (!measurer) {
			measurer = document.createElement('div');
			measurer.setAttribute('aria-hidden', 'true');
			Object.assign(measurer.style, {
				position: 'fixed',
				left: '-10000px',
				top: '0',
				visibility: 'hidden',
				pointerEvents: 'none',
				whiteSpace: 'pre-wrap',
				overflowWrap: 'break-word',
				wordBreak: 'break-word',
				padding: '0',
				border: '0'
			});
			document.body.appendChild(measurer);
		}
		const css = getComputedStyle(editor);
		for (const key of COPIED_STYLES) measurer.style[key] = css[key];
		measurer.style.width = `${Math.max(1, width)}px`;
		measurer.textContent = text || ' ';
		const lineHeight = parseFloat(css.lineHeight) || parseFloat(css.fontSize) * 1.5 || 20;
		return Math.max(1, Math.round(measurer.scrollHeight / lineHeight));
	}

	function check() {
		frame = 0;
		const editor = options.getEditor();
		const island = options.getIsland();
		if (!editor || !island || !editor.isConnected) return;
		const now = { island: island.clientWidth, circles: options.getCircles?.()?.offsetWidth ?? 0 };
		const wrapped = options.isWrapped();
		let width: number;
		if (!wrapped || !base) {
			// Measured directly while the single-line layout shows.
			width = contentWidth(editor);
			if (!wrapped) base = { text: width, ...now };
		} else {
			width = singleLineTextWidth(base, now);
		}
		const text = options.getText();
		const count = options.countLines ?? lineCount;
		const wrap = needsWrap(text, text.trim() ? count(text.replace(/\s+$/, ''), editor, width) : 1);
		if (wrap !== wrapped) options.setWrapped(wrap);
	}

	function schedule() {
		if (typeof window === 'undefined' || frame) return;
		frame = requestAnimationFrame(check);
	}

	return {
		schedule,
		/** Re-measure when the box or the circles change size. Returns the teardown. */
		observe(island: HTMLElement, circles?: HTMLElement | null): () => void {
			schedule();
			if (typeof ResizeObserver === 'undefined') return () => {};
			const observer = new ResizeObserver(schedule);
			observer.observe(island);
			if (circles) observer.observe(circles);
			return () => observer.disconnect();
		},
		destroy() {
			if (frame) cancelAnimationFrame(frame);
			frame = 0;
			measurer?.remove();
			measurer = null;
		}
	};
}
