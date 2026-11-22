/**
 * Design mode helpers for the live browser panel.
 *
 * Design mode turns point-and-prompt into a small review: the user marks
 * several elements, says what each should become (in words, or by trying a
 * size or colour on the page first), then sends the whole list to the agent
 * in one message. These helpers own the shape of a marked change, the wording
 * shown in the panel and the prompt the agent receives. They are pure so they
 * can be tested without a DOM.
 *
 * Everything read from an element descriptor comes from the inspected page's
 * own JS realm: treat it as untrusted text and cap it before using it.
 */
import { describeElement, type SelectedElement } from './inspect';

/** Where the page says the element comes from (framework dev builds only). */
export interface ElementSource {
	file: string;
	line: number;
}

/** One of the page's own colour variables. */
export interface PageToken {
	name: string;
	value: string;
}

/** Extra descriptor fields the backend adds to a `select` hit. */
export interface DesignDetail {
	/** Handle for this element in the page-side preview registry. */
	key: string | null;
	label: string;
	styles: Record<string, string>;
	similar: number;
	source: ElementSource | null;
	tokens: PageToken[];
}

export type DesignScope = 'one' | 'all';

/** A change the user has marked on the page. */
export interface DesignChange {
	id: number;
	element: SelectedElement;
	detail: DesignDetail;
	note: string;
	/** CSS property → value being previewed on the page. */
	tweaks: Record<string, string>;
	scope: DesignScope;
}

/** Properties the preview may set. Mirrors the backend allow-list. */
export const DESIGN_PROPS = [
	'font-size',
	'font-weight',
	'color',
	'background-color',
	'padding',
	'border-radius',
	'margin-bottom',
] as const;
export type DesignProp = (typeof DESIGN_PROPS)[number];

const PROP_LABELS: Record<DesignProp, string> = {
	'font-size': 'Size',
	'font-weight': 'Weight',
	color: 'Text colour',
	'background-color': 'Background',
	padding: 'Padding',
	'border-radius': 'Corners',
	'margin-bottom': 'Space below',
};

/** Computed-style property that holds the starting value for a control. */
const ORIGIN_PROP: Record<DesignProp, string> = {
	'font-size': 'font-size',
	'font-weight': 'font-weight',
	color: 'color',
	'background-color': 'background-color',
	padding: 'padding-top',
	'border-radius': 'border-top-left-radius',
	'margin-bottom': 'margin-bottom',
};

