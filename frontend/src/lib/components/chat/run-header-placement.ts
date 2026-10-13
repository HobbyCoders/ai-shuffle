/**
 * One frame loop for every open live-run sticky header (WorkLogGroup's
 * `stickWithinRun`), shared across groups and scrollers (#1282).
 *
 * Each header used to own a scroll listener and its own frame, and each frame
 * read geometry (the scroller's rect, the header's height, the run end's rect)
 * and then wrote a transform. With several open runs the next header's reads
 * landed after the previous header's write, so one scroll frame could force
 * layout once per header. Here, every scheduled header is placed in one frame
 * with ALL reads first and ALL writes after, the scroller's rect is read once
 * per scroller, unchanged values are not rewritten, and a header whose run is
 * far off-screen (per IntersectionObserver) is not measured on scroll at all.
 */

export interface RunHeader {
	/** The sticky header. */
	node: HTMLElement;
	/** Its scroll container, or null when none was found. */
	scroller: HTMLElement | null;
	/** The run's last row; the header rides up with it once it reaches the line. */
	end: () => HTMLElement | null | undefined;
}

interface Registration extends RunHeader {
	/** False while the header is far outside its scroller's viewport. */
	near: boolean;
}

interface ScrollerState {
	headers: Set<Registration>;
	onScroll: () => void;
	observer: IntersectionObserver | null;
}

/** How far outside the viewport a header still counts as on screen. */
const NEAR_MARGIN = '400px 0px';

const registrations = new Set<Registration>();
const scrollers = new Map<HTMLElement, ScrollerState>();
const pending = new Set<Registration>();
let frame = 0;
let onResize: (() => void) | null = null;

/** Running counts (frames, headers measured, DOM writes) for regression tests. */
export const runHeaderStats = { flushes: 0, measured: 0, writes: 0 };

function schedule(registration: Registration): void {
	pending.add(registration);
	if (!frame) frame = requestAnimationFrame(flush);
}

function flush(): void {
	frame = 0;
	const batch = [...pending];
	pending.clear();
	runHeaderStats.flushes++;
	// Read phase: no style or attribute writes until every header is measured.
	const lines = new Map<HTMLElement, number>();
	const placements: Array<{ node: HTMLElement; transform: string; off: boolean }> = [];
	for (const registration of batch) {
		if (!registrations.has(registration)) continue;
		const { node, scroller } = registration;
		const end = registration.end();
		let line: number | undefined;
		if (scroller) {
			line = lines.get(scroller);
			if (line === undefined) {
				line = scroller.getBoundingClientRect().top;
				lines.set(scroller, line);
			}
		}
		if (!end || line === undefined) {
			placements.push({ node, transform: '', off: false });
			continue;
		}
		runHeaderStats.measured++;
		const height = node.offsetHeight || node.getBoundingClientRect().height || 0;
		const overlap = end.getBoundingClientRect().bottom - (line + height);
		placements.push({
			node,
			transform: overlap < 0 ? `translateY(${Math.max(overlap, -height)}px)` : '',
			off: overlap <= -height
		});
	}
	// Write phase: only what changed.
	for (const { node, transform, off } of placements) {
		if (node.style.transform !== transform) {
			node.style.transform = transform;
			runHeaderStats.writes++;
		}
		if (node.hasAttribute('data-run-header-off') !== off) {
			node.toggleAttribute('data-run-header-off', off);
			runHeaderStats.writes++;
		}
	}
}

function scrollerState(scroller: HTMLElement): ScrollerState {
	let state = scrollers.get(scroller);
	if (state) return state;
	const headers = new Set<Registration>();
	const onScroll = () => {
		for (const registration of headers) if (registration.near) schedule(registration);
	};
	const observer = typeof IntersectionObserver === 'undefined'
		? null
		: new IntersectionObserver((entries) => {
			for (const entry of entries) {
				for (const registration of headers) {
					if (registration.node !== entry.target) continue;
					registration.near = entry.isIntersecting;
					// Coming back into range: place it before it is seen.
					if (registration.near) schedule(registration);
				}
			}
		}, { root: scroller, rootMargin: NEAR_MARGIN });
	scroller.addEventListener('scroll', onScroll, { passive: true });
	state = { headers, onScroll, observer };
	scrollers.set(scroller, state);
	return state;
}

/**
 * Track one open run header until the returned `release` is called. `place`
 * re-places it on the next shared frame (after a click, a resize of its group,
 * or a change of which run it heads).
 */
export function trackRunHeader(header: RunHeader): { place: () => void; release: () => void } {
	const registration: Registration = { ...header, near: true };
	registrations.add(registration);
	if (header.scroller) {
		const state = scrollerState(header.scroller);
		state.headers.add(registration);
		state.observer?.observe(header.node);
	}
	if (!onResize) {
		onResize = () => { for (const r of registrations) schedule(r); };
		window.addEventListener('resize', onResize);
	}
	schedule(registration);
	return {
		place: () => schedule(registration),
		release: () => {
			registrations.delete(registration);
			pending.delete(registration);
			const scroller = header.scroller;
			const state = scroller ? scrollers.get(scroller) : undefined;
			if (scroller && state) {
				state.headers.delete(registration);
				state.observer?.unobserve(header.node);
				if (state.headers.size === 0) {
					scroller.removeEventListener('scroll', state.onScroll);
					state.observer?.disconnect();
					scrollers.delete(scroller);
				}
			}
			if (registrations.size === 0) {
				if (onResize) window.removeEventListener('resize', onResize);
				onResize = null;
				if (frame && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(frame);
				frame = 0;
				pending.clear();
			}
		}
	};
}
