/**
 * Vite's `build.assetsInlineLimit`: never inline a font.
 *
 * Vite turns assets under 4 kB into `data:` URLs. One subset of
 * @fontsource-variable/jetbrains-mono is that small, and the server's
 * Content-Security-Policy (`font-src 'self'`) blocks `data:` fonts, so every
 * page load logged a refused font. Fonts are emitted as files instead;
 * everything else keeps Vite's default limit (returning undefined).
 *
 * @param {string} filePath
 * @returns {boolean | undefined}
 */
export function assetsInlineLimit(filePath) {
	return /\.(woff2?|ttf|otf|eot)(\?.*)?$/i.test(filePath) ? false : undefined;
}
