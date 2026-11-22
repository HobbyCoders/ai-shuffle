import { readFileSync } from 'node:fs';
import { beforeEach, describe, expect, it } from 'vitest';

// The preview script runs inside the inspected page, so the worker keeps it as
// text (app/core/browser_stream.py). These tests run that exact text against a
// DOM: what a preview does to the page, and that the page really goes back.
const source = readFileSync('../app/core/browser_stream.py', 'utf8');

function pythonText(name: string): string {
	const match = source.match(new RegExp(`^${name} = """([\\s\\S]*?)"""`, 'm'));
	if (!match) throw new Error(`${name} is not in browser_stream.py`);
	return match[1];
}

const REGISTRY = pythonText('_DESIGN_REGISTRY_JS');
const TEMPLATE = pythonText('_DESIGN_JS_TEMPLATE');
const REGISTRY_KEY = Symbol.for('ais.design');

type Args = Record<string, unknown>;

function run(args: Args): Record<string, unknown> {
	const script = TEMPLATE.split('__REGISTRY__')
		.join(REGISTRY)
		.split('__ARGS__')
		.join(JSON.stringify({ owner: 'viewer-a', ...args }));
	return (0, eval)(script);
}

function style(key: string, selector: string, styles: Record<string, string>, extra: Args = {}) {
	return run({ op: 'style', key, selector, scope: 'one', styles, ...extra });
}

const el = (selector: string) => document.querySelector(selector) as HTMLElement;

beforeEach(() => {
	delete (window as unknown as Record<symbol, unknown>)[REGISTRY_KEY];
	// jsdom has no CSS.escape; class names here need no escaping.
	if (!globalThis.CSS?.escape) {
		(globalThis as unknown as { CSS: unknown }).CSS = { ...(globalThis.CSS ?? {}), escape: (s: string) => s };
	}
	document.body.innerHTML = `
		<button id="one" class="btn">One</button>
		<button id="two" class="btn">Two</button>
		<button id="three" class="btn">Three</button>
		<p id="text">Text</p>`;
});

