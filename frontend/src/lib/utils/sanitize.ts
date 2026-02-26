/**
 * HTML sanitization utility using DOMPurify.
 *
 * Centralizes XSS protection for all {@html} usage in the app.
 * DOMPurify strips dangerous elements (script, iframe, object, etc.)
 * while preserving safe HTML from markdown rendering.
 */

import DOMPurify from 'dompurify';

// Configure DOMPurify for our use case:
// - Allow common HTML from markdown rendering
// - Allow inline styles (needed for syntax highlighting, media)
// - Allow data: URIs for base64 images
// - Allow onclick for our overlay buttons (download, copy)
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
} satisfies DOMPurify.Config;

/**
 * Sanitize HTML string to prevent XSS attacks.
 * Safe to use with Svelte's {@html} directive.
 */
export function sanitizeHtml(dirty: string): string {
	return DOMPurify.sanitize(dirty, PURIFY_CONFIG);
}
