/** Controlled geometry, not a browser render or microphone-call test.
 * Execute the production follow edge and evaluate the production CSS offsets
 * against callDockRoom's real active/held/released lifecycle. */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import ts from 'typescript';
import { nextCallRoom, NO_CALL_ROOM } from '../layout/callDockRoom';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');
const css = source.slice(source.indexOf('<style>'));
function rule(selector: string, last = false): string {
	const start = last ? css.lastIndexOf(selector) : css.indexOf(selector);
	if (start < 0) throw new Error(`Missing CSS rule: ${selector}`);
	return css.slice(start, css.indexOf('}', start));
}
function property(block: string, name: string): string {
	const value = block.match(new RegExp(`\\b${name}:\\s*([^;]+);`))?.[1];
	if (!value) throw new Error(`Missing ${name}`);
	return value;
}
function pixels(expression: string, vars: Record<string, number>, safe = 0): number {
	const arithmetic = expression
		.replace(/env\(safe-area-inset-bottom,\s*0px\)/g, String(safe))
		.replace(/var\((--[\w-]+)(?:,\s*([^()]+))?\)/g, (_, key, fallback) => String(vars[key] ?? parseFloat(fallback ?? '0')))
		.replace(/calc\(/g, '(').replace(/px/g, '');
	if (!/^[\d\s.+\-()]+$/.test(arithmetic)) throw new Error(`Unresolved CSS: ${arithmetic}`);
	return new Function(`return (${arithmetic});`)();
}
const queueBottom = property(rule('.tail-controls, .tail-error {'), 'bottom');
const errorBottom = property(rule('\n\t.tail-error {'), 'bottom');
const jumpBottom = property(rule('.jump-latest-layer, .selection-limit-layer {'), 'bottom');
const phoneJumpBottom = property(rule('.jump-latest-layer {', true), 'bottom');
const workingTop = property(rule('.tail-activity {'), 'top');
const viewport = 641;

function call(phone: boolean, active: boolean, safe: number) {
	const open = nextCallRoom(NO_CALL_ROOM, { phone, callActive: true, dockHeight: 120, session: 'chat', streaming: false });
	const room = active ? open : nextCallRoom(open, { phone, callActive: false, dockHeight: 0, session: 'chat', streaming: false });
	return { safe, room: room.room, lift: phone && active ? 120 : 0 };
}
function controlBottom(kind: 'queue' | 'error', state: ReturnType<typeof call>, base = 400, errorOffset = 0) {
	const reserve = base + state.safe + state.room;
	const spacerTop = viewport - reserve;
	return spacerTop - pixels(kind === 'queue' ? queueBottom : errorBottom, {
		'--transcript-tail-reserve': reserve, '--call-dock-lift': state.lift,
		'--call-dock-reserve': state.room, '--error-tail-offset': errorOffset
	}, state.safe);
}

const followSource = source.slice(source.indexOf('function renderedFollowEnd()'), source.indexOf('function readingFloorReserve()'));
const followJs = ts.transpile(followSource, { target: ts.ScriptTarget.ES2022 });
function followEnd(lift: number, bottoms: (number | undefined)[], proseEnd = 500, errorOffset = 0) {
	return new Function('env', `
		const containerRef = { scrollTop: 1000, getBoundingClientRect: () => ({ top: 0 }) };
		const box = bottom => ({ getBoundingClientRect: () => ({ height: 60, bottom }) });
		const tailControlsRef = env.bottoms[0] === undefined ? null : box(env.bottoms[0]);
		const tailErrorRef = env.bottoms[1] === undefined ? null : box(env.bottoms[1]);
		const chatColumnRef = {};
		const getComputedStyle = () => ({ getPropertyValue: key => key === '--call-dock-lift' ? env.lift + 'px' : '' });
		const tailErrorOffsetPx = env.errorOffset, MIN_TRANSCRIPT_TAIL_RESERVE = 24;
		const transcriptContentEnd = () => 1000 + env.proseEnd;
		${followJs}
		return renderedFollowEnd();
	`)({ lift, bottoms, proseEnd, errorOffset }) as number;
}

describe('floating call dock clearance for MessageArea tail surfaces', () => {
	it.each(['queue', 'error'] as const)('keeps %s controls above an active phone dock at maximum scroll', kind => {
		for (const safe of [0, 20]) {
			const state = call(true, true, safe);
			const offset = kind === 'error' ? 90 : 0;
			expect(controlBottom(kind, state, 400, offset)).toBe(viewport - state.lift - 24 - safe - offset);
			expect(controlBottom(kind, state, 24, offset)).toBeLessThanOrEqual(viewport - state.lift);
		}
	});

	it.each(['queue', 'error'] as const)('leaves %s placement identical without a floating dock, including retained room after hangup', kind => {
		for (const state of [call(false, true, 0), call(false, true, 20), call(true, false, 0), call(true, false, 20), { safe: 20, room: 0, lift: 0 }]) {
			const offset = kind === 'error' ? 90 : 0;
			expect(controlBottom(kind, state, 400, offset)).toBe(viewport - 24 - state.safe - offset);
		}
	});

	it.each(['queue', 'error'] as const)('follows growing %s controls against the visible edge above the dock', kind => {
		const offset = kind === 'error' ? 90 : 0;
		const bottom = viewport - 120 - 24 - offset + 40;
		const bottoms = kind === 'queue' ? [bottom] : [undefined, bottom];
		expect(followEnd(120, bottoms, 500, offset) - (1000 + viewport)).toBe(40);
	});

	it('keeps the retained visible error slot and the queue on the same lifted follow edge', () => {
		const queue = viewport - 120 - 24 + 40;
		expect(followEnd(120, [queue, queue - 90], 500, 90)).toBe(1000 + viewport + 40);
	});

	it('keeps no-call follow geometry identical, including the error offset and prose fallback', () => {
		expect(followEnd(0, [600])).toBe(1624);
		expect(followEnd(0, [600, 510], 500, 90)).toBe(1624);
		expect(followEnd(0, [])).toBe(1500);
	});

	it('follows the working line above a phone dock without changing its no-call layout', () => {
		const top = pixels(workingTop, {});
		expect(top).toBe(-6);
		const workingBottomAtMax = viewport - (24 + 120) + top + 30;
		expect(workingBottomAtMax).toBe(viewport - 120);
		// Working's 30px row extends 24px past the prose's zero-height origin.
		expect(followEnd(120, [], viewport - 120 - 24 + 40)).toBe(1000 + viewport + 40);
	});

	it('lifts desktop/default and phone/coarse jump controls only while the dock is active', () => {
		for (const lift of [0, 120]) {
			expect(pixels(jumpBottom, { '--call-dock-lift': lift })).toBe(12 + lift);
			expect(pixels(phoneJumpBottom, { '--call-dock-lift': lift })).toBe(8 + lift);
		}
	});

	it('retains then releases call room through the actual producer lifecycle', () => {
		const held = nextCallRoom(NO_CALL_ROOM, { phone: true, callActive: true, dockHeight: 120, session: 'chat', streaming: false });
		const closed = nextCallRoom(held, { phone: true, callActive: false, dockHeight: 0, session: 'chat', streaming: false });
		expect(closed.room).toBe(120);
		expect(nextCallRoom(closed, { phone: true, callActive: false, dockHeight: 0, session: 'chat', streaming: true }).room).toBe(0);
		expect(nextCallRoom(held, { phone: false, callActive: true, dockHeight: 120, session: 'chat', streaming: false }).room).toBe(0);
	});
});
