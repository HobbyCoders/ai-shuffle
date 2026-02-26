<script lang="ts">
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

	// Enhanced code blocks with copy button
	renderer.code = ({ text, lang }: { text: string; lang?: string }) => {
		const langClass = lang ? ` language-${lang}` : '';
		const langLabel = lang ? `<span class="code-lang">${lang}</span>` : '';
		return `<div class="code-block-wrapper">
			<div class="code-block-header">
				${langLabel}
				<button class="code-copy-btn" onclick="
					const code = this.closest('.code-block-wrapper').querySelector('code').textContent;
					navigator.clipboard.writeText(code);
					this.textContent = 'Copied!';
					setTimeout(() => this.textContent = 'Copy', 1500);
				">Copy</button>
			</div>
			<pre><code class="${langClass}">${text}</code></pre>
		</div>`;
	};

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

<div class="docs-content prose" bind:this={contentEl}>
	{@html sanitizeHtml(renderedHtml)}
</div>

<style>
	.docs-content {
		padding: var(--space-5);
		max-width: 800px;
		width: 100%;
	}

	/* Code block wrapper styling */
	.docs-content :global(.code-block-wrapper) {
		position: relative;
		margin: 1rem 0;
		border: 1px solid var(--border-subtle);
		border-radius: 8px;
		overflow: hidden;
		background: var(--surface-0);
	}

	.docs-content :global(.code-block-header) {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 6px 12px;
		background: var(--surface-active);
		border-bottom: 1px solid var(--border-subtle);
		font-size: 12px;
	}

	.docs-content :global(.code-lang) {
		color: var(--text-tertiary);
		font-family: var(--font-mono, 'JetBrains Mono', monospace);
		text-transform: uppercase;
		font-size: 11px;
		letter-spacing: 0.5px;
	}

	.docs-content :global(.code-copy-btn) {
		padding: 2px 10px;
		font-size: 12px;
		color: var(--text-secondary);
		background: var(--surface-0);
		border: 1px solid var(--border-default);
		border-radius: 4px;
		cursor: pointer;
		transition: all 0.15s;
	}

	.docs-content :global(.code-copy-btn:hover) {
		color: var(--text-primary);
		background: var(--surface-active);
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
		background: var(--surface-active);
		border-bottom: 2px solid var(--border-default);
		color: var(--text-primary);
	}

	.docs-content :global(td) {
		padding: 8px 12px;
		border-bottom: 1px solid var(--border-subtle);
		color: var(--text-secondary);
	}

	.docs-content :global(tr:hover td) {
		background: var(--surface-0);
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
		color: var(--text-tertiary);
		font-weight: 400;
	}

	@media (max-width: 639px) {
		.docs-content {
			padding: var(--space-3);
		}

		.docs-content :global(h2:hover::before),
		.docs-content :global(h3:hover::before) {
			display: none;
		}
	}
</style>
