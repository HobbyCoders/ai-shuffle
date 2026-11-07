// A busy phone's timing around ProseMirror's focus handler, made deterministic.
//
// On focus ProseMirror arms a 20ms timer that writes the editor's selection
// back into the page. Chrome runs queued input before timers, so on a loaded
// device that timer can run after a key has moved the caret but before the
// `selectionchange` that tells ProseMirror about the move. prosemirror-view
// before 1.42.9 then put the caret back where it was: in #1241 the five
// ArrowLeft presses of switch-chats/draft-caret came to nothing on a hosted
// runner (caret 10, not 5), and locally one of the five was often undone.
//
// `holdFocusTimer` is a page init script (page.addInitScript) that holds any
// 20ms timer armed during a focus event while key input streams in, and runs
// it at the first ArrowLeft keyup once due (between that key's caret move and
// its selectionchange, the order seen in the failing runs) or once key input
// has been quiet for 40ms. A case that moves the caret right after focusing
// then meets the worst ordering on every run instead of by chance.

/** Page init script; `win` exists for the unit test. */
export function holdFocusTimer(win) {
	win = win || window;
	const native = win.setTimeout.bind(win);
	const now = () => win.performance.now();
	let lastKey = -Infinity;
	let held = null;
	const run = () => {
		const timer = held;
		held = null;
		if (timer) timer.fire();
	};
	for (const type of ['keydown', 'keypress', 'beforeinput', 'input']) {
		win.addEventListener(type, () => { lastKey = now(); }, true);
	}
	win.addEventListener('keyup', (event) => {
		lastKey = now();
		if (event.key === 'ArrowLeft' && held && held.due <= now()) run();
	}, true);
	win.setTimeout = function (fn, delay, ...rest) {
		if (delay !== 20 || win.event?.type !== 'focus' || typeof fn !== 'function') return native(fn, delay, ...rest);
		const timer = { due: now() + 20, fire: () => fn(...rest) };
		held = timer;
		const poll = () => {
			if (held !== timer) return;
			if (now() - lastKey >= 40) run();
			else native(poll, 5);
		};
		return native(poll, 20);
	};
}
