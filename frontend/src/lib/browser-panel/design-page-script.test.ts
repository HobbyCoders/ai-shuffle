import { readFileSync } from 'node:fs';
import { beforeEach, describe, expect, it } from 'vitest';

// The preview script runs inside the inspected page, so the worker keeps it as
// text (app/core/browser_stream.py). These tests run that exact text against a
// DOM: what a preview does to the page, and that the page really goes back.
//
// A preview is a rule in a stylesheet the script adds to the page, matched by
// a marker attribute on the element. jsdom has no such stylesheets and does
// not apply them, so the tests keep a small stand-in and read the cascade
// themselves: `shown` is what the preview rules give an element.
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

const MARK = 'data-ais-design';

type RuleModel = { token: string; style: { values: Map<string, string>; setProperty(name: string, value: string, priority: string): void } };

class SheetModel {
	cssRules: RuleModel[] = [];
	replaceSync(text: string) {
		if (text !== '') throw new Error('the script only ever empties its sheet this way');
		this.cssRules = [];
	}
	insertRule(text: string, index: number): number {
		const match = /^\[data-ais-design~="(c\d+)"\]:is\(\*, #a#a#a#a\) \{\}$/.exec(text);
		if (!match) throw new Error(`unexpected rule: ${text}`);
		const values = new Map<string, string>();
		this.cssRules.splice(index, 0, {
			token: match[1],
			style: {
				values,
				setProperty: (name, value, priority) => {
					// A browser drops a value it cannot parse; unbalanced brackets stand in for that here.
					if (value.split('(').length !== value.split(')').length) return;
					values.set(name, priority ? `${value} !${priority}` : value);
				}
			}
		});
		return index;
	}
}

type Adopting = { adoptedStyleSheets: SheetModel[] };
const adopted = () => (document as unknown as Adopting).adoptedStyleSheets;

/** The preview value an element gets for a property: the last matching rule wins. */
function shown(target: Element, prop: string): string {
	const tokens = (target.getAttribute(MARK) ?? '').split(' ').filter(Boolean);
	let value = '';
	for (const sheet of adopted()) {
		for (const rule of sheet.cssRules) {
			if (tokens.includes(rule.token)) value = rule.style.values.get(prop) ?? value;
		}
	}
	return value;
}

/** Everything a preview may have left on the page. */
function leftovers(): string[] {
	const found = Array.from(document.querySelectorAll(`[${MARK}]`)).map((node) => `marker on ${node.id}`);
	if (adopted().length) found.push('stylesheet');
	return found;
}

beforeEach(() => {
	delete (window as unknown as Record<symbol, unknown>)[REGISTRY_KEY];
	// jsdom has no CSS.escape; class names here need no escaping.
	if (!globalThis.CSS?.escape) {
		(globalThis as unknown as { CSS: unknown }).CSS = { ...(globalThis.CSS ?? {}), escape: (s: string) => s };
	}
	(globalThis as unknown as { CSSStyleSheet: unknown }).CSSStyleSheet = SheetModel;
	(document as unknown as Adopting).adoptedStyleSheets = [];
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
		expect(shown(el('#one'), 'color')).toBe('rgb(0, 0, 255) !important');
		expect(shown(el('#two'), 'color')).toBe('');
		run({ op: 'reset', key: 'k1' });
		expect(shown(el('#one'), 'color')).toBe('');
		expect(leftovers()).toEqual([]);
	});

	it("never writes to an element's own style", () => {
		// A shorthand written with a variable next to a longhand cannot be read
		// back out of a browser and written in again, so it is never touched:
		// not by a preview, not by taking it off, however often.
		const own = '--pad: 16px; padding: var(--pad); padding-left: 4px';
		el('#one').setAttribute('style', own);
		for (let round = 0; round < 3; round++) {
			style('k1', '#one', { padding: '24px', color: 'rgb(0, 0, 255)' });
			expect(shown(el('#one'), 'padding')).toBe('24px !important');
			expect(el('#one').getAttribute('style')).toBe(own);
			run({ op: 'reset', key: 'k1' });
			expect(el('#one').getAttribute('style')).toBe(own);
		}
		// An element with no style attribute never gets one.
		style('k2', '#two', { color: 'rgb(0, 0, 255)' });
		expect(el('#two').hasAttribute('style')).toBe(false);
		run({ op: 'reset', key: null });
		expect(el('#two').hasAttribute('style')).toBe(false);
		expect(leftovers()).toEqual([]);
	});

	it('leaves alone what the page does to its own style while a preview is on', () => {
		el('#one').setAttribute('style', 'font-size: 16px');
		style('k1', '#one', { 'font-size': '24px' });
		// The page's own code runs, as a resize handler would.
		el('#one').style.setProperty('font-size', '20px');
		style('k1', '#one', { 'font-size': '28px' });
		expect(shown(el('#one'), 'font-size')).toBe('28px !important');
		run({ op: 'reset', key: 'k1' });
		expect(el('#one').style.getPropertyValue('font-size')).toBe('20px');
		expect(leftovers()).toEqual([]);
	});

	it('keeps overlapping changes: one on every match, one on a single element', () => {
		style('k1', '#one', { 'background-color': 'rgb(0, 0, 255)' }, { scope: 'all' });
		style('k2', '#two', { 'font-size': '20px' });
		// The single-element change did not cost the button its shared colour.
		expect(shown(el('#two'), 'background-color')).toBe('rgb(0, 0, 255) !important');
		expect(shown(el('#two'), 'font-size')).toBe('20px !important');
		// Removing either one leaves the other.
		run({ op: 'reset', key: 'k1' });
		expect(shown(el('#two'), 'background-color')).toBe('');
		expect(shown(el('#two'), 'font-size')).toBe('20px !important');
		expect(el('#one').hasAttribute(MARK)).toBe(false);
		expect(el('#three').hasAttribute(MARK)).toBe(false);
	});

