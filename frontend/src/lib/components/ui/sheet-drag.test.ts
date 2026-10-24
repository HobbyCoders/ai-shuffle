import { afterEach, describe, expect, it } from 'vitest';
import {
	DISMISS_DISTANCE,
	DRAG_SLOP,
	FLICK_STALE_MS,
	canStartSheetDrag,
	moveSheetDrag,
	sheetDragDismisses,
	startSheetDrag
} from './sheet-drag';

const p = (y: number, time: number, x = 0) => ({ x, y, time });

describe('sheet drag gesture', () => {
	it('waits inside the slop, then drags downward with the finger', () => {
		const drag = startSheetDrag(p(100, 0));
		expect(moveSheetDrag(drag, p(100 + DRAG_SLOP, 10))).toBe('pending');
		expect(moveSheetDrag(drag, p(130, 30))).toBe('drag');
		expect(drag.offset).toBe(30);
		moveSheetDrag(drag, p(90, 60));
		expect(drag.offset).toBe(0);
	});

	it('gives an upward or sideways move to the page', () => {
		const up = startSheetDrag(p(100, 0));
		expect(moveSheetDrag(up, p(80, 20))).toBe('scroll');
		expect(moveSheetDrag(up, p(200, 40))).toBe('scroll');
		const side = startSheetDrag(p(100, 0));
		expect(moveSheetDrag(side, p(112, 20, 30))).toBe('scroll');
	});

	it('never drags from an ineligible start', () => {
		const drag = startSheetDrag(p(100, 0), false);
		expect(moveSheetDrag(drag, p(200, 50))).toBe('scroll');
		expect(sheetDragDismisses(drag, 60)).toBe(false);
	});

	it('closes past the distance, springs back short of it', () => {
		const far = startSheetDrag(p(0, 0));
		moveSheetDrag(far, p(40, 400));
		moveSheetDrag(far, p(DISMISS_DISTANCE + 5, 800));
		expect(sheetDragDismisses(far, 810)).toBe(true);

		const short = startSheetDrag(p(0, 0));
		moveSheetDrag(short, p(30, 400));
		moveSheetDrag(short, p(50, 800));
		expect(sheetDragDismisses(short, 810)).toBe(false);
	});

	it('closes on a fast downward flick from a short pull', () => {
		const flick = startSheetDrag(p(0, 0));
		moveSheetDrag(flick, p(12, 8));
		moveSheetDrag(flick, p(30, 16));
		moveSheetDrag(flick, p(45, 24));
		expect(flick.velocity).toBeGreaterThan(0.5);
		expect(sheetDragDismisses(flick, 30)).toBe(true);
	});

	it('a fast pull held still before letting go is not a flick', () => {
		// A finger held still sends no moves: the last speed is all there is.
		const held = startSheetDrag(p(0, 0));
		moveSheetDrag(held, p(12, 8));
		moveSheetDrag(held, p(28, 16));
		moveSheetDrag(held, p(40, 24));
		expect(held.velocity).toBeGreaterThan(0.5);
		expect(sheetDragDismisses(held, 24 + FLICK_STALE_MS + 1)).toBe(false);
		expect(sheetDragDismisses(held, 1024)).toBe(false);
		// Released straight away, the same pull is a flick.
		expect(sheetDragDismisses(held, 40)).toBe(true);
	});

	it('does not close when the finger stopped or turned back before letting go', () => {
		const slow = startSheetDrag(p(0, 0));
		moveSheetDrag(slow, p(30, 10));
		moveSheetDrag(slow, p(50, 20));
		moveSheetDrag(slow, p(48, 400));
		moveSheetDrag(slow, p(44, 450));
		expect(sheetDragDismisses(slow, 460)).toBe(false);
	});

	it('a tap is never a dismissal', () => {
		const tap = startSheetDrag(p(0, 0));
		moveSheetDrag(tap, p(3, 5));
		expect(tap.mode).toBe('pending');
		expect(sheetDragDismisses(tap, 10)).toBe(false);
	});
});

describe('canStartSheetDrag', () => {
	afterEach(() => {
		document.body.innerHTML = '';
	});

	function sheet() {
		document.body.innerHTML = `
			<div id="sheet"><header id="head"><h2 id="title">Title</h2></header>
				<div id="list"><button id="row">row</button></div>
				<input id="field" /></div>
			<p id="outside">x</p>`;
		return document.getElementById('sheet')!;
	}

	function scrolled(id: string, top: number) {
		Object.defineProperty(document.getElementById(id)!, 'scrollTop', { configurable: true, value: top });
	}

	it('starts from the header and from a list at its top', () => {
		const s = sheet();
		expect(canStartSheetDrag(document.getElementById('title'), s)).toBe(true);
		expect(canStartSheetDrag(document.getElementById('row'), s)).toBe(true);
	});

	it('leaves a list scrolled down to scroll', () => {
		const s = sheet();
		scrolled('list', 40);
		expect(canStartSheetDrag(document.getElementById('row'), s)).toBe(false);
		expect(canStartSheetDrag(document.getElementById('title'), s)).toBe(true);
	});

	it('leaves a scrolled sheet to scroll, and never starts from a field or outside', () => {
		const s = sheet();
		expect(canStartSheetDrag(document.getElementById('field'), s)).toBe(false);
		expect(canStartSheetDrag(document.getElementById('outside'), s)).toBe(false);
		scrolled('sheet', 12);
		expect(canStartSheetDrag(document.getElementById('title'), s)).toBe(false);
	});
});
