import { describe, expect, it } from 'vitest';
import {
	composeElementPrompt,
	describeElement,
	isDrawableRect,
	parseElementHit,
	type SelectedElement,
} from './inspect';

const rect = { x: 10, y: 20, width: 100, height: 40 };

function makeElement(overrides: Partial<SelectedElement> = {}): SelectedElement {
	return {
		selector: 'main > button#cta',
		tag: 'button',
		id: 'cta',
		classes: ['btn', 'btn-primary'],
		rect,
		...overrides,
	};
}

describe('isDrawableRect', () => {
	it('accepts a finite rect and rejects everything else', () => {
		expect(isDrawableRect(rect)).toBe(true);
		expect(isDrawableRect(null)).toBe(false);
		expect(isDrawableRect({ x: 1, y: 2, width: 3 })).toBe(false);
		expect(isDrawableRect({ ...rect, width: NaN })).toBe(false);
		expect(isDrawableRect({ ...rect, y: Infinity })).toBe(false);
	});
});

describe('parseElementHit', () => {
	it('parses a full descriptor', () => {
		const el = parseElementHit({
			selector: '#cta',
			tag: 'button',
			id: 'cta',
			classes: ['btn'],
			rect,
			role: 'button',
			text: 'Get started',
			html: '<button id="cta">Get started</button>',
			attrs: { id: 'cta', type: 'submit' },
		});
		expect(el).not.toBeNull();
		expect(el!.selector).toBe('#cta');
		expect(el!.text).toBe('Get started');
		expect(el!.attrs).toEqual({ id: 'cta', type: 'submit' });
	});

	it('returns null for misses and malformed payloads', () => {
		expect(parseElementHit(null)).toBeNull();
		expect(parseElementHit('string')).toBeNull();
		expect(parseElementHit({ tag: 'div' })).toBeNull(); // no selector
		expect(parseElementHit({ selector: '#x', tag: 'div' })).toBeNull(); // no rect
		expect(parseElementHit({ selector: '#x', tag: 'div', rect: { x: NaN } })).toBeNull();
	});

	it('drops non-string classes and caps page-provided lengths', () => {
		const el = parseElementHit({
			selector: 'x'.repeat(1000),
			tag: 'div',
			id: null,
			classes: ['ok', 42, null, 'also-ok'],
			rect,
			html: 'h'.repeat(5000),
		});
		expect(el!.classes).toEqual(['ok', 'also-ok']);
		expect(el!.selector.length).toBe(500);
		expect(el!.html!.length).toBe(1000);
	});
});

describe('describeElement', () => {
	it('builds tag#id.class labels, capping classes at two', () => {
		expect(describeElement(makeElement())).toBe('button#cta.btn.btn-primary');
		expect(
			describeElement(makeElement({ id: null, classes: ['a', 'b', 'c'] })),
		).toBe('button.a.b…');
		expect(describeElement(makeElement({ id: null, classes: [] }))).toBe('button');
	});
});

describe('composeElementPrompt', () => {
	it('embeds the ask plus selector/element context and the page url', () => {
		const prompt = composeElementPrompt(
			makeElement({ text: 'Get started' }),
			'http://localhost:5173/',
			'Make this button bigger and blue.',
		);
		expect(prompt).toContain('http://localhost:5173/');
		expect(prompt).toContain('- selector: main > button#cta');
		expect(prompt).toContain('- element: button#cta.btn.btn-primary');
		expect(prompt).toContain('- text: "Get started"');
		expect(prompt).toContain('Make this button bigger and blue.');
		expect(prompt).toContain('source');
	});

	it('includes the HTML snippet fenced when present, omits it when absent', () => {
		const withHtml = composeElementPrompt(
			makeElement({ html: '<button id="cta">Go</button>' }),
			null,
			'Center it.',
		);
		expect(withHtml).toContain('```html');
		expect(withHtml).toContain('<button id="cta">Go</button>');

		const withoutHtml = composeElementPrompt(makeElement(), null, 'Center it.');
		expect(withoutHtml).not.toContain('```html');
	});
});
