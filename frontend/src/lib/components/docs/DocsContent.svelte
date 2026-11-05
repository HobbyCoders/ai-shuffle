<script lang="ts">
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	/**
	 * DocsContent - Markdown content renderer for documentation pages
	 *
	 * Renders markdown to HTML using the `marked` library with:
	 * - Heading ID injection for anchor links
	 * - Code blocks with copy-to-clipboard
	 * - In-panel navigation for `/docs/<category>/<slug>#anchor` and `#anchor` links
	 * - Existing .prose CSS styling
	 */
	import { tick } from 'svelte';
	import { marked } from 'marked';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import { layout } from '$lib/stores/layout.svelte';
	import type { DocSection } from '$lib/docs/types';

	interface Props {
		content: string;
		/** DetailPane provides the article title; retain its anchor without a duplicate heading. */
		framed?: boolean;
		onHeadingsExtracted?: (headings: DocSection[]) => void;
	}

	let { content, framed = false, onHeadingsExtracted }: Props = $props();

	let contentEl = $state<HTMLDivElement | null>(null);

	// Custom renderer for heading IDs
	const renderer = new marked.Renderer();
	renderer.heading = ({ text, depth }: { text: string; depth: number }) => {
		const slug = text
			.toLowerCase()
			.replace(/<[^>]*>/g, '')
			.replace(/[^\w\s-]/g, '')
			.replace(/\s+/g, '-')
			.replace(/-+/g, '-')
			.trim();
		return `<h${depth} id="${slug}">${text}</h${depth}>`;
	};

	// Enhanced code blocks with copy button. The markup goes through the
	// sanitizer, which strips every inline handler, so the button carries no
	// onclick; the click is handled by delegation on the container below.
	renderer.code = ({ text, lang }: { text: string; lang?: string }) => {
		const langClass = lang ? ` language-${lang}` : '';
		const langLabel = lang ? `<span class="code-lang">${lang}</span>` : '';
		return `<div class="code-block-wrapper">
			<div class="code-block-header">
				${langLabel}
				<button type="button" class="code-copy-btn">Copy</button>
			</div>
			<pre><code class="${langClass}">${text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')}</code></pre>
		</div>`;
	};

	function scrollToAnchor(anchor: string) {
		if (!anchor || !contentEl) return;
		const el = contentEl.querySelector(`#${CSS.escape(decodeURIComponent(anchor))}`);
		el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
	}

	/**
	 * Doc pages link to each other as `/docs/<category>/<slug>` (a bare
	 * `/docs/<category>` means its `_index` page). The app has no `/docs` route,
	 * so a plain navigation would leave the app and 404. Open the target inside
	 * the Docs panel instead, through the same deep link the search uses.
	 */
	async function handleDocLink(e: MouseEvent, link: HTMLAnchorElement): Promise<boolean> {
		const href = link.getAttribute('href') ?? '';
		if (href.startsWith('#')) {
			e.preventDefault();
			scrollToAnchor(href.slice(1));
			return true;
		}
		const match = href.match(/^\/docs\/([\w-]+)(?:\/([\w-]+))?\/?(?:#(.*))?$/);
		if (!match) return false;
		e.preventDefault();
		const [, category, slug = '_index', anchor = ''] = match;
		layout.openFeaturePage('docs', `${category}/${slug}`);
		await tick();
		if (anchor) requestAnimationFrame(() => scrollToAnchor(anchor));
		return true;
	}

	async function handleContentClick(e: MouseEvent) {
		const target = e.target as HTMLElement | null;
		const link = target?.closest?.('a[href]') as HTMLAnchorElement | null;
		if (link && contentEl?.contains(link) && (await handleDocLink(e, link))) return;
		const btn = target?.closest?.('.code-copy-btn') as HTMLElement | null;
		if (!btn) return;
		e.preventDefault();
		const code = btn.closest('.code-block-wrapper')?.querySelector('code')?.textContent ?? '';
		if (!code) return;
		try {
			await copyText(code);
		} catch {
			btn.textContent = 'Copy failed';
			btn.title = COPY_ERROR_MESSAGE;
			return;
		}
		btn.removeAttribute('title');
		btn.textContent = 'Copied!';
		setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
	}

	const renderedHtml = $derived.by(() => {
		if (!content) return '';
		return marked.parse(content, { renderer, gfm: true, breaks: true }) as string;
	});

	// Extract headings after render for ToC
	$effect(() => {
		if (!contentEl || !onHeadingsExtracted) return;
		// Track new article content for the table of contents.
		void renderedHtml;
		requestAnimationFrame(() => {
			if (!contentEl) return;
			const headingEls = contentEl.querySelectorAll('h2, h3');
			const headings: DocSection[] = [];
			headingEls.forEach((el) => {
				headings.push({
					id: el.id,
					title: el.textContent || '',
					level: parseInt(el.tagName[1])
				});
			});
			onHeadingsExtracted?.(headings);
		});
	});
</script>

<!-- Native copy buttons emit clicks for Enter/Space too; a key handler here would copy twice. -->
<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
<div class="docs-content" class:framed bind:this={contentEl} onclick={handleContentClick}>
	{@html sanitizeHtml(renderedHtml)}
</div>

<style>
 .docs-content.framed :global(> h1:first-child) { display: none; }
 .docs-content { width:100%; font:14px/1.7 var(--font-sans); color:var(--foreground); overflow-wrap:anywhere; }
 .docs-content :global(h1) { font-size:28px; font-weight:600; line-height:1.25; margin:0 0 24px; }
 .docs-content :global(h2) { font-size:20px; font-weight:600; line-height:1.4; margin:40px 0 16px; }
 .docs-content :global(h3) { font-size:16px; font-weight:600; margin:32px 0 12px; }
 .docs-content :global(h1), .docs-content :global(h2), .docs-content :global(h3) { scroll-margin-top:24px; }
 .docs-content :global(p), .docs-content :global(ul), .docs-content :global(ol) { margin:0 0 16px; }
 .docs-content :global(ul) { list-style:disc; padding-left:24px; }
 .docs-content :global(ol) { list-style:decimal; padding-left:24px; }
 .docs-content :global(li) { margin-bottom:6px; }
 .docs-content :global(a) { color:var(--primary); text-decoration:underline; text-underline-offset:3px; }
 .docs-content :global(a:hover) { color:var(--foreground); }
 .docs-content :global(blockquote) { border-left:2px solid var(--border); color:var(--muted-foreground); padding-left:16px; margin:24px 0; }
 .docs-content :global(hr) { border:0; border-top:1px solid var(--border); margin:32px 0; }
 .docs-content :global(.code-block-wrapper) { margin:24px 0; border:1px solid var(--border); border-radius:var(--radius-control); overflow:hidden; background:var(--code-bg); }
 .docs-content :global(.code-block-header) { display:flex; align-items:center; justify-content:space-between; padding:8px 12px; border-bottom:1px solid var(--border); font-size:12px; color:var(--muted-foreground); }
 /* Sanitizer permits only this renderer-owned class on a prose control. */
 .docs-content :global(.code-copy-btn) { display:inline-flex; align-items:center; justify-content:center; height:32px; padding:0 12px; font:500 13px var(--font-sans); border:1px solid var(--border); border-radius:var(--radius-control); background:transparent; color:var(--foreground); cursor:pointer; }
 .docs-content :global(.code-copy-btn:hover) { background:var(--hover-overlay); }
 .docs-content :global(.code-copy-btn:focus-visible) { outline:2px solid var(--ring); outline-offset:2px; }
 @media (max-width:600px) { .docs-content :global(.code-copy-btn) { height:44px; } }
 .docs-content :global(pre) { margin:0; padding:16px; overflow:auto; }
 .docs-content :global(code) { font-family:var(--st-mono); font-size:13px; background:var(--code-bg); padding:2px 4px; border-radius:var(--radius-control); }
 .docs-content :global(pre code) { padding:0; background:none; }
 .docs-content :global(table) { width:100%; table-layout:fixed; border-collapse:collapse; margin:24px 0; font-size:13px; }
 .docs-content :global(th:first-child), .docs-content :global(td:first-child) { width:160px; overflow-wrap:anywhere; }
 .docs-content :global(th) { text-align:left; font-weight:500; padding:10px 12px; color:var(--muted-foreground); border-bottom:1px solid var(--border); }
 .docs-content :global(td) { overflow-wrap:anywhere; padding:10px 12px; border-bottom:1px solid var(--border); }
 .docs-content :global(img) { max-width:100%; height:auto; }
</style>
