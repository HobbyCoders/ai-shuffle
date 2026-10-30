/** Exercise MessageArea's actual compensation statements with the browser's
 * quantized scrollTop contract, rather than a second copy of the arithmetic.
 * Importing the component would pull in the entire live session graph. */
import { afterEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

type Remainder = { scrollTop: number; error: number } | null;
const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');
function statements(start: string, end: string): string {
	const from = source.indexOf(start), to = source.indexOf(end, from);
	if (from < 0 || to <= from) throw new Error(`Missing MessageArea compensation block: ${start}`);
	return source.slice(from, to);
}
const compensate = new Function('container', 'delta', 'anchor', 'previous', 'preserveAnchor', `
	let anchorRounding = previous;
	${statements('if (!preserveAnchor) anchorRounding = null;', 'if (next.start === mountedStart')}
	${statements('const roundingError = anchor && anchorRounding', '// Oversize adjacent bodies')}
	${statements('const previousScrollTop = container.scrollTop;', 'lastKnownScrollTop = container.scrollTop;')}
	return anchorRounding;
`) as (container: HTMLElement, delta: number, anchor: HTMLElement | null, previous: Remainder, preserveAnchor: boolean) => Remainder;
const scroll = new Function('containerRef', 'previous', `
	let anchorRounding = previous;
	${statements('if (anchorRounding && containerRef.scrollTop !== anchorRounding.scrollTop)', 'if (programmaticScrollDepth > 0)')}
	return anchorRounding;
`) as (container: HTMLElement, previous: Remainder) => Remainder;

const fixtures: HTMLElement[] = [];
afterEach(() => { for (const node of fixtures.splice(0)) node.remove(); });
function viewport(quantize = Math.round, initial = 10_000_000) {
	const container = document.createElement('div'), anchor = document.createElement('div');
	container.append(anchor); document.body.append(container); fixtures.push(container);
	let position = initial;
	// jsdom does not have a layout engine. Model only the native quantization
	// and origin clamp; the component's correction/reset code above is real.
	Object.defineProperty(container, 'scrollTop', {
		get: () => position, set: (value: number) => { position = Math.max(0, quantize(value)); }
	});
	return { container, anchor };
}

describe('MessageArea subpixel anchor compensation', () => {
	it.each([['nearest integer', Math.round], ['integer truncation', Math.trunc]] as const)(
		'keeps cumulative drift below 1px over 1000 repricings with %s', (_mode, quantize) => {
			const { container, anchor } = viewport(quantize);
			// Fractional movements measured in the failing 1440x900/4x run.
			const deltas = [-23.359375, -9693.40625, -89.359375];
			let remainder: Remainder = null, drift = 0, maxDrift = 0;
			for (let index = 0; index < 1000; index++) {
				const delta = deltas[index % deltas.length], before = container.scrollTop;
				remainder = compensate(container, delta, anchor, remainder, true);
				drift += delta - (container.scrollTop - before);
				maxDrift = Math.max(maxDrift, Math.abs(drift));
			}
			expect(maxDrift).toBeLessThan(1);
			expect(Math.abs(remainder!.error)).toBeLessThan(1);
			// A negative control proves this fixture catches the old creeping
			// position, not just a series of exactly representable scrolls.
			const legacy = viewport(quantize);
			let uncorrected = 0;
			for (let index = 0; index < 1000; index++) {
				const delta = deltas[index % deltas.length], before = legacy.container.scrollTop;
				compensate(legacy.container, delta, legacy.anchor, null, true);
				uncorrected += delta - (legacy.container.scrollTop - before);
			}
			expect(Math.abs(uncorrected)).toBeGreaterThan(1);
		}
	);
	it('clears the remainder on user scroll, even when the reader returns to the same coordinate', () => {
		const { container, anchor } = viewport();
		let remainder = compensate(container, 0.359375, anchor, null, true);
		const original = container.scrollTop;
		container.scrollTop += 10; remainder = scroll(container, remainder);
		expect(remainder).toBeNull();
		container.scrollTop = original;
		remainder = compensate(container, 0.359375, anchor, remainder, true);
		expect(container.scrollTop).toBe(original);
		expect(remainder?.error).toBe(0.359375);
	});
	it('discards a previous view remainder before the next correction', () => {
		const { container, anchor } = viewport();
		const previous = compensate(container, 0.359375, anchor, null, true);
		const original = container.scrollTop;
		const remainder = compensate(container, 0.359375, anchor, previous, false);
		expect(container.scrollTop).toBe(original);
		expect(remainder?.error).toBe(0.359375);
	});
	it('never carries a real origin clamp or a disjoint navigation boundary', () => {
		const { container, anchor } = viewport(Math.round, 1);
		const previous = compensate(container, 0.359375, anchor, null, true);
		expect(compensate(container, -100, anchor, previous, true)).toBeNull();
		expect(container.scrollTop).toBe(0);
		const remainder = compensate(container, 0.359375, anchor, null, true);
		expect(remainder?.error).toBe(0.359375);
		expect(compensate(container, 0.359375, null, remainder, true)).toBeNull();
	});
});
