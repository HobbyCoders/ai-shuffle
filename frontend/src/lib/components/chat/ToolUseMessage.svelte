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
	 *
	 * An Emulator / EmulatorScreenshot row carries "Show phone" beside it when
	 * the worker has the emulator and the Phone panel is not showing. The panel
	 * opens by itself when the agent drives the phone live; this is the way back
	 * after the user closed it or reopened the chat.
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
	import { phonePanel, isEmulatorTool } from '$lib/stores/phone-panel.svelte';
	import { PhoneSidePanel } from '$lib/components/deck/lazy/panels';
	import { Download, Smartphone } from 'lucide-svelte';
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
	}

	let { name, sessionId, input, partialInput, result, status: rawStatus, streaming = false, serverSide = false, screenshot, previewTitle }: Props = $props();

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

	const showPhone = $derived(
		isEmulatorTool(name) && phonePanel.offeredFor(sessionId) && !phonePanel.isOpen(sessionId ?? null)
	);

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

	// A generation's result renders as its media, not JSON: both the old
	// top-level shape and the unified tools' nested `result` (#784). A refused,
	// skipped or needs-confirmation result has none, so it stays as text.
	const generated = $derived(status === 'complete' ? toolResultMedia(shownResult) : []);
	const generatedLabel = $derived(toolResultMediaLabel(generated));
	const playable = $derived(
		generated.filter((item) => item.kind !== 'file').map((item, i, all) =>
			sanitizeHtml(mediaHtml(
				item.kind as MediaKind,
				resolveMediaUrl(item.src, sessionId),
				`Generated ${item.kind}${all.length > 1 ? ` ${i + 1}` : ''}`,
				item.filename
			))
		)
	);
	const files = $derived(generated.filter((item) => item.kind === 'file'));
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
	{#if showPhone}
		<button
			type="button"
			class="wl-phone"
			onclick={() => phonePanel.open(sessionId ?? null)}
			onpointerenter={() => void PhoneSidePanel.preload()}
			onfocus={() => void PhoneSidePanel.preload()}
			title="Show the phone the agent is using"
		>
			<Smartphone size={13} strokeWidth={1.9} aria-hidden="true" />
			<span>Show phone</span>
		</button>
	{/if}
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
				<div class="wl-pane" class:is-media={generated.length > 0}>
					{#if dismissedQuestion}
						<pre class="is-muted" data-testid="tool-dismissed-result">{shownResult}</pre>
					{:else if result}
						{#if generated.length > 0}
							<span class="wl-media-note" data-testid="tool-media-note">{generatedLabel}</span>
							{#if playable.length > 0}
								<div class="wl-media" class:is-many={playable.length > 1} data-testid="tool-media" use:retryImages>
									{#each playable as html, i (i)}
										{@html html}
									{/each}
								</div>
							{/if}
							{#each files as file (file.src)}
								<a
									class="wl-file"
									data-testid="tool-media-file"
									href={resolveMediaUrl(file.src, sessionId)}
									download={file.filename ?? ''}
									target="_blank"
									rel="noopener noreferrer"
								>
									<Download size={13} strokeWidth={1.9} aria-hidden="true" />
									<span>{file.filename ?? 'Download file'}</span>
								</a>
							{/each}
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

	/* "Show phone": the one action a tool row carries, so it reads as a
	   control (primary tint, pill) rather than another badge. */
	.wl-phone {
		position: relative;
		flex: none;
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		gap: 5px;
		height: 26px;
		padding: 0 10px 0 8px;
		border-radius: 999px;
		border: 1px solid color-mix(in oklab, var(--primary) 32%, var(--border));
		background: color-mix(in oklch, var(--primary) 7%, transparent);
		color: var(--primary);
		font-size: 11.5px;
		font-weight: 600;
		letter-spacing: 0.01em;
		white-space: nowrap;
		cursor: pointer;
		animation: wl-phone-in 220ms ease-out both;
		transition: background-color 160ms ease-out, border-color 160ms ease-out, transform 120ms ease-out;
	}

	.wl-phone:hover {
		background: color-mix(in oklch, var(--primary) 14%, transparent);
		border-color: color-mix(in oklab, var(--primary) 55%, var(--border));
	}

	.wl-phone:active {
		transform: scale(0.96);
	}

	.wl-phone:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	@keyframes wl-phone-in {
		from {
			opacity: 0;
			transform: translateX(-4px);
		}
	}

	@media (max-width: 639px) {
		/* A 44px touch target without a 44px pill. */
		.wl-phone::after {
			content: '';
			position: absolute;
			inset: -9px -4px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.wl-phone {
			animation: none;
			transition: none;
		}
	}

	.wl-media-note {
		display: block;
		font-size: 11.5px;
		color: var(--emerald);
	}

	/* Generated media sits in the Result pane at a working size: the shared
	   player markup (media.ts) with its expand/download overlay and lightbox,
	   scaled down so the work log stays a log. */
	.wl-pane.is-media {
		max-height: none;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 8px;
	}

	/* Stretch, not shrink-to-fit: the players' percentage max-widths need a
	   definite width, or a video collapses to its border. */
	.wl-media {
		align-self: stretch;
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		min-width: 0;
	}

	.wl-media :global(.generated-media-container) {
		margin: 0;
		max-width: 100%;
	}

	.wl-media :global(.generated-media-container > img),
	.wl-media :global(.generated-media-container > video) {
		display: block;
		max-height: 240px;
		box-shadow: none;
	}

	.wl-media :global(.generated-media-container > video) {
		width: min(100%, 427px);
	}

	.wl-media.is-many :global(.generated-media-container > img),
	.wl-media.is-many :global(.generated-media-container > video) {
		max-height: 160px;
	}

	.wl-media :global(.generated-audio) {
		width: min(28rem, 100%);
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
