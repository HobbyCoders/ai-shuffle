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
	import { untrack } from 'svelte';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import ToolScreenshot from './ToolScreenshot.svelte';
	import { swarms } from '$lib/stores/swarms';
	import { isSwarmTool, parseSwarm } from '$lib/utils/swarm';
	import ToolIcon from './ToolIcon.svelte';
	import { displayToolName } from '$lib/utils/tool-icons';
	import { liveToolLabel, toolPreview, toolStateLabel } from '$lib/utils/tool-preview';
	import { isDismissedQuestion, QUESTION_DISMISSED_LABEL } from './question-outcome';
	import { stripUntrustedFence } from '$lib/utils/untrusted-fence';

	interface Props {
		name: string;
		sessionId?: string | null;
		input?: Record<string, unknown>;
		partialInput?: string;
		result?: string;
		status: 'running' | 'complete' | 'error';
		streaming?: boolean;
		serverSide?: boolean;
		/** Data URI of a screenshot from MCP image results (e.g. browser automation). */
		screenshot?: string;
		/** Hover text for the preview, when it should say more than the preview. */
		previewTitle?: string;
	}

	let { name, sessionId, input, partialInput, result, status, streaming = false, serverSide = false, screenshot, previewTitle }: Props = $props();

	const prefs = $derived(chatPreferences.current);
	// Scalar boundaries stop replay's store-backed prop getters from making
	// this effect depend on the store it publishes into. Writes stay untracked.
	const swarmTool = $derived(isSwarmTool(name));
	const swarmResult = $derived(result);
	const swarmOwner = $derived(sessionId);
	$effect(() => {
		if (!swarmTool) return;
		const snapshot = parseSwarm(swarmResult);
		const owner = swarmOwner;
		if (snapshot && (!owner || snapshot.session_id === owner)) {
			untrack(() => swarms.receive(snapshot, owner ?? undefined));
		}
	});

	// Once the user has toggled this row, their choice outranks the preference.
	let userToggled = $state(false);
	let expanded = $state(false);

	$effect(() => {
		if (!userToggled) expanded = !isSwarmTool(name) && prefs.toolsStartExpanded;
	});

	function toggle() {
		userToggled = true;
		expanded = !expanded;
	}

	// The harness's model-facing "treat as data" wrapper, removed for display.
	const shownResult = $derived(stripUntrustedFence(result));

	const preview = $derived(
		toolPreview({ name, input, partialInput, result, streaming })
	);

	// Closing the AskUserQuestion modal resolves the call as a DENY (the tool has
	// no "unanswered" value). Mechanically correct, but not a failure — so the
	// row drops the error chrome for neutral, muted "dismissed" styling.
	const dismissedQuestion = $derived(isDismissedQuestion(name, status, result));
	const compactName = $derived(
		status === 'running' ? liveToolLabel(name, input, partialInput) : displayToolName(name)
	);
	const stateLabel = $derived(toolStateLabel(status, dismissedQuestion));
	const accessibleLabel = $derived(
		`${compactName}${preview ? `: ${preview}` : ''}. ${stateLabel}`
	);

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
		aria-label={accessibleLabel}
	>
		<ToolIcon {name} class="wig" />
		<span class="wname" title={name}>{compactName}</span>

		{#if serverSide}
			<span class="wl-badge" title="Run on the provider's servers">server</span>
		{/if}

		{#if dismissedQuestion}
			<span class="wl-badge">dismissed</span>
			<span class="wtxt" title={QUESTION_DISMISSED_LABEL}>{QUESTION_DISMISSED_LABEL}</span>
		{:else if preview}
			<span class="wtxt" title={previewTitle || preview}>{preview}</span>
		{/if}

		<span class="wright">
			{#if status !== 'complete' || dismissedQuestion}
				<span class="wl-state" class:is-error={status === 'error' && !dismissedQuestion}>{stateLabel}</span>
			{/if}
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

	{#if expanded && (partialInput || input || result || status === 'running' || status === 'error')}
		<div class="wl-body">
			{#if partialInput || (input && Object.keys(input).length > 0)}
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
			{/if}
			<div class="wl-panel">
				<div class="wl-panel-label">Result</div>
				<div class="wl-pane">
					{#if dismissedQuestion}
						<pre class="is-muted" data-testid="tool-dismissed-result">{shownResult}</pre>
					{:else if result}
						{#if toolResultHasMedia(result)}
							<span class="wl-media-note">{toolResultMediaLabel(result)} generated</span>
						{:else}
							<pre>{shownResult}</pre>
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
		<ToolScreenshot {screenshot} {name} {sessionId} />
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
		transition: background-color 180ms ease-out;
	}

	.wlrow:hover {
		background: var(--scrim);
	}

	.wlrow:active {
		background: var(--active-overlay, var(--scrim));
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

	.wl-state {
		font-family: var(--font-mono);
		font-size: 9.5px;
		font-variant-numeric: tabular-nums;
		color: var(--subtle-foreground);
	}

	.wl-state.is-error {
		color: var(--rose);
	}

	.wl-chev {
		transition: transform 180ms ease-out;
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
		.wl-glyph.is-running,
		.wl-caret {
			animation: none;
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
