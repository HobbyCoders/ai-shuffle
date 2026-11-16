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
 * box's width and in the circles' width (the mic widening while it
 * listens). The circles keep one size in both layouts so the estimate stays
 * exact. Stop takes no seat while it is not shown, so in a chat, where it can
 * arrive and leave under a draft, the text is measured as if it were there
 * (`getReserve`): Stop coming or going never flips the layout.
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

/**
 * Characters of a single line laid out to see whether it wraps: far more than
 * fit on one line of any composer, so a line that does not fit within them
 * wraps without measuring the rest.
 */
export const MEASURED_CHARS = 2000;

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
	/** Width of circles not shown now that the text must leave room for: Stop's seat. */
	getReserve?: () => number;
	isWrapped: () => boolean;
	setWrapped: (wrapped: boolean) => void;
	/** Lines the text takes at a width. Defaults to a hidden copy of the editor's text. */
	countLines?: (text: string, editor: HTMLElement, width: number) => number;
}

/**
 * Watches the composer and flips the wrapped layout. `measureNow()` settles
 * text/control updates before paint; `observe(island, circles)` coalesces
 * later box-size changes through `schedule()` to one measurement per frame.
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
		// A hidden/unlaid-out box has no line width. Keep the seeded/current
		// layout until attachment or the size observer supplies real geometry.
		if (!editor || !island || !editor.isConnected || editor.clientWidth <= 0 || island.clientWidth <= 0) return;
		const reserve = options.getReserve?.() ?? 0;
		const now = { island: island.clientWidth, circles: (options.getCircles?.()?.offsetWidth ?? 0) + reserve };
		const wrapped = options.isWrapped();
		let width: number;
		if (!wrapped || !base) {
			// Measured directly while the single-line layout shows.
			width = contentWidth(editor) - reserve;
			if (!wrapped) base = { text: width, ...now };
		} else {
			width = singleLineTextWidth(base, now);
		}
		const text = options.getText();
		const wrap = needsWrap(text, measuredLines(text, editor, width));
		if (wrap !== wrapped) options.setWrapped(wrap);
	}

	/**
	 * Lines at the single-line width, laid out only when they decide the
	 * layout: empty text and text with a line break are settled without a
	 * measurement (needsWrap), and a long line is measured by its start. The
	 * hidden copy re-lays out everything it holds, so copying a whole 200 KB
	 * paste on every change cost a long task per keystroke (#912).
	 */
	function measuredLines(text: string, editor: HTMLElement, width: number): number {
		if (!text.trim() || text.includes('\n')) return 1;
		const count = options.countLines ?? lineCount;
		const line = text.trimEnd();
		const head = line.slice(0, MEASURED_CHARS);
		const lines = count(head, editor, width);
		// A start that already wraps settles it; one that fits (zero-width
		// characters, a very wide box) falls back to the whole line.
		return lines > 1 || head.length === line.length ? lines : count(line, editor, width);
	}

	function schedule() {
		if (typeof window === 'undefined' || frame) return;
		frame = requestAnimationFrame(check);
	}

	return {
		/** Settle the current render without a narrow-text paint before the next rAF. */
		measureNow() {
			if (frame) cancelAnimationFrame(frame);
			check();
		},
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

/** The action row left to right: dictate, then the circles after it. */
export type RowButton = 'mic' | ComposerCircle;

/**
 * How long a mic, Talk or Stop circle ignores pointer presses after it moved
 * into its seat (#1632). The row keeps no empty seats (owner decision,
 * 2026-11-14), so when Stop or a held Send leaves, the next button slides
 * into the place a tap was aimed at; a press made that soon after was aimed
 * at the button that left, not at one nobody has seen yet.
 */
export const ARRIVAL_GUARD_MS = 400;

/** How long after a button moves in a double click's later press can still reach it. */
export const DOUBLE_PRESS_MS = 500;

/** Arrivals whose press would start or stop something: dictation, a call, the turn. */
const GUARDED: ReadonlySet<RowButton> = new Set(['mic', 'talk', 'stop']);

/**
 * When each seat of the action row last got another button in place of the one
 * it held (a seat that was empty arms nothing). Seats count from
 * the row's right end, where the circles sit, so a button that slides over
 * takes a new seat. A change the user made themselves (typing turns Talk
 * into Send, a chat switch) is `settled`: they are looking at the row, and
 * nothing arrives unseen, except under the rest of a double click or double
 * tap: its later presses land where the first one was, so one that reaches a
 * button which moved in since (Send became Stop) is never meant for it. Send
 * is never guarded: a stray press on it sends the draft that was typed to be
 * sent.
 */
export function createArrivalGuard(now: () => number = () => performance.now()) {
	let row: RowButton[] | null = null;
	let arrivedAt: number[] = [];
	let movedAt: number[] = [];
	// A seat that was empty arms nothing: no press was aimed at a button there.
	const seatTimes = (next: RowButton[], before: RowButton[] | null, times: number[], at: number) =>
		next.map((button, i) => {
			const seat = next.length - 1 - i;
			const was = before?.[before.length - 1 - seat];
			if (was === undefined) return -Infinity;
			return was === button ? (times[seat] ?? -Infinity) : at;
		}).reverse();
	return {
		/** The row as shown now. */
		update(next: RowButton[], settled: boolean) {
			const at = now();
			movedAt = seatTimes(next, row, movedAt, at);
			arrivedAt = settled ? next.map(() => -Infinity) : seatTimes(next, row, arrivedAt, at);
			row = [...next];
		},
		/**
		 * Whether a pointer press made at `pressedAt` on `button` came too soon
		 * after it arrived. `clicks` is the press's click count (`event.detail`).
		 */
		ignores(button: RowButton, pressedAt: number, clicks = 1): boolean {
			if (!row || !GUARDED.has(button)) return false;
			const index = row.lastIndexOf(button);
			if (index < 0) return false;
			const seat = row.length - 1 - index;
			if (clicks > 1 && pressedAt < (movedAt[seat] ?? -Infinity) + DOUBLE_PRESS_MS) return true;
			return pressedAt < (arrivedAt[seat] ?? -Infinity) + ARRIVAL_GUARD_MS;
		}
	};
}

/** Where and when a pointer press on the action row began. */
export interface RowPress<T> { at: number; target: T | null }

/** Presses older than this are dropped: a click never follows its press that late. */
const PRESS_KEPT_MS = 10_000;

/**
 * The pointer presses on the action row, so a click is judged by when its
 * press began rather than when it was released (#1632). Each pointer keeps
 * only its latest press; a cancelled press (a scroll, the system taking the
 * touch) is forgotten, and a press is used once. A click with no press of
 * its own (keyboard, assistive technology, a script) gets none.
 */
export function createPressTracker<T>() {
	const presses = new Map<number, RowPress<T>>();
	let last: { id: number; press: RowPress<T> } | null = null;
	return {
		down(pointerId: number, at: number, target: T | null) {
			for (const [id, press] of presses) if (at - press.at > PRESS_KEPT_MS) presses.delete(id);
			const press = { at, target };
			presses.set(pointerId, press);
			last = { id: pointerId, press };
		},
		cancel(pointerId: number) {
			presses.delete(pointerId);
			if (last?.id === pointerId) last = null;
		},
		/**
		 * The press behind a click on `target`. `pointerId` is the click's own
		 * (undefined where a click carries none: then only the latest press on
		 * this same target counts). Clicks from no pointer (id -1) get none.
		 */
		take(pointerId: number | undefined, clickAt: number, target: T | null): RowPress<T> | null {
			let id = pointerId;
			if (id === undefined) {
				if (!last || last.press.target !== target) return null;
				id = last.id;
			}
			if (id < 0) return null;
			const press = presses.get(id);
			if (!press || press.at > clickAt || clickAt - press.at > PRESS_KEPT_MS) return null;
			presses.delete(id);
			if (last?.id === id) last = null;
			return press;
		}
	};
}
