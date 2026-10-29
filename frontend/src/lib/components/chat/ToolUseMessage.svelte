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
	 * see is useless. Generated media (an image, video, audio clip or file a
	 * completed generation made) follows the same rule (#784).
	 *
	 */
	import { untrack } from 'svelte';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import ToolScreenshot from './ToolScreenshot.svelte';
	import { swarms } from '$lib/stores/swarms';
	import { isSwarmTool, parseSwarm } from '$lib/utils/swarm';
	import ToolIcon from './ToolIcon.svelte';
	import { displayToolName } from '$lib/utils/tool-icons';
	import { liveToolLabel, toolPreview, toolStateLabel } from '$lib/utils/tool-preview';
	import { isDismissedQuestion, isPostedQuestion, QUESTION_DISMISSED_LABEL } from './question-outcome';
	import { stripUntrustedFence } from '$lib/utils/untrusted-fence';
	import { Download } from 'lucide-svelte';
	import {
		mediaHtml,
		resolveMediaUrl,
		retryImages,
		toolResultMedia,
		toolResultMediaLabel,
		type MediaKind
	} from '$lib/utils/media';
	import { sanitizeHtml } from '$lib/utils/sanitize';

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
		/**
		 * Resolved sources of generated media shown elsewhere on screen (the
		 * turn's gallery, or a reply that embeds the same picture): not drawn
		 * again here, so a picture never shows twice.
		 */
		hiddenMedia?: ReadonlySet<string>;
	}

	let { name, sessionId, input, partialInput, result, status: rawStatus, streaming = false, serverSide = false, screenshot, previewTitle, hiddenMedia }: Props = $props();

	// The delegated Claude engine posts a question card and then has to deny the
	// CLI's own AskUserQuestion call (an away owner's auto-answer goes back the
	// same way), so the result arrives flagged as an error. The question was
	// handled, so the row shows it as complete.
	const status = $derived(isPostedQuestion(name, rawStatus, result) ? 'complete' : rawStatus);

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

	// A generation's result IS its media, so it renders below the row like a
	// screenshot, never only inside the Result pane (#784). Both the old
	// top-level shape and the unified tools' nested `result` count. A refused,
	// skipped, needs-confirmation, failed or still-running call has none, so it
	// stays a text row. (Persisted history also spells a finished call "completed".)
	const finished = $derived(status === 'complete' || (rawStatus as string) === 'completed');
	const generated = $derived(finished && !streaming ? toolResultMedia(shownResult) : []);
	const generatedLabel = $derived(toolResultMediaLabel(generated));
	const outputs = $derived(
		generated
			.map((item) => ({ ...item, url: resolveMediaUrl(item.src, sessionId) }))
			.filter((item) => !hiddenMedia?.has(item.url))
	);
	const playable = $derived(
		outputs.filter((item) => item.kind !== 'file').map((item, i, all) => ({
			kind: item.kind,
			html: sanitizeHtml(mediaHtml(
				item.kind as MediaKind,
				item.url,
				`Generated ${item.kind}${all.length > 1 ? ` ${i + 1}` : ''}`,
				item.filename
			))
		}))
	);
	const pictures = $derived(playable.filter((item) => item.kind !== 'audio').length);
	const files = $derived(outputs.filter((item) => item.kind === 'file'));
</script>

<div class="wl-item">
	<div class="wl-head">
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
	</div>

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
						{#if generated.length > 0}
							<!-- The media itself sits below the row, outside this body. -->
							<span class="wl-media-note" data-testid="tool-media-note">{generatedLabel}</span>
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

	<!-- Generated media too: the picture is the result (#784). Same shared
	     player markup as a screenshot (expand, download, lightbox). -->
	{#if playable.length > 0}
		<div class="tool-media" class:is-many={pictures > 1} data-testid="tool-media" use:retryImages>
			{#each playable as item, i (i)}
				{@html item.html}
			{/each}
		</div>
	{/if}
	{#if files.length > 0}
		<div class="tool-files">
			{#each files as file (file.src)}
				<a
					class="wl-file"
					data-testid="tool-media-file"
					href={file.url}
					download={file.filename ?? ''}
					target="_blank"
					rel="noopener noreferrer"
				>
					<Download size={13} strokeWidth={1.9} aria-hidden="true" />
					<span>{file.filename ?? 'Download file'}</span>
				</a>
			{/each}
		</div>
	{/if}
</div>

<style>
	.wl-item {
		width: 100%;
		min-width: 0;
	}

	/* The row and its one action. "Show phone" sits beside the row while the
	   row keeps ~320px; a narrower column (a phone, or a chat squeezed by a
	   side panel) drops it to its own line, right-aligned under the status. */
	.wl-head {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		column-gap: 6px;
		row-gap: 2px;
		min-width: 0;
	}

	.wlrow {
		flex: 1 1 320px;
		min-width: 0;
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
		display: block;
		font-size: 11.5px;
		color: var(--emerald);
	}

	/* Generated media below the row, like ToolScreenshot: the shared player
	   markup (media.ts) brings its own spacing, border, expand/download overlay
	   and lightbox, so one output reads exactly like a screenshot. */
	.tool-media {
		display: block;
		min-width: 0;
	}

	/* Several outputs of one call tile on a row, smaller, so four pictures do
	   not become a wall. A tile wraps on a phone. */
	.tool-media.is-many {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin: 16px 0;
	}

	.tool-media.is-many :global(.generated-media-container) {
		margin: 0;
		max-width: 100%;
	}

	.tool-media.is-many :global(.generated-media-container > img),
	.tool-media.is-many :global(.generated-media-container > video) {
		display: block;
		max-height: 220px;
	}

	/* A video needs a definite width: the player's percentage max-width
	   against a shrink-to-fit container collapses it to its border. */
	.tool-media :global(.generated-media-container > video) {
		width: min(100%, 640px);
	}

	.tool-media.is-many :global(.generated-media-container > video) {
		width: min(100%, 392px);
	}

	.tool-media :global(.generated-audio) {
		width: min(28rem, 100%);
	}

	.tool-files {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin: 8px 0;
	}

	.wl-file {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		max-width: 100%;
		min-height: 28px;
		padding: 0 10px 0 8px;
		border-radius: 7px;
		border: 1px solid var(--border);
		color: var(--foreground);
		font-family: var(--font-mono);
		font-size: 11.5px;
		text-decoration: none;
		transition: background-color 160ms ease-out, border-color 160ms ease-out;
	}

	.wl-file span {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.wl-file :global(svg) {
		flex: none;
		color: var(--subtle-foreground);
	}

	.wl-file:hover {
		background: var(--scrim);
		border-color: color-mix(in oklab, var(--foreground) 22%, var(--border));
	}

	.wl-file:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	@media (max-width: 639px) {
		.wl-file {
			min-height: 44px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.wl-file {
			transition: none;
		}
	}
</style>
