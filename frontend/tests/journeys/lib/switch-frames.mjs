// Per-frame probe of a chat switch (#1352, #1142): on every animation frame,
// which chat the surface says it shows (data-ui-session), its message count,
// the chat the sidebar highlights, and which transcript rows are painted. A
// screenshot or a poll can miss a two-frame flash of the previous chat's rows;
// this cannot. Rows the loading notice conceals (visibility: hidden, while the
// left chat is retained under it: TranscriptHydrationBoundary) are not painted
// and do not count.

const CHAT = '[data-ui-state="chat"]';

/** The rows and session tag the page shows right now (top-level rows only). */
export function transcriptNow(page) {
	return page.evaluate((selector) => {
		const chat = document.querySelector(selector);
		const rows = [...document.querySelectorAll('[role="log"] [data-ui-row]')]
			.filter((el) => !el.parentElement?.closest('[data-ui-row]'))
			.map((el) => el.getAttribute('data-ui-row'));
		return { session: chat?.getAttribute('data-ui-session') ?? null, messages: Number(chat?.getAttribute('data-ui-messages') || 0), rows };
	}, CHAT);
}

/**
 * Start sampling on every frame. Returns `stop()`, which resolves the frames
 * [{ t, session, messages, active, rows: string[] }] (t in ms since the start). Only a
 * frame that differs from the one before is kept, with the time it was painted.
 */
export async function watchSwitchFrames(page) {
	const key = `s${Math.random().toString(36).slice(2)}`;
	await page.evaluate(({ key, selector }) => {
		const watch = (window.__journeyWatch ||= {});
		const state = (watch[key] = { running: true, frames: [] });
		const started = performance.now();
		let last = '';
		const tick = () => {
			if (!state.running) return;
			const chat = document.querySelector(selector);
			const rows = [...document.querySelectorAll('[role="log"] [data-ui-row]')]
				.filter((el) => !el.parentElement?.closest('[data-ui-row]') && (el.checkVisibility?.({ visibilityProperty: true }) ?? true))
				.map((el) => el.getAttribute('data-ui-row'));
			const session = chat?.getAttribute('data-ui-session') ?? null;
			const messages = Number(chat?.getAttribute('data-ui-messages') || 0);
			// The sidebar's highlighted chat (SessionCard marks it .active), when the list is in the page.
			const active = document.querySelector('[role="group"][aria-label$=" session"].active')?.getAttribute('aria-label')?.slice(0, -8) ?? null;
			const sig = `${session}|${messages}|${active}|${rows.join(',')}`;
			if (sig !== last) {
				last = sig;
				state.frames.push({ t: Math.round(performance.now() - started), session, messages, active, rows });
			}
			requestAnimationFrame(tick);
		};
		requestAnimationFrame(tick);
	}, { key, selector: CHAT });
	return () =>
		page.evaluate((key) => {
			const state = window.__journeyWatch?.[key];
			if (!state) return [];
			state.running = false;
			delete window.__journeyWatch[key];
			return state.frames;
		}, key);
}

/**
 * The frames that showed the chat being LEFT after the new one was selected.
 * `left` is what the page showed before the click ({ session, rows }). The new
 * chat counts as selected from the first frame whose session tag is neither the
 * left chat's nor 'none'; from then on any frame carrying the left chat's tag or
 * one of its rows is a flash of the previous transcript.
 */
export function previousChatFrames(frames, left) {
	const leftRows = new Set(left.rows);
	const selected = frames.findIndex((f) => f.session && f.session !== 'none' && f.session !== left.session);
	if (selected < 0) return { selectedAt: null, bad: [] };
	const bad = frames
		.slice(selected)
		.filter((f) => f.session === left.session || f.rows.some((id) => leftRows.has(id)))
		.map((f) => ({ t: f.t, session: f.session, messages: f.messages, leftRows: f.rows.filter((id) => leftRows.has(id)).length }));
	return { selectedAt: frames[selected].t, bad };
}

/**
 * Frames of chat `session` that showed fewer than `atLeast` messages (#1142:
 * a chat switched back to must never show one message short).
 */
export function shortFrames(frames, session, atLeast) {
	return frames.filter((f) => f.session === session && f.messages > 0 && f.messages < atLeast);
}

/**
 * Frames that still showed rows of the chat left while the sidebar already
 * highlighted `title`: the new chat named, the old transcript on screen.
 */
export function oldRowsUnderNewTitle(frames, left, title) {
	const leftRows = new Set(left.rows);
	return frames.filter((f) => f.active === title && f.rows.some((id) => leftRows.has(id)));
}
