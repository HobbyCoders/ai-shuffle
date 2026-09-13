<script lang="ts">
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	/**
	 * DocsContent - Markdown content renderer for documentation pages
	 *
	 * Renders markdown to HTML using the `marked` library with:
	 * - Heading ID injection for anchor links
	 * - Code blocks with copy-to-clipboard
	 * - Existing .prose CSS styling
	 */
	import { marked } from 'marked';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import type { DocSection } from '$lib/docs/types';

	interface Props {
		content: string;
		onHeadingsExtracted?: (headings: DocSection[]) => void;
	}

	let { content, onHeadingsExtracted }: Props = $props();

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

	async function handleContentClick(e: MouseEvent) {
		const target = e.target as HTMLElement | null;
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
		// Give the DOM a tick to update
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
<div class="docs-content prose" bind:this={contentEl} onclick={handleContentClick}>
	{@html sanitizeHtml(renderedHtml)}
</div>

<style>
	.docs-content {
		width: 100%;
	}

	/* ── Studio-v2 prose headings (serif display) ── */
	.docs-content :global(h1) {
		font-family: var(--font-display, Georgia, serif);
		font-weight: 800;
		font-size: 1.85rem;
		letter-spacing: -0.02em;
		line-height: 1.15;
		margin: 0 0 12px;
	}

	.docs-content :global(h2) {
		font-family: var(--font-display, Georgia, serif);
		font-weight: 700;
		font-size: 1.3rem;
		line-height: 1.2;
		margin: 32px 0 12px;
	}

	.docs-content :global(h3) {
		font-weight: 700;
		font-size: 1.05rem;
		margin: 24px 0 8px;
	}

	.docs-content :global(p),
	.docs-content :global(li) {
		font-size: 0.95rem;
		line-height: 1.7;
	}

	/* Code block wrapper styling */
	.docs-content :global(.code-block-wrapper) {
		position: relative;
		margin: 1rem 0;
		border: 1px solid var(--border);
		border-radius: 10px;
		overflow: hidden;
		background: var(--muted);
	}

	.docs-content :global(.code-block-header) {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 6px 12px;
		background: var(--card);
		border-bottom: 1px solid var(--border);
		font-size: 12px;
	}

	.docs-content :global(.code-lang) {
		color: var(--muted-foreground);
		font-family: var(--font-mono, 'JetBrains Mono', monospace);
		text-transform: uppercase;
		font-size: 11px;
		letter-spacing: 0.5px;
	}

	.docs-content :global(.code-copy-btn) {
		padding: 2px 10px;
		font-size: 12px;
		color: var(--muted-foreground);
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 4px;
		cursor: pointer;
		transition: all 0.15s;
	}

	.docs-content :global(.code-copy-btn:hover) {
		color: var(--foreground);
		background: var(--muted);
	}

	.docs-content :global(.code-block-wrapper pre) {
		margin: 0;
		padding: 16px;
		overflow-x: auto;
		border: none;
		border-radius: 0;
	}

	.docs-content :global(.code-block-wrapper code) {
		background: none;
		border: none;
		padding: 0;
		font-size: 13px;
		font-family: var(--font-mono, 'JetBrains Mono', monospace);
	}

	/* Table styling enhancements */
	.docs-content :global(table) {
		width: 100%;
		border-collapse: collapse;
		margin: 1rem 0;
		font-size: 14px;
	}

	.docs-content :global(th) {
		text-align: left;
		font-weight: 600;
		padding: 10px 12px;
		background: var(--muted);
		border-bottom: 2px solid var(--border);
		color: var(--foreground);
	}

	.docs-content :global(td) {
		padding: 8px 12px;
		border-bottom: 1px solid var(--border);
		color: var(--muted-foreground);
	}

	.docs-content :global(tr:hover td) {
		background: var(--muted);
	}

	/* Heading anchor hover indicator */
	.docs-content :global(h2),
	.docs-content :global(h3) {
		scroll-margin-top: 80px;
		cursor: pointer;
		position: relative;
	}

	.docs-content :global(h2:hover::before),
	.docs-content :global(h3:hover::before) {
		content: '#';
		position: absolute;
		left: -1.5em;
		color: var(--muted-foreground);
		font-weight: 400;
	}

	@media (max-width: 639px) {
		.docs-content :global(h2:hover::before),
		.docs-content :global(h3:hover::before) {
			display: none;
		}
	}
</style>
