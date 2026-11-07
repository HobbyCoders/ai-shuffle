/** UUID v4 for both HTTPS and plain-HTTP worker addresses. */
export function randomUUID(): string {
	try {
		if (typeof globalThis.crypto.randomUUID === 'function') return globalThis.crypto.randomUUID();
	} catch {
		// Some WebViews expose the method but refuse it outside a secure context.
	}
	// Unlike randomUUID, getRandomValues is available in insecure contexts too.
	const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
	bytes[6] = (bytes[6] & 0x0f) | 0x40;
	bytes[8] = (bytes[8] & 0x3f) | 0x80;
	const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
	return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
