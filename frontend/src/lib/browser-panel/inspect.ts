/**
 * Point-and-prompt inspect helpers for the live browser panel.
 *
 * The panel's inspect mode lets the user click any element in the screencast;
 * the backend hit-tests the page (CDP `Runtime.evaluate` + `elementFromPoint`)
 * and answers with an element descriptor. These helpers are the single source
 * of truth for that descriptor's shape, the human-readable chip label, and the
 * prompt sent to the agent — kept pure so they're unit-testable without a DOM.
 *
 * The descriptor comes from the INSPECTED PAGE's own JS realm, so every field
 * is untrusted text: render it as text (never HTML) and cap lengths when
 * embedding it anywhere.
 */

/** Page-CSS-pixel rect as reported by `getBoundingClientRect` in the page. */
export interface ElementRect {
	x: number;
	y: number;
	width: number;
	height: number;
}

/** Element descriptor delivered on an `inspect-hit` message. */
export interface SelectedElement {
	selector: string;
	tag: string;
	id: string | null;
	classes: string[];
	rect: ElementRect;
	/** Detail fields — present on `select` hits, absent on `hover` hits. */
	role?: string | null;
	text?: string;
	html?: string;
	attrs?: Record<string, string>;
}

/** Rect fields are page-provided; only a fully finite rect is drawable. */
export function isDrawableRect(rect: unknown): rect is ElementRect {
	if (typeof rect !== 'object' || rect === null) return false;
	const r = rect as Record<string, unknown>;
	return (['x', 'y', 'width', 'height'] as const).every(
		(k) => typeof r[k] === 'number' && Number.isFinite(r[k]),
	);
}

/**
 * Parse one `inspect-hit` element payload into a usable descriptor, or null
 * for a miss / malformed payload (the page owns the JS realm that built it).
 */
export function parseElementHit(raw: unknown): SelectedElement | null {
	if (typeof raw !== 'object' || raw === null) return null;
	const el = raw as Record<string, unknown>;
	if (typeof el.selector !== 'string' || typeof el.tag !== 'string') return null;
	if (!isDrawableRect(el.rect)) return null;
	const classes = Array.isArray(el.classes)
		? el.classes.filter((c): c is string => typeof c === 'string').slice(0, 12)
		: [];
	const out: SelectedElement = {
		selector: el.selector.slice(0, 500),
		tag: el.tag.slice(0, 40),
		id: typeof el.id === 'string' ? el.id.slice(0, 120) : null,
		classes,
		rect: el.rect,
	};
	if (typeof el.role === 'string') out.role = el.role.slice(0, 60);
	if (typeof el.text === 'string') out.text = el.text.slice(0, 300);
	if (typeof el.html === 'string') out.html = el.html.slice(0, 1000);
	if (typeof el.attrs === 'object' && el.attrs !== null && !Array.isArray(el.attrs)) {
		const attrs: Record<string, string> = {};
		for (const [k, v] of Object.entries(el.attrs as Record<string, unknown>).slice(0, 12)) {
			if (typeof v === 'string') attrs[k.slice(0, 60)] = v.slice(0, 200);
		}
		out.attrs = attrs;
	}
	return out;
}

/** Short chip label, e.g. `button#cta.btn.primary` (classes capped at two). */
export function describeElement(el: SelectedElement): string {
	let label = el.tag;
	if (el.id) label += `#${el.id}`;
	for (const cls of el.classes.slice(0, 2)) label += `.${cls}`;
	if (el.classes.length > 2) label += '…';
	return label;
}

/**
 * Compose the chat prompt for a point-and-prompt request: the user's ask plus
 * enough element context (selector, tag/classes, text, HTML snippet, page URL)
 * for the agent to find the matching source code — code stays the single
 * source of truth; the agent makes the edit.
 */
export function composeElementPrompt(
	el: SelectedElement,
	url: string | null,
	userText: string,
): string {
	const lines: string[] = [
		`Using the live browser preview${url ? ` of ${url}` : ''}, I selected this element:`,
		'',
		`- element: ${describeElement(el)}`,
		`- selector: ${el.selector}`,
	];
	if (el.role) lines.push(`- role: ${el.role}`);
	if (el.text) lines.push(`- text: ${JSON.stringify(el.text.slice(0, 160))}`);
	if (el.html) {
		lines.push('', 'HTML snippet:', '```html', el.html, '```');
	}
	lines.push('', userText.trim(), '', 'Find where this element is defined in the project source and make the change there.');
	return lines.join('\n');
}