const VALUE_RE = /^[#%(),./\w\s-]{1,64}$/;
const KEY_RE = /^k\d{1,7}$/;

export function propLabel(prop: DesignProp): string {
	return PROP_LABELS[prop];
}

/** Read the design fields off a raw `inspect-hit` element payload. */
export function parseDesignDetail(raw: unknown): DesignDetail {
	const el = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
	const styles: Record<string, string> = {};
	if (typeof el.styles === 'object' && el.styles !== null && !Array.isArray(el.styles)) {
		for (const [k, v] of Object.entries(el.styles as Record<string, unknown>).slice(0, 20)) {
			if (typeof v === 'string') styles[k.slice(0, 40)] = v.slice(0, 80);
		}
	}
	let source: ElementSource | null = null;
	if (typeof el.source === 'object' && el.source !== null) {
		const s = el.source as Record<string, unknown>;
		if (typeof s.file === 'string' && s.file) {
			source = {
				file: s.file.slice(0, 200),
				line: typeof s.line === 'number' && Number.isFinite(s.line) ? Math.max(0, Math.trunc(s.line)) : 0,
			};
		}
	}
	const tokens: PageToken[] = [];
	if (Array.isArray(el.tokens)) {
		for (const t of el.tokens.slice(0, 24)) {
			if (typeof t !== 'object' || t === null) continue;
			const { name, value } = t as Record<string, unknown>;
			if (typeof name === 'string' && typeof value === 'string' && /^--[\w-]{1,58}$/.test(name)) {
				tokens.push({ name, value: value.slice(0, 60) });
			}
		}
	}
	return {
		key: typeof el.key === 'string' && KEY_RE.test(el.key) ? el.key : null,
		label: typeof el.label === 'string' ? el.label.slice(0, 120) : '',
		styles,
		similar:
			typeof el.similar === 'number' && Number.isFinite(el.similar)
				? Math.max(1, Math.min(999, Math.trunc(el.similar)))
				: 1,
		source,
		tokens,
	};
}

const KIND_BY_TAG: Record<string, string> = {
	h1: 'Heading', h2: 'Heading', h3: 'Heading', h4: 'Heading', h5: 'Heading', h6: 'Heading',
	p: 'Paragraph', a: 'Link', button: 'Button', img: 'Image', svg: 'Icon', picture: 'Image',
	input: 'Field', textarea: 'Field', select: 'Menu', label: 'Label', li: 'List item',
	ul: 'List', ol: 'List', nav: 'Navigation', header: 'Header', footer: 'Footer',
	main: 'Page body', section: 'Section', article: 'Article', aside: 'Sidebar',
	form: 'Form', table: 'Table', td: 'Table cell', th: 'Table heading', video: 'Video',
	span: 'Text', strong: 'Text', em: 'Text', small: 'Text', code: 'Code',
};

const KIND_BY_ROLE: Record<string, string> = {
	button: 'Button', link: 'Link', heading: 'Heading', img: 'Image', tab: 'Tab',
	dialog: 'Dialog', navigation: 'Navigation', textbox: 'Field', checkbox: 'Checkbox',
	switch: 'Switch', menuitem: 'Menu item', listitem: 'List item',
};

/** What a non-developer would call the element: "Button", "Heading", "Block". */
export function elementKind(el: SelectedElement): string {
	const role = (el.role ?? '').toLowerCase();
	return KIND_BY_ROLE[role] ?? KIND_BY_TAG[el.tag.toLowerCase()] ?? 'Block';
}

function clip(text: string, max: number): string {
	const flat = text.replace(/\s+/g, ' ').trim();
	return flat.length > max ? `${flat.slice(0, max - 1).trimEnd()}…` : flat;
}

/** The words on or about the element, if it has any. */
export function elementWords(el: SelectedElement, detail: DesignDetail, max = 44): string {
	return clip(el.text || detail.label || '', max);
}

/** Short file reference such as `Hero.svelte:12` (full path stays in the title). */
export function sourceLabel(source: ElementSource | null): string {
	if (!source) return '';
	const name = source.file.split(/[\\/]/).pop() || source.file;
	return source.line > 0 ? `${name}:${source.line}` : name;
}

/** A value the preview is allowed to carry, or null. Mirrors the backend. */
export function safeValue(value: string): string | null {
	const v = value.trim();
	if (!VALUE_RE.test(v) || /url\(/i.test(v)) return null;
	return v;
}

/** The element's starting value for a control, e.g. `32px` or `rgb(…)`. */
export function originalValue(detail: DesignDetail, prop: DesignProp): string {
	return detail.styles[ORIGIN_PROP[prop]] ?? '';
}

/** Pixel number out of a computed length, or `fallback` when it has none. */
export function pixels(value: string, fallback = 0): number {
	const n = Number.parseFloat(value);
	return Number.isFinite(n) ? Math.round(n * 10) / 10 : fallback;
}

/** `#rrggbb` for a computed `rgb()`/`rgba()`/hex colour, or null. */
export function toHex(color: string): string | null {
	const c = color.trim();
	const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(c);
	if (hex) {
		const h = hex[1];
		return `#${(h.length === 3 ? h.replace(/./g, (x) => x + x) : h).toLowerCase()}`;
	}
	const rgb = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/i.exec(c);
	if (!rgb) return null;
	const part = (s: string) =>
		Math.max(0, Math.min(255, Math.round(Number.parseFloat(s))))
			.toString(16)
			.padStart(2, '0');
	return `#${part(rgb[1])}${part(rgb[2])}${part(rgb[3])}`;
}

/** A colour as hue (0–360), saturation and lightness (both 0–100). */
export interface Hsl {
	h: number;
	s: number;
	l: number;
}

/** Hue, saturation and lightness of a hex or `rgb()` colour, or null. */
export function toHsl(color: string): Hsl | null {
	const hex = toHex(color);
	if (!hex) return null;
	const [r, g, b] = [1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16) / 255);
	const max = Math.max(r, g, b);
	const min = Math.min(r, g, b);
	const l = (max + min) / 2;
	const d = max - min;
	if (d === 0) return { h: 0, s: 0, l: l * 100 };
	const s = d / (1 - Math.abs(2 * l - 1));
	const h = max === r ? ((g - b) / d + 6) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
	return { h: h * 60, s: s * 100, l: l * 100 };
}

/** `#rrggbb` for a hue, saturation and lightness. */
export function hslToHex(h: number, s: number, l: number): string {
	const sat = Math.max(0, Math.min(100, s)) / 100;
	const light = Math.max(0, Math.min(100, l)) / 100;
	const a = sat * Math.min(light, 1 - light);
	const part = (n: number) => {
		const k = (n + (((h % 360) + 360) % 360) / 30) % 12;
		const v = light - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
		return Math.round(v * 255)
			.toString(16)
			.padStart(2, '0');
	};
	return `#${part(0)}${part(8)}${part(4)}`;
}

/** True when a computed colour paints nothing (e.g. `rgba(0, 0, 0, 0)`). */
export function isTransparent(color: string): boolean {
	const c = color.trim().toLowerCase();
	return c === 'transparent' || /^rgba\([^)]*,\s*0\)$/.test(c);
}

