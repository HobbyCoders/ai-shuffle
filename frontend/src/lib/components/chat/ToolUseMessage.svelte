<script lang="ts">
	/**
	 * ToolUseMessage — one flat work-log row for a single tool call.
	 *
	 * Row anatomy (Studio Noir `.wlrow`): subtle tool icon, bold short name, a
	 * truncated mono preview of the call's identifying argument, then a right
	 * cluster with the expand chevron and the status glyph. No card border — the
	 * transcript reads as a log, not a stack of panels.
	 *
	 * Expanding reveals the Input / Result panels indented past the icon gutter.
	 * Screenshots stay OUTSIDE the collapsible body: a screenshot the user can't
	 * see is useless.
	 */
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import ToolScreenshot from './ToolScreenshot.svelte';
	import ToolIcon from './ToolIcon.svelte';
	import { displayToolName } from '$lib/utils/tool-icons';
	import { toolPreview } from '$lib/utils/tool-preview';
	import { isDismissedQuestion, QUESTION_DISMISSED_LABEL } from './question-outcome';

	interface Props {
		name: string;
		input?: Record<string, unknown>;
		partialInput?: string;
		result?: string;
		status: 'running' | 'complete' | 'error';
		streaming?: boolean;
		serverSide?: boolean;
		/** Data URI of a screenshot from MCP image results (e.g. browser automation). */
		screenshot?: string;
	}

	let { name, input, partialInput, result, status, streaming = false, serverSide = false, screenshot }: Props = $props();

	const prefs = $derived(chatPreferences.current);

	// Once the user has toggled this row, their choice outranks the preference.
	let userToggled = $state(false);
	let expanded = $state(false);

	$effect(() => {
		if (!userToggled) expanded = prefs.toolsStartExpanded;
	});

	function toggle() {
		userToggled = true;
		expanded = !expanded;
	}

	const preview = $derived(
		toolPreview({ name, input, partialInput, result, streaming })
	);

	// Closing the AskUserQuestion modal resolves the call as a DENY (the tool has
	// no "unanswered" value). Mechanically correct, but not a failure — so the
	// row drops the error chrome for neutral, muted "dismissed" styling.
	const dismissedQuestion = $derived(isDismissedQuestion(name, status, result));

	function toolResultHasMedia(content: string): boolean {
		try {
			const data = JSON.parse(content);
			return data.success && (
				data.image_url || data.image_base64 || data.video_url ||
				data.audio_url || data.audio_base64 || data.file_url
			);
		} catch {
			return false;
		}
	}

	/** Short label for the generated-media kind ("Image" | "Video" | "Audio"). */
	function toolResultMediaLabel(content: string): string {
		try {
			const data = JSON.parse(content);
			if (data.video_url) return 'Video';
			if (data.audio_url || data.audio_base64) return 'Audio';
			return 'Image';
		} catch {
			return 'Image';
		}
	}
</script>