	it('lets the later change win where two set the same property, and falls back when it goes', () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)' }, { scope: 'all' });
		style('k2', '#two', { color: 'rgb(255, 0, 0)' });
		expect(shown(el('#two'), 'color')).toBe('rgb(255, 0, 0) !important');
		run({ op: 'reset', key: 'k2' });
		expect(shown(el('#two'), 'color')).toBe('rgb(0, 0, 255) !important');
	});

	it('drops a property that is no longer in the change', () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)', 'font-size': '20px' });
		style('k1', '#one', { 'font-size': '22px' });
		expect(shown(el('#one'), 'color')).toBe('');
		expect(shown(el('#one'), 'font-size')).toBe('22px !important');
		// Nothing left to preview: the change is gone and so is everything it added.
		style('k1', '#one', {});
		expect(leftovers()).toEqual([]);
	});

	it('narrows a change from every match back to one element', () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)' }, { scope: 'all' });
		expect(shown(el('#three'), 'color')).toBe('rgb(0, 0, 255) !important');
		style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		expect(shown(el('#one'), 'color')).toBe('rgb(0, 0, 255) !important');
		expect(shown(el('#three'), 'color')).toBe('');
		expect(el('#three').hasAttribute(MARK)).toBe(false);
	});

	it('removes one change while the original page is showing without losing the rest', () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		style('k2', '#text', { 'font-size': '20px' });
		expect(run({ op: 'peek', on: true }).peek).toBe(true);
		expect(shown(el('#one'), 'color')).toBe('');
		expect(shown(el('#text'), 'font-size')).toBe('');
		run({ op: 'reset', key: 'k1' });
		expect(run({ op: 'peek', on: false }).peek).toBe(false);
		expect(shown(el('#text'), 'font-size')).toBe('20px !important');
		expect(shown(el('#one'), 'color')).toBe('');
		expect(el('#one').hasAttribute(MARK)).toBe(false);
	});

	it('shows the changes again when a new value is tried while the original is showing', () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		run({ op: 'peek', on: true });
		style('k2', '#text', { 'font-size': '20px' });
		expect(shown(el('#one'), 'color')).toBe('rgb(0, 0, 255) !important');
		expect(shown(el('#text'), 'font-size')).toBe('20px !important');
	});

	it('drops a value the browser does not accept without losing the other changes', () => {
		style('k1', '#one', { color: 'rgb(0, 0', 'font-size': '20px' });
		style('k2', '#text', { color: 'rgb(255, 0, 0)' });
		expect(shown(el('#one'), 'color')).toBe('');
		expect(shown(el('#one'), 'font-size')).toBe('20px !important');
		expect(shown(el('#text'), 'color')).toBe('rgb(255, 0, 0) !important');
	});

	it('clears the marker from a copy the page made of a previewed element', () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		style('k2', '#two', { color: 'rgb(255, 0, 0)' });
		const copy = el('#one').cloneNode(true) as HTMLElement;
		copy.id = 'copy';
		document.body.appendChild(copy);
		run({ op: 'reset', key: 'k1' });
		expect(copy.hasAttribute(MARK)).toBe(false);
		expect(shown(el('#two'), 'color')).toBe('rgb(255, 0, 0) !important');
		run({ op: 'reset', key: null });
		expect(leftovers()).toEqual([]);
	});

	it('keeps its stylesheet last and beside the ones the page adopted itself', () => {
		const pages = new SheetModel();
		(document as unknown as Adopting).adoptedStyleSheets = [pages];
		style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		expect(adopted()).toHaveLength(2);
		expect(adopted()[0]).toBe(pages);
		// The page adopts another sheet, dropping the preview's; the next change puts it back, last.
		const later = new SheetModel();
		(document as unknown as Adopting).adoptedStyleSheets = [pages, later];
		style('k1', '#one', { color: 'rgb(255, 0, 0)' });
		expect(adopted().slice(0, 2)).toEqual([pages, later]);
		expect(shown(el('#one'), 'color')).toBe('rgb(255, 0, 0) !important');
		run({ op: 'reset', key: null });
		expect(adopted()).toEqual([pages, later]);
	});

	it('always includes the picked element when a change applies to every match', () => {
		document.body.innerHTML = Array.from({ length: 1005 }, (_, i) => `<i id="r${i}" class="row"></i>`).join('');
		const result = style('k1', '#r1004', { color: 'rgb(0, 0, 255)' }, { scope: 'all' });
		expect(result.count).toBe(999);
		expect(shown(el('#r1004'), 'color')).toBe('rgb(0, 0, 255) !important');
		expect(shown(el('#r0'), 'color')).toBe('rgb(0, 0, 255) !important');
	});

	it("takes one viewer's previews off without touching another viewer's", () => {
		style('k1', '#one', { color: 'rgb(0, 0, 255)' });
		style('k1', '#one', { 'font-size': '20px' }, { owner: 'viewer-b' });
		// Hiding and removing are per viewer: the same key is a different change.
		run({ op: 'peek', on: true });
		expect(shown(el('#one'), 'color')).toBe('');
		expect(shown(el('#one'), 'font-size')).toBe('20px !important');
		run({ op: 'reset', key: null });
		expect(shown(el('#one'), 'font-size')).toBe('20px !important');
		// The worker's own reset (no viewer) clears everything that is left.
		run({ op: 'reset', key: null, owner: null });
		expect(leftovers()).toEqual([]);
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
