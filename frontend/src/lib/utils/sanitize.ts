/**
 * HTML sanitization utility using DOMPurify.
 *
 * Centralizes XSS protection for all {@html} usage in the app.
 * DOMPurify strips dangerous elements (script, iframe, object, etc.)
 * while preserving safe HTML from markdown rendering.
 */

import DOMPurify, { type Config } from 'dompurify';

// Configure DOMPurify for our use case:
// - Allow common HTML from markdown rendering
// - Allow inline styles (needed for syntax highlighting, media)
// - Allow data: URIs for base64 images
// - onclick is allowed in ADD_ATTR but restricted by the afterSanitizeAttributes
//   hook below to only app-generated buttons (code-copy-btn, image overlays)
const PURIFY_CONFIG = {
	ADD_TAGS: ['svg', 'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'g', 'defs', 'use'],
	ADD_ATTR: [
		'viewBox', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin',
		'd', 'cx', 'cy', 'r', 'x', 'y', 'width', 'height', 'points',
		'onclick', 'class', 'style', 'title', 'target', 'rel',
		'data-language', 'data-filename', 'data-line-numbers'
	],
	ALLOW_DATA_ATTR: true,
	// Allow blob: and data: URIs for images/media
	ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel|blob|data):|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$))/i,
} satisfies Config;

// Restrict onclick to only app-generated buttons (code copy, image download overlays).
// Without this, any injected HTML with onclick would execute — e.g., via LLM prompt injection.
const ALLOWED_ONCLICK_CLASSES = ['code-copy-btn', 'image-overlay-btn', 'image-download-btn'];
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
	if (node.hasAttribute('onclick')) {
		const isAllowed = node.tagName === 'BUTTON' &&
			ALLOWED_ONCLICK_CLASSES.some(cls => node.classList.contains(cls));
		if (!isAllowed) {
			node.removeAttribute('onclick');
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
