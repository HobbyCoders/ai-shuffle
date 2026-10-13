/** Human-readable byte size (base 1024, one decimal): `512 B`, `1.5 KB`, `2.0 MB`. Empty for a missing size. */
export function formatBytes(size: number | null | undefined): string {
	if (size === null || size === undefined) return '';
	if (size < 1024) return `${size} B`;
	if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
	return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
