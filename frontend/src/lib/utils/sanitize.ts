/**
 * HTML sanitization utility using DOMPurify.
 *
 * Centralizes XSS protection for all {@html} usage in the app.
 * DOMPurify strips dangerous elements (script, iframe, object, etc.)
 * while preserving safe HTML from markdown rendering.
 */

import DOMPurify, { type Config } from 'dompurify';
import { MEDIA_EXPAND_BUTTON_CLASS } from './media';

// Configure DOMPurify for our use case:
// - Allow common HTML from markdown rendering
// - Allow inline styles (needed for syntax highlighting, media)
// - Allow data: URIs for base64 images
// - Event handlers are never accepted from rendered content. App interactions
//   use delegated listeners rather than trusting CSS classes as provenance.
// - Agent-written HTML must not restyle the page or render controls that pass
//   for the app's own (#1718): <style> and every form element are dropped. The
//   only buttons kept are the icon controls the app's renderers emit (code copy,
//   media expand), and the only inputs kept are task-list checkboxes.
const FORBIDDEN_TAGS = [
	'style', 'form', 'select', 'textarea', 'option', 'optgroup', 'datalist',
	'fieldset', 'legend', 'output', 'progress', 'meter'
];

const PURIFY_CONFIG = {
	FORBID_TAGS: FORBIDDEN_TAGS,
	FORBID_ATTR: ['form', 'formaction', 'formmethod', 'formtarget', 'formenctype', 'formnovalidate'],
	ADD_TAGS: ['svg', 'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'g', 'defs', 'use'],
	ADD_ATTR: [
		'viewBox', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin',
		'd', 'cx', 'cy', 'r', 'x', 'y', 'width', 'height', 'points',
		'class', 'style', 'title', 'target', 'rel',
		'data-language', 'data-filename', 'data-line-numbers',
		// Inline media players (<audio>/<video>) + download links — see media.ts
		'controls', 'controlslist', 'preload', 'playsinline', 'loop', 'muted', 'autoplay', 'download', 'type', 'aria-label'
	],
	ALLOW_DATA_ATTR: true,
	// Allow blob: and data: URIs for images/media
	ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel|blob|data):|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$))/i,
} satisfies Config;

/** App-rendered buttons (chat-markdown.ts, media.ts, docs): the class that marks
 * each one, and every class token it is built from. */
const APP_BUTTONS: ReadonlyArray<{ marker: string; classes: ReadonlySet<string> }> = [
	{ marker: 'code-copy-btn', classes: new Set(['code-copy-btn']) },
	{ marker: 'media-expand-btn', classes: new Set(MEDIA_EXPAND_BUTTON_CLASS.split(/\s+/)) }
];
/** Visible text an app button may carry: icon-only, or the docs page's "Copy". */
const APP_BUTTON_TEXT = new Set(['', 'Copy']);

function isAppButton(node: Element): boolean {
	if ((node.getAttribute('type') ?? '').toLowerCase() !== 'button') return false;
	const tokens = (node.getAttribute('class') ?? '').split(/\s+/).filter(Boolean);
	if (tokens.length === 0) return false;
	const owner = APP_BUTTONS.find((button) => tokens.includes(button.marker));
	if (!owner || !tokens.every((token) => owner.classes.has(token))) return false;
	return APP_BUTTON_TEXT.has((node.textContent ?? '').trim());
}

/** A GFM task-list checkbox: `<li><input type="checkbox">` (or inside its `<p>`). */
function isTaskCheckbox(node: Element): boolean {
	if ((node.getAttribute('type') ?? '').toLowerCase() !== 'checkbox') return false;
	const parent = node.parentElement;
	if (!parent) return false;
	const tag = parent.tagName.toLowerCase();
	return tag === 'li' || (tag === 'p' && parent.parentElement?.tagName.toLowerCase() === 'li');
}

const TASK_CHECKBOX_ATTRS = new Set(['type', 'checked', 'disabled']);

DOMPurify.addHook('uponSanitizeElement', (node, data) => {
	const element = node as Element;
	if (data.tagName === 'button') {
		if (!isAppButton(element)) element.remove();
	} else if (data.tagName === 'input') {
		if (!isTaskCheckbox(element)) {
			element.remove();
			return;
		}
		for (const name of element.getAttributeNames()) {
			if (!TASK_CHECKBOX_ATTRS.has(name.toLowerCase())) element.removeAttribute(name);
		}
	}
});

/**
 * Sanitize HTML string to prevent XSS attacks.
 * Safe to use with Svelte's {@html} directive.
 */
export function sanitizeHtml(dirty: string): string {
	return DOMPurify.sanitize(dirty, PURIFY_CONFIG) as string;
}