<div class="wl-item">
	<button
		type="button"
		class="wlrow"
		class:is-open={expanded}
		onclick={toggle}
		aria-expanded={expanded}
	>
		<ToolIcon {name} class="wig" />
		<span class="wname" title={name}>{displayToolName(name)}</span>

		{#if serverSide}
			<span class="wl-badge" title="Executed server-side by Anthropic API">server</span>
		{/if}

		{#if dismissedQuestion}
			<span class="wl-badge">dismissed</span>
			<span class="wtxt" title={QUESTION_DISMISSED_LABEL}>{QUESTION_DISMISSED_LABEL}</span>
		{:else if preview}
			<span class="wtxt" title={preview}>{preview}</span>
		{/if}

		<span class="wright">
			<svg class="wl-chev" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<path d="m6 9 6 6 6-6" />
			</svg>
			{#if dismissedQuestion}
				<svg class="wl-glyph is-muted" data-testid="tool-dismissed-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
					<circle cx="12" cy="12" r="9" />
					<path stroke-linecap="round" d="M8.5 12h7" />
				</svg>
			{:else if status === 'running'}
				<svg class="wl-glyph is-running" data-testid="tool-status-running" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
					<path d="M12 3a9 9 0 1 0 9 9" />
				</svg>
			{:else if status === 'error'}
				<svg class="wl-glyph is-error" data-testid="tool-status-error" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
					<path d="M18 6 6 18M6 6l12 12" />
				</svg>
			{:else}
				<svg class="wl-glyph is-ok" data-testid="tool-status-ok" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
					<path d="M20 6 9 17l-5-5" />
				</svg>
			{/if}
		</span>
	</button>

	{#if expanded && (partialInput || input)}
		<div class="wl-body">
			<div class="wl-panel">
				<div class="wl-panel-label">Input</div>
				<div class="wl-pane">
					{#if streaming && partialInput}
						<pre>{partialInput}<span class="wl-caret"></span></pre>
					{:else if input && Object.keys(input).length > 0}
						<pre>{JSON.stringify(input, null, 2)}</pre>
					{:else if partialInput}
						<pre>{partialInput}</pre>
					{/if}
				</div>
			</div>
			<div class="wl-panel">
				<div class="wl-panel-label">Result</div>
				<div class="wl-pane">
					{#if dismissedQuestion}
						<pre class="is-muted" data-testid="tool-dismissed-result">{result}</pre>
					{:else if result}
						{#if toolResultHasMedia(result)}
							<span class="wl-media-note">{toolResultMediaLabel(result)} generated</span>
						{:else}
							<pre>{result}</pre>
						{/if}
					{:else if status === 'error'}
						<pre class="is-error">[Interrupted]</pre>
					{:else if status === 'running'}
						<pre class="is-muted">Running…</pre>
					{:else}
						<pre class="is-muted"> </pre>
					{/if}
				</div>
			</div>
		</div>
	{/if}

	<!-- Outside the collapsible body on purpose: the screenshot is the result. -->
	{#if screenshot}
		<ToolScreenshot {screenshot} {name} />
	{/if}
</div>

<style>
	.wl-item {
		width: 100%;
		min-width: 0;
	}

	.wlrow {
		display: flex;
		align-items: center;
		gap: 9px;
		width: 100%;
		padding: 6px 8px;
		border-radius: 8px;
		text-align: left;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: background-color 0.13s ease;
	}

	.wlrow:hover {
		background: var(--scrim);
	}

	.wlrow:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.wlrow :global(.wig) {
		color: var(--subtle-foreground);
		flex: none;
		width: 14px;
		height: 14px;
	}

	.wname {
		font-size: 12.5px;
		font-weight: 600;
		color: var(--foreground);
		white-space: nowrap;
		flex: none;
	}

	.wtxt {
		font-family: var(--font-mono);
		font-size: 11.5px;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		min-width: 0;
	}

	.wl-badge {
		flex: none;
		font-size: 9.5px;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		padding: 1px 5px;
		border-radius: 5px;
		border: 1px solid var(--border);
		color: var(--subtle-foreground);
	}

	.wright {
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		gap: 8px;
		color: var(--subtle-foreground);
		flex: none;
	}

	.wl-chev {
		transition: transform 0.16s ease;
	}

	.wlrow.is-open .wl-chev {
		transform: rotate(180deg);
	}

	.wl-glyph.is-ok {
		color: var(--emerald);
	}

	.wl-glyph.is-error {
		color: var(--rose);
	}

	.wl-glyph.is-muted {
		color: var(--subtle-foreground);
	}

	.wl-glyph.is-running {
		color: var(--primary);
		animation: wl-spin 0.9s linear infinite;
	}

	@keyframes wl-spin {
		to {
			transform: rotate(360deg);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.wl-glyph.is-running {
			animation-duration: 2.4s;
		}
	}

	@media (max-width: 639px) {
		.wlrow {
			min-height: 44px;
		}
	}

	.wl-body {
		margin: 2px 0 6px 14px;
		padding-left: 12px;
		border-left: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.wl-panel {
		min-width: 0;
	}

	.wl-panel-label {
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.075em;
		text-transform: uppercase;
		color: var(--subtle-foreground);
		margin-bottom: 4px;
	}

	.wl-pane {
		max-height: 15rem;
		overflow: auto;
		border-radius: 7px;
		border: 1px solid var(--border);
		background: var(--code-bg);
		padding: 7px 9px;
	}

	.wl-pane pre {
		margin: 0;
		padding: 0;
		background: transparent;
		border-radius: 0;
		width: max-content;
		font-family: var(--font-mono);
		font-size: 11.5px;
		line-height: 1.6;
		color: var(--muted-foreground);
	}

	.wl-pane pre.is-muted {
		color: var(--subtle-foreground);
	}

	.wl-pane pre.is-error {
		color: var(--rose);
	}

	.wl-caret {
		display: inline-block;
		width: 5px;
		height: 11px;
		margin-left: 2px;
		vertical-align: middle;
		background: var(--primary);
		animation: wl-blink 1s steps(2, start) infinite;
	}

	@keyframes wl-blink {
		50% {
			opacity: 0;
		}
	}

	.wl-media-note {
		font-size: 11.5px;
		color: var(--emerald);
	}
</style>
