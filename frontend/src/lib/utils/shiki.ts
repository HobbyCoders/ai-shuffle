/**
 * Lazily-loaded Shiki highlighter for chat code blocks.
 *
 * ## Why this is no longer a static import
 *
 * The previous version statically imported `shiki/core`, the JS RegExp engine,
 * two theme JSONs and **fourteen grammars** — ~750 kB of raw grammar data that
 * every visitor downloaded and parsed on boot, whether or not any message
 * contained code (perf review R6). The highlighter is now assembled on demand:
 *
 *  • `shiki/core` + engine + themes load as one `vendor-shiki` chunk.
 *  • Each grammar loads as its own chunk, **only for languages actually used**.
 *  • `warmHighlighter()` lets the shell pull the core in during idle time, so
 *    in practice the highlighter is ready long before the first code block.
 *
 * ## Staying synchronous for the caller
 *
 * The markdown pipeline in `chat-markdown.ts` is a pure synchronous `marked`
 * render driven from a `$derived`; it cannot await. `highlightToInnerHtml()`
 * therefore keeps its original signature and contract: it returns highlighted
 * HTML when the grammar is resident, and `null` otherwise — the caller already
 * falls back to escaped plain text for `null`. Asking for an unloaded language
 * kicks the load in the background and notifies `onHighlighterReady()`
 * subscribers when it lands, so the caller can re-render (the prefix/tail cache
 * makes that re-render cheap). A streaming message re-renders anyway.
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
import type { HighlighterCore } from 'shiki/core';

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

/**
 * One dynamic import per grammar. Each becomes its own Rollup chunk, so a
 * transcript full of Python pays for `python` and nothing else. Grammars that
 * embed others (svelte → ts/js/css) pull their dependencies through the shared
 * chunks Rollup derives automatically.
 */
const LANG_LOADERS: Record<string, () => Promise<{ default: unknown }>> = {
	typescript: () => import('shiki/langs/typescript.mjs'),
	javascript: () => import('shiki/langs/javascript.mjs'),
	svelte: () => import('shiki/langs/svelte.mjs'),
	html: () => import('shiki/langs/html.mjs'),
	css: () => import('shiki/langs/css.mjs'),
	json: () => import('shiki/langs/json.mjs'),
	python: () => import('shiki/langs/python.mjs'),
	bash: () => import('shiki/langs/bash.mjs'),
	diff: () => import('shiki/langs/diff.mjs'),
	markdown: () => import('shiki/langs/markdown.mjs'),
	yaml: () => import('shiki/langs/yaml.mjs'),
	sql: () => import('shiki/langs/sql.mjs'),
	rust: () => import('shiki/langs/rust.mjs'),
	go: () => import('shiki/langs/go.mjs')
};

/** Resolve a fence tag to a supported grammar id, or null when unsupported. */
export function resolveShikiLang(lang: string | undefined | null): string | null {
	if (!lang) return null;
	return LANG_ALIASES[lang.trim().toLowerCase()] ?? null;
}

// ── Highlighter lifecycle ────────────────────────────────────────────────────

let _highlighter: HighlighterCore | null = null;
let _corePromise: Promise<HighlighterCore | null> | null = null;
let _highlighterFailed = false;

const _loadedLangs = new Set<string>();
const _pendingLangs = new Set<string>();
const _readyListeners = new Set<() => void>();

/**
 * Subscribe to "new highlighting became available" (the core finished loading,
 * or a grammar landed). Consumers that render settled, non-streaming content
 * can use this to re-run their markdown derivation once, turning the plain-text
 * placeholder into highlighted output. Returns an unsubscribe function.
 *
 * Additive API — nothing that existed before changed shape.
 */
export function onHighlighterReady(listener: () => void): () => void {
	_readyListeners.add(listener);
	return () => _readyListeners.delete(listener);
}

function notifyReady() {
	for (const listener of [..._readyListeners]) {
		try {
			listener();
		} catch (e) {
			console.error('[shiki] ready listener threw', e);
		}
	}
}

async function loadCore(): Promise<HighlighterCore | null> {
	if (_highlighter) return _highlighter;
	if (_highlighterFailed) return null;
	if (_corePromise) return _corePromise;

	_corePromise = (async () => {
		try {
			const [{ createHighlighterCore }, { createJavaScriptRegexEngine }, dark, light] =
				await Promise.all([
					import('shiki/core'),
					import('shiki/engine/javascript'),
					import('shiki/themes/github-dark-default.mjs'),
					import('shiki/themes/github-light-default.mjs')
				]);

			_highlighter = await createHighlighterCore({
				themes: [dark.default, light.default],
				// Grammars are attached on demand by ensureLanguage().
				langs: [],
				engine: createJavaScriptRegexEngine()
			});
			return _highlighter;
		} catch (e) {
			// Never let a highlighter failure take the transcript down with it.
			console.error('[shiki] highlighter init failed; code blocks render plain', e);
			_highlighterFailed = true;
			return null;
		} finally {
			_corePromise = null;
		}
	})();

	return _corePromise;
}

/**
 * Pull the highlighter core in ahead of time (call it from an idle callback).
 * Optionally pre-attach specific grammars. Never throws.
 */
export function warmHighlighter(langs: readonly string[] = []): void {
	if (_highlighterFailed) return;
	void loadCore().then((core) => {
		if (!core) return;
		notifyReady();
		for (const lang of langs) ensureLanguage(lang);
	});
}

/** Kick off (once) the load of the core + one grammar. Never throws. */
function ensureLanguage(lang: string): void {
	if (_highlighterFailed) return;
	if (_loadedLangs.has(lang) || _pendingLangs.has(lang)) return;
	const loader = LANG_LOADERS[lang];
	if (!loader) return;

	_pendingLangs.add(lang);
	void (async () => {
		try {
			const [core, mod] = await Promise.all([loadCore(), loader()]);
			if (!core) return;
			await core.loadLanguage(mod.default as never);
			_loadedLangs.add(lang);
			notifyReady();
		} catch (e) {
			console.error('[shiki] grammar load failed; rendering plain', lang, e);
		} finally {
			_pendingLangs.delete(lang);
		}
	})();
}

/** True when `lang` can be highlighted synchronously right now. */
export function isHighlightReady(lang: string | null): boolean {
	return !!lang && _highlighter !== null && _loadedLangs.has(lang);
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
 * Returns `null` when the language is unsupported, when the highlighter is
 * unavailable, or when the grammar has not finished loading yet — in which case
 * the caller renders escaped plain text. In the last case the grammar load is
 * started here and `onHighlighterReady` fires when it lands.
 */
export function highlightToInnerHtml(code: string, lang: string | null): string | null {
	if (!lang) return null;
	const key = `${lang}\n::\n${code}`;
	const cached = cacheGet(key);
	if (cached !== undefined) return cached;

	const hl = _highlighter;
	if (!hl || !_loadedLangs.has(lang)) {
		// Not ready: start (or continue) loading and render plain for now.
		ensureLanguage(lang);
		return null;
	}

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
