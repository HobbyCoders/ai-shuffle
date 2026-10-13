/**
 * Tests for the notes textarea auto-resize / scroll-preservation logic.
 *
 * The bug: typing in the notes editor once the content overflowed the viewport
 * yanked the scroll position to the top, hiding the caret. Cause — the
 * `height='auto'` step of the auto-grow collapses the textarea to its
 * min-height, the scroll container shrinks, and the browser clamps scrollTop
 * upward. These tests pin the contract that the scroll position survives a
 * resize, using a faithful simulation of the browser's scrollTop clamp.
 */

import { describe, expect, it } from 'vitest';
import { resizeToFitContent } from './notes-textarea-resize';

/**
 * A scroll container whose scrollTop the browser clamps into the current scroll
 * range on every assignment. Setting scrollTop above the max snaps it down —
 * this clamp is exactly what produces the "jump to top" when the textarea
 * collapses and the content height temporarily shrinks.
 */
class FakeScroller {
	clientHeight: number;
	contentHeight: number;
	#scrollTop = 0;

	constructor(clientHeight: number, contentHeight: number) {
		this.clientHeight = clientHeight;
		this.contentHeight = contentHeight;
	}

	get maxScrollTop(): number {
		return Math.max(0, this.contentHeight - this.clientHeight);
	}

	get scrollTop(): number {
		return this.#scrollTop;
	}

	set scrollTop(value: number) {
		this.#scrollTop = Math.min(Math.max(0, value), this.maxScrollTop);
	}
}

/**
 * A textarea that, like the real element, does NOT grow with its content:
 * `height: 'auto'` collapses it to its min-height, while `scrollHeight` always
 * reports the full content height (overflow:hidden still measures content).
 * Changing the rendered height updates the enclosing scroller's content height
 * (textarea height + the fixed chrome above it).
 */
class FakeTextarea {
	contentHeight: number;
	minHeight: number;
	fixedOffset: number;
	scroller: FakeScroller;
	#height = 'auto';

	constructor(opts: {
		contentHeight: number;
		minHeight: number;
		fixedOffset: number;
		scroller: FakeScroller;
	}) {
		this.contentHeight = opts.contentHeight;
		this.minHeight = opts.minHeight;
		this.fixedOffset = opts.fixedOffset;
		this.scroller = opts.scroller;
	}

	get scrollHeight(): number {
		return this.contentHeight;
	}

	get style() {
		const self = this;
		return {
			get height(): string {
				return self.#height;
			},
			set height(value: string) {
				self.#height = value;
				const rendered = value === 'auto' ? self.minHeight : parseFloat(value);
				self.scroller.contentHeight = self.fixedOffset + Math.max(rendered, 0);
				// The browser re-clamps the scroll offset into range whenever a
				// layout change alters the scroll height — re-apply to simulate it.
				self.scroller.scrollTop = self.scroller.scrollTop;
			}
		};
	}
}

/** Reproduce the un-fixed resize (no scroll preservation) for contrast. */
function buggyResize(textarea: FakeTextarea): void {
	textarea.style.height = 'auto';
	textarea.style.height = `${textarea.scrollHeight}px`;
}

describe('resizeToFitContent', () => {
	it('preserves the scroll position when content grows past the viewport', () => {
		const scroller = new FakeScroller(400, 800); // viewport 400, content 800
		const textarea = new FakeTextarea({
			contentHeight: 650, // grew from the 600 baked into scroller's 800
			minHeight: 240,
			fixedOffset: 200, // title/tags/tabs above the textarea
			scroller
		});
		scroller.scrollTop = 400; // user scrolled to the bottom

		resizeToFitContent(textarea, scroller);

		// View stays put; height tracks the grown content.
		expect(scroller.scrollTop).toBe(400);
		expect(textarea.style.height).toBe('650px');
	});

	it('documents the bug: the same sequence WITHOUT preservation jumps to the top', () => {
		const scroller = new FakeScroller(400, 800);
		const textarea = new FakeTextarea({
			contentHeight: 650,
			minHeight: 240,
			fixedOffset: 200,
			scroller
		});
		scroller.scrollTop = 400;

		buggyResize(textarea);

		// Collapse clamped scrollTop to (240 + 200 - 400) = 40, and it stayed there.
		expect(scroller.scrollTop).toBe(40);
		expect(scroller.scrollTop).toBeLessThan(400);
	});

	it('sets the height to the measured content height', () => {
		const scroller = new FakeScroller(400, 800);
		const textarea = new FakeTextarea({
			contentHeight: 1234,
			minHeight: 240,
			fixedOffset: 200,
			scroller
		});

		resizeToFitContent(textarea, scroller);

		expect(textarea.style.height).toBe('1234px');
	});

	it('clamps to the new range when content shrinks below the old scroll position', () => {
		const scroller = new FakeScroller(400, 1000); // content 1000 → max 600
		const textarea = new FakeTextarea({
			contentHeight: 300, // content shrank a lot (user deleted text)
			minHeight: 240,
			fixedOffset: 200,
			scroller
		});
		scroller.scrollTop = 600; // was at the bottom of the tall content

		resizeToFitContent(textarea, scroller);

		// New content height 300 + offset 200 = 500; max scrollTop = 100.
		expect(textarea.style.height).toBe('300px');
		expect(scroller.scrollTop).toBe(100);
	});

	it('does nothing to scroll state and still resizes when there is no scroller', () => {
		const scroller = new FakeScroller(400, 800);
		const textarea = new FakeTextarea({
			contentHeight: 720,
			minHeight: 240,
			fixedOffset: 200,
			scroller
		});

		expect(() => resizeToFitContent(textarea, null)).not.toThrow();
		expect(textarea.style.height).toBe('720px');
	});
});
