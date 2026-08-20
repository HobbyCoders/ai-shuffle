/**
 * Synchronous Shiki highlighter for chat code blocks.
 *
 * Why the *sync* core: the markdown pipeline in AssistantMessage is a pure
 * synchronous `marked` render driven from a `$derived`. An async highlighter
 * would force a second render pass per code block and re-tear the {@html}
 * subtree — which is exactly what makes embedded media flicker while a message
 * streams. `createHighlighterCoreSync` + the JavaScript RegExp engine gives us
 * highlighting inside the same synchronous call, with no WASM fetch.
 *
 * Themes are emitted as CSS custom properties (`--shiki-light` / `--shiki-dark`)
 * so ONE highlighted string serves both app themes; the `.dark` class on <html>
 * picks which variable wins (see AssistantMessage's scoped styles). That keeps
 * the cache theme-independent — a theme switch never invalidates a single entry.
 *
 * The cache is module-level and LRU-capped: a long session re-renders the same
 * settled code blocks on every scroll-back, and re-tokenising them is by far the
 * most expensive thing the transcript does.
 */
import { createHighlighterCoreSync, type HighlighterCore } from 'shiki/core';
import { createJavaScriptRegexEngine } from 'shiki/engine/javascript';

import langTs from 'shiki/langs/typescript.mjs';
import langJs from 'shiki/langs/javascript.mjs';
import langSvelte from 'shiki/langs/svelte.mjs';
import langHtml from 'shiki/langs/html.mjs';
import langCss from 'shiki/langs/css.mjs';
import langJson from 'shiki/langs/json.mjs';
import langPython from 'shiki/langs/python.mjs';
import langBash from 'shiki/langs/bash.mjs';
import langDiff from 'shiki/langs/diff.mjs';
import langMarkdown from 'shiki/langs/markdown.mjs';
import langYaml from 'shiki/langs/yaml.mjs';
import langSql from 'shiki/langs/sql.mjs';
import langRust from 'shiki/langs/rust.mjs';
import langGo from 'shiki/langs/go.mjs';

import themeDark from 'shiki/themes/github-dark-default.mjs';
import themeLight from 'shiki/themes/github-light-default.mjs';

const DARK_THEME = 'github-dark-default';
const LIGHT_THEME = 'github-light-default';

/**
 * Fence languages we accept, mapped to the loaded grammar id. Anything not in
 * here renders unhighlighted (still inside the code-block chrome) rather than
 * throwing — an unknown fence tag must never break a message.
 */
const LANG_ALIASES: Record<string, string> = {
	ts: 'typescript', typescript: 'typescript', tsx: 'typescript', mts: 'typescript', cts: 'typescript',
	js: 'javascript', javascript: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript',
	svelte: 'svelte',
	html: 'html', xml: 'html', vue: 'html',
	css: 'css', scss: 'css', postcss: 'css',
	json: 'json', jsonc: 'json',
	py: 'python', python: 'python',
	sh: 'bash', bash: 'bash', shell: 'bash', zsh: 'bash', console: 'bash',
	diff: 'diff', patch: 'diff',
	md: 'markdown', markdown: 'markdown',
	yml: 'yaml', yaml: 'yaml',
	sql: 'sql',
	rs: 'rust', rust: 'rust',
	go: 'go', golang: 'go'
};

/** Resolve a fence tag to a loaded grammar id, or null when unsupported. */
export function resolveShikiLang(lang: string | undefined | null): string | null {
	if (!lang) return null;
	return LANG_ALIASES[lang.trim().toLowerCase()] ?? null;
}

let _highlighter: HighlighterCore | null = null;
let _highlighterFailed = false;

function getHighlighter(): HighlighterCore | null {
	if (_highlighter) return _highlighter;
	if (_highlighterFailed) return null;
	try {
		_highlighter = createHighlighterCoreSync({
			themes: [themeDark, themeLight],
			langs: [
				langTs, langJs, langSvelte, langHtml, langCss, langJson, langPython,
				langBash, langDiff, langMarkdown, langYaml, langSql, langRust, langGo
			],
			engine: createJavaScriptRegexEngine()
		});
		return _highlighter;
	} catch (e) {
		// Never let a highlighter failure take the transcript down with it.
		console.error('[shiki] highlighter init failed; code blocks render plain', e);
		_highlighterFailed = true;
		return null;
	}
}

// ── LRU cache ────────────────────────────────────────────────────────────────
// Keyed on lang + code. Map preserves insertion order, so the oldest key is
// always the first one the iterator yields.
const CACHE_MAX = 240;
const _cache = new Map<string, string>();

function cacheGet(key: string): string | undefined {
	const hit = _cache.get(key);
	if (hit === undefined) return undefined;
	// Refresh recency.
	_cache.delete(key);
	_cache.set(key, hit);
	return hit;
}

function cacheSet(key: string, value: string) {
	_cache.set(key, value);
	while (_cache.size > CACHE_MAX) {
		const oldest = _cache.keys().next();
		if (oldest.done) break;
		_cache.delete(oldest.value);
	}
}

/** Test hook — drops every cached entry. */
export function clearHighlightCache() {
	_cache.clear();
}

/**
 * Highlight `code` as `lang`, returning the inner HTML of the `<code>` element
 * (spans only — the caller owns the `<pre>`/`<code>` wrapper and its chrome).
 *
 * Returns `null` when the language is unsupported or the highlighter is
 * unavailable, in which case the caller renders escaped plain text.
 */
export function highlightToInnerHtml(code: string, lang: string | null): string | null {
	if (!lang) return null;
	const key = `${lang}\n::\n${code}`;
	const cached = cacheGet(key);
	if (cached !== undefined) return cached;

	const hl = getHighlighter();
	if (!hl) return null;

	let html: string;
	try {
		html = hl.codeToHtml(code, {
			lang,
			themes: { light: LIGHT_THEME, dark: DARK_THEME },
			defaultColor: false,
			cssVariablePrefix: '--shiki-'
		});
	} catch (e) {
		console.error('[shiki] highlight failed', lang, e);
		return null;
	}

	// codeToHtml wraps in <pre class="shiki ..."><code>…</code></pre>. We only
	// want the code interior so the surrounding chrome stays ours.
	const open = html.indexOf('<code');
	const gt = open === -1 ? -1 : html.indexOf('>', open);
	const close = html.lastIndexOf('</code>');
	const inner = gt !== -1 && close > gt ? html.slice(gt + 1, close) : null;
	if (inner === null) return null;

	cacheSet(key, inner);
	return inner;
}
