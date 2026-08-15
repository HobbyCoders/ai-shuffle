import { describe, it, expect } from 'vitest';
import {
	fitScale,
	letterbox,
	canvasToPage,
	modifiersFromEvent,
	MOD_ALT,
	MOD_CTRL,
	MOD_META,
	MOD_SHIFT,
	type FrameMetadata,
	type CanvasRect,
} from './coords';

const meta = (deviceWidth: number, deviceHeight: number): FrameMetadata => ({
	deviceWidth,
	deviceHeight,
});

const rect = (left: number, top: number, width: number, height: number): CanvasRect => ({
	left,
	top,
	width,
	height,
});

describe('fitScale', () => {
	it('picks the contain scale limited by the wider axis (pillarbox)', () => {
		// 1000x500 frame in a 800x800 canvas → limited by width: 800/1000 = 0.8.
		expect(fitScale(rect(0, 0, 800, 800), meta(1000, 500))).toBe(0.8);
	});

	it('picks the contain scale limited by the taller axis (letterbox)', () => {
		// 500x1000 frame in an 800x800 canvas → limited by height: 800/1000 = 0.8.
		expect(fitScale(rect(0, 0, 800, 800), meta(500, 1000))).toBe(0.8);
	});

	it('returns 0 for a degenerate frame or canvas', () => {
		expect(fitScale(rect(0, 0, 800, 800), meta(0, 500))).toBe(0);
		expect(fitScale(rect(0, 0, 0, 800), meta(1000, 500))).toBe(0);
	});
});

describe('letterbox', () => {
	it('centers a pillarboxed frame with equal top/bottom bars', () => {
		// 1000x500 @ 0.8 → drawn 800x400 in an 800x800 canvas → x:0, y:200.
		const scale = fitScale(rect(0, 0, 800, 800), meta(1000, 500));
		expect(letterbox(rect(0, 0, 800, 800), meta(1000, 500), scale)).toEqual({ x: 0, y: 200 });
	});
});

describe('canvasToPage', () => {
	it('maps an exact-fit canvas 1:1 (no offset, no scale)', () => {
		// Canvas at (0,0) sized exactly to a 1000x800 frame.
		const p = canvasToPage(300, 200, rect(0, 0, 1000, 800), meta(1000, 800));
		expect(p).toEqual({ x: 300, y: 200 });
	});

	it('accounts for the canvas rect offset (position on the page)', () => {
		// Canvas positioned at (100, 50); a client point (400, 250) → local (300, 200).
		const p = canvasToPage(400, 250, rect(100, 50, 1000, 800), meta(1000, 800));
		expect(p).toEqual({ x: 300, y: 200 });
	});

	it('accounts for the fit scale (downscaled frame)', () => {
		// 2000x1600 frame drawn into a 1000x800 canvas → scale 0.5.
		// A canvas-local point (300,200) maps to page (600,400).
		const p = canvasToPage(300, 200, rect(0, 0, 1000, 800), meta(2000, 1600));
		expect(p).toEqual({ x: 600, y: 400 });
	});

	it('accounts for letterbox bars (aspect mismatch)', () => {
		// 1000x500 frame in an 800x800 canvas → scale 0.8, drawn 800x400,
		// vertically centered with 200px bars top/bottom.
		// A click at the drawn image's top-left (canvas 0,200) → page (0,0).
		const topLeft = canvasToPage(0, 200, rect(0, 0, 800, 800), meta(1000, 500));
		expect(topLeft).toEqual({ x: 0, y: 0 });
		// A click at canvas (400, 400) → local (400,200) → page (500, 250).
		const mid = canvasToPage(400, 400, rect(0, 0, 800, 800), meta(1000, 500));
		expect(mid).toEqual({ x: 500, y: 250 });
	});

	it('clamps coordinates that land on the letterbox bars', () => {
		// A click above the drawn image (canvas y=50, inside the 200px top bar)
		// clamps to page y=0 rather than going negative.
		const p = canvasToPage(400, 50, rect(0, 0, 800, 800), meta(1000, 500));
		expect(p).not.toBeNull();
		expect(p!.y).toBe(0);
	});

	it('returns null for a degenerate frame', () => {
		expect(canvasToPage(10, 10, rect(0, 0, 800, 800), meta(0, 0))).toBeNull();
	});
});

describe('modifiersFromEvent', () => {
	const flags = (alt: boolean, ctrl: boolean, meta_: boolean, shift: boolean) => ({
		altKey: alt,
		ctrlKey: ctrl,
		metaKey: meta_,
		shiftKey: shift,
	});

	it('returns 0 when no modifiers are held', () => {
		expect(modifiersFromEvent(flags(false, false, false, false))).toBe(0);
	});

	it('maps each modifier to its CDP bit', () => {
		expect(modifiersFromEvent(flags(true, false, false, false))).toBe(MOD_ALT);
		expect(modifiersFromEvent(flags(false, true, false, false))).toBe(MOD_CTRL);
		expect(modifiersFromEvent(flags(false, false, true, false))).toBe(MOD_META);
		expect(modifiersFromEvent(flags(false, false, false, true))).toBe(MOD_SHIFT);
	});

	it('ORs multiple held modifiers into one bitmask', () => {
		expect(modifiersFromEvent(flags(true, true, false, true))).toBe(
			MOD_ALT | MOD_CTRL | MOD_SHIFT,
		);
		expect(modifiersFromEvent(flags(true, true, true, true))).toBe(
			MOD_ALT | MOD_CTRL | MOD_META | MOD_SHIFT,
		);
		// Sanity: all four = 15.
		expect(modifiersFromEvent(flags(true, true, true, true))).toBe(15);
	});
});