describe('the page-side design preview script', () => {
	it('previews a value and puts the page back', () => {
		const result = style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		expect(result.count).toBe(1);
		expect(el('#one').style.getPropertyValue('color')).toBe('rgb(0, 0, 255)');
		expect(el('#one').style.getPropertyPriority('color')).toBe('important');
		run({ op: 'reset', key: 'k1' });
		// No empty style attribute is left on an element that never had one.
		expect(el('#one').hasAttribute('style')).toBe(false);
	});

	it('reads the style attribute before removing it, which Chromium needs', () => {
		// Chromium writes the style attribute lazily. After a change through
		// el.style, removeAttribute misses it until something has read it, and
		// the next read then shows style="". jsdom is eager, so model the rule.
		const target = el('#one');
		let unread = false;
		for (const name of ['setProperty', 'removeProperty'] as const) {
			const real = target.style[name].bind(target.style) as (...args: string[]) => unknown;
			target.style[name] = ((...args: string[]) => {
				unread = true;
				return real(...args);
			}) as never;
		}
		const text = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(target.style), 'cssText');
		if (text?.set) {
			Object.defineProperty(target.style, 'cssText', {
				configurable: true,
				get: () => text.get?.call(target.style),
				set: (value: string) => {
					unread = true;
					text.set?.call(target.style, value);
				},
			});
		}
		const has = target.hasAttribute.bind(target);
		target.hasAttribute = (name) => {
			unread = false;
			return has(name);
		};
		const remove = target.removeAttribute.bind(target);
		target.removeAttribute = (name) => {
			if (!unread) remove(name);
		};
		style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		run({ op: 'reset', key: 'k1' });
		expect(has('style')).toBe(false);
	});

	it('keeps a style attribute the page wrote itself, even an empty one', () => {
		el('#two').setAttribute('style', '');
		style('k1', '#two', { color: 'rgb(0, 0, 255)' });
		run({ op: 'reset', key: 'k1' });
		expect(el('#two').getAttribute('style')).toBe('');
	});

	it('keeps overlapping changes: one on every match, one on a single element', () => {
		style('k1', '#one', { 'background-color': 'rgb(0, 0, 255)' }, { scope: 'all' });
		style('k2', '#two', { 'font-size': '20px' });
		// The single-element change did not cost the button its shared colour.
		expect(el('#two').style.getPropertyValue('background-color')).toBe('rgb(0, 0, 255)');
		expect(el('#two').style.getPropertyValue('font-size')).toBe('20px');
		// Removing either one leaves the other.
		run({ op: 'reset', key: 'k1' });
		expect(el('#two').style.getPropertyValue('background-color')).toBe('');
		expect(el('#two').style.getPropertyValue('font-size')).toBe('20px');
		expect(el('#one').getAttribute('style') ?? '').toBe('');
		expect(el('#three').getAttribute('style') ?? '').toBe('');
	});

	it('lets the later change win where two set the same property, and falls back when it goes', () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)' }, { scope: 'all' });
		style('k2', '#two', { color: 'rgb(255, 0, 0)' });
		expect(el('#two').style.getPropertyValue('color')).toBe('rgb(255, 0, 0)');
		run({ op: 'reset', key: 'k2' });
		expect(el('#two').style.getPropertyValue('color')).toBe('rgb(0, 0, 255)');
	});

	it('drops a property that is no longer in the change', () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)', 'font-size': '20px' });
		style('k1', '#one', { 'font-size': '22px' });
		expect(el('#one').style.getPropertyValue('color')).toBe('');
		expect(el('#one').style.getPropertyValue('font-size')).toBe('22px');
		// Nothing left to preview: the change is gone and so is its style.
		style('k1', '#one', {});
		expect(el('#one').getAttribute('style') ?? '').toBe('');
	});

	it('narrows a change from every match back to one element', () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)' }, { scope: 'all' });
		expect(el('#three').style.getPropertyValue('color')).toBe('rgb(0, 0, 255)');
		style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		expect(el('#one').style.getPropertyValue('color')).toBe('rgb(0, 0, 255)');
		expect(el('#three').style.getPropertyValue('color')).toBe('');
	});

	it('removes one change while the original page is showing without losing the rest', () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		style('k2', '#text', { 'font-size': '20px' });
		expect(run({ op: 'peek', on: true }).peek).toBe(true);
		expect(el('#one').style.getPropertyValue('color')).toBe('');
		expect(el('#text').style.getPropertyValue('font-size')).toBe('');
		run({ op: 'reset', key: 'k1' });
		expect(run({ op: 'peek', on: false }).peek).toBe(false);
		expect(el('#text').style.getPropertyValue('font-size')).toBe('20px');
		expect(el('#one').style.getPropertyValue('color')).toBe('');
	});

	it('shows the changes again when a new value is tried while the original is showing', () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		run({ op: 'peek', on: true });
		style('k2', '#text', { 'font-size': '20px' });
		expect(el('#one').style.getPropertyValue('color')).toBe('rgb(0, 0, 255)');
		expect(el('#text').style.getPropertyValue('font-size')).toBe('20px');
	});

	it('restores the inline styles the page set itself, with their priority', () => {
		el('#one').setAttribute('style', 'color: rgb(255, 0, 0) !important; padding-left: 4px');
		style('k1', '#one', { color: 'rgb(0, 0, 255)', padding: '20px' });
		expect(el('#one').style.getPropertyValue('padding-left')).toBe('20px');
		run({ op: 'reset', key: 'k1' });
		expect(el('#one').style.getPropertyValue('color')).toBe('rgb(255, 0, 0)');
		expect(el('#one').style.getPropertyPriority('color')).toBe('important');
		// The shorthand preview did not swallow the page's own longhand.
		expect(el('#one').style.getPropertyValue('padding-left')).toBe('4px');
		expect(el('#one').style.getPropertyValue('padding-top')).toBe('');
	});

	it('puts back a shorthand the page wrote with a variable', () => {
		// The longhands of `padding: var(--pad)` read as empty, so they cannot
		// be what is kept: the page's own text is.
		el('#one').setAttribute('style', '--pad: 16px; padding: var(--pad)');
		const before = el('#one').style.cssText;
		style('k1', '#one', { padding: '24px' });
		expect(el('#one').style.getPropertyValue('padding-top')).toBe('24px');
		run({ op: 'reset', key: 'k1' });
		expect(el('#one').style.cssText).toBe(before);
		expect(el('#one').style.getPropertyValue('padding')).toBe('var(--pad)');
	});

	it('keeps an inline value the page set itself while the preview was on', () => {
		el('#one').setAttribute('style', 'font-size: 16px');
		style('k1', '#one', { 'font-size': '24px', color: 'rgb(0, 0, 255)' });
		// The page's own code runs, as a resize handler would.
		el('#one').style.setProperty('font-size', '20px');
		run({ op: 'reset', key: 'k1' });
		expect(el('#one').style.getPropertyValue('font-size')).toBe('20px');
		expect(el('#one').style.getPropertyValue('color')).toBe('');
	});

	it('previews again over a value the page set while the preview was on', () => {
		el('#one').setAttribute('style', 'font-size: 16px');
		style('k1', '#one', { 'font-size': '24px' });
		el('#one').style.setProperty('font-size', '20px');
		style('k1', '#one', { 'font-size': '28px' });
		expect(el('#one').style.getPropertyValue('font-size')).toBe('28px');
		run({ op: 'reset', key: 'k1' });
		expect(el('#one').style.getPropertyValue('font-size')).toBe('20px');
	});

	it('puts a variable shorthand back even when the page changed something else', () => {
		el('#one').setAttribute('style', '--pad: 16px; padding: var(--pad)');
		style('k1', '#one', { padding: '24px' });
		el('#one').style.setProperty('color', 'rgb(255, 0, 0)');
		run({ op: 'reset', key: 'k1' });
		expect(el('#one').style.getPropertyValue('padding')).toBe('var(--pad)');
		expect(el('#one').style.getPropertyValue('color')).toBe('rgb(255, 0, 0)');
	});

	it('always includes the picked element when a change applies to every match', () => {
		document.body.innerHTML = Array.from({ length: 1005 }, (_, i) => `<i id="r${i}" class="row"></i>`).join('');
		const result = style('k1', '#r1004', { color: 'rgb(0, 0, 255)' }, { scope: 'all' });
		expect(result.count).toBe(999);
		expect(el('#r1004').style.getPropertyValue('color')).toBe('rgb(0, 0, 255)');
		expect(el('#r0').style.getPropertyValue('color')).toBe('rgb(0, 0, 255)');
	});

	it("takes one viewer's previews off without touching another viewer's", () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		style('k1', '#one', { 'font-size': '20px' }, { owner: 'viewer-b' });
		// Hiding and removing are per viewer: the same key is a different change.
		run({ op: 'peek', on: true });
		expect(el('#one').style.getPropertyValue('color')).toBe('');
		expect(el('#one').style.getPropertyValue('font-size')).toBe('20px');
		run({ op: 'reset', key: null });
		expect(el('#one').style.getPropertyValue('font-size')).toBe('20px');
		// The worker's own reset (no viewer) clears everything that is left.
		run({ op: 'reset', key: null, owner: null });
		expect(el('#one').getAttribute('style') ?? '').toBe('');
	});

	it('reports a marked element that is no longer on the page', () => {
		expect(style('k1', '#gone', { color: 'rgb(0, 0, 255)' })).toEqual({ key: 'k1', missing: true });
		style('k2', '#one', { color: 'rgb(0, 0, 255)' });
		const { rects } = run({
			op: 'rects',
			items: [
				{ key: 'k2', selector: '#one' },
				{ key: 'k9', selector: '#gone' },
			],
		}) as { rects: Record<string, unknown> };
		expect(rects.k2).toEqual({ x: 0, y: 0, width: 0, height: 0 });
		expect(rects.k9).toBeNull();
	});
});
