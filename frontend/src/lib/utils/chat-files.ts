import { apiOrigin, apiUrl } from '$lib/api/runtime';

/** Keep file requests on the authenticated viewer, with explicit chat ownership. */
export function chatFileUrl(href: string, sessionId?: string | null): string {
	const origin = apiOrigin();
	const local = origin && href.startsWith(`${origin}/api/`) ? href.slice(origin.length) : href;
	if (!/^\/api\/(?:files\/by-path\?|generated-(?:images|videos|audio)\/[^/?#])/.test(local)) return href;
	href = local;
	if (!sessionId) return apiUrl(href);
	const url = new URL(href, 'https://file.invalid');
	url.searchParams.set('session_id', sessionId);
	return apiUrl(`${url.pathname}${url.search}${url.hash}`);
}

/** Input is sanitized chat HTML, not markdown: code examples remain escaped. */
export function routeChatFiles(html: string, sessionId?: string | null): string {
	return html.replace(/\s(href|src)="([^"<>]*)"/g, (match, attr, href) => {
		const decoded = href.replace(/&amp;/g, '&');
		const routed = chatFileUrl(decoded, sessionId);
		return routed === decoded ? match : ` ${attr}="${routed.replace(/&/g, '&amp;').replace(/"/g, '&quot;')}"`;
	});
}