/** How a previewed value reads in a sentence: `var(--brand)` → `brand`. */
export function readableValue(value: string): string {
	const token = /^var\((--[\w-]+)\)$/.exec(value.trim());
	return token ? token[1].replace(/^--/, '') : value.trim();
}

/** One line per tried value: `Size 32px → 40px`. */
export function tweakLines(change: Pick<DesignChange, 'detail' | 'tweaks'>): string[] {
	const lines: string[] = [];
	for (const prop of DESIGN_PROPS) {
		const next = change.tweaks[prop];
		if (!next) continue;
		const from = originalValue(change.detail, prop);
		const fromText = prop === 'color' || prop === 'background-color' ? toHex(from) ?? from : from;
		lines.push(`${PROP_LABELS[prop]} ${fromText ? `${fromText} → ` : ''}${readableValue(next)}`);
	}
	return lines;
}

/** True when a change says nothing yet: no note and nothing tried. */
export function isEmptyChange(change: Pick<DesignChange, 'note' | 'tweaks'>): boolean {
	return !change.note.trim() && Object.keys(change.tweaks).length === 0;
}

/** The one-line summary shown in the change list. */
export function changeSummary(change: Pick<DesignChange, 'note' | 'detail' | 'tweaks'>): string {
	const note = clip(change.note, 90);
	const tried = tweakLines(change).join(', ');
	if (note && tried) return `${note} (${tried})`;
	return note || tried;
}

/**
 * Compose the single chat message for a list of marked changes. Each entry
 * carries what the agent needs to find the element in source (selector, the
 * file the page reported, an HTML snippet) and what the user wants: their
 * words and any values they tried on the page.
 */
export function composeDesignPrompt(changes: DesignChange[], url: string | null): string {
	const many = changes.length !== 1;
	const lines: string[] = [
		`Using the live browser preview${url ? ` of ${url}` : ''}, I marked ${changes.length} change${many ? 's' : ''}:`,
	];
	changes.forEach((change, index) => {
		const el = change.element;
		const words = elementWords(el, change.detail, 80);
		lines.push(
			'',
			`${index + 1}. ${elementKind(el)}${words ? ` ${JSON.stringify(words)}` : ''} (${describeElement(el)})`,
			`   - selector: ${el.selector}`,
		);
		if (change.detail.source) {
			const { file, line } = change.detail.source;
			lines.push(`   - source: ${file}${line > 0 ? `:${line}` : ''}`);
		}
		if (change.detail.similar > 1) {
			lines.push(
				change.scope === 'all'
					? `   - applies to: all ${change.detail.similar} elements like this one`
					: `   - applies to: only this one (${change.detail.similar} elements share its classes)`,
			);
		}
		if (change.note.trim()) lines.push(`   - request: ${change.note.trim()}`);
		const tried = tweakLines(change);
		if (tried.length) lines.push(`   - tried in the preview: ${tried.join('; ')}`);
		if (el.html) lines.push('   ```html', ...el.html.split('\n').map((l) => `   ${l}`), '   ```');
	});
	lines.push(
		'',
		`Find where ${many ? 'each element is' : 'this element is'} defined in the project source and make the change${many ? 's' : ''} there.`,
	);
	if (changes.some((c) => Object.keys(c.tweaks).length > 0)) {
		lines.push(
			'Values under "tried in the preview" are the look I want. Use the project\'s own tokens or scale where one is close instead of a one-off value.',
		);
	}
	return lines.join('\n');
}
