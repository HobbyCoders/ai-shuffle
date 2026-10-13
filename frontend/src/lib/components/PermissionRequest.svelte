<script lang="ts">
	import { untrack } from 'svelte';
	import './deck/cards/card-design-system.css';
	import type { PermissionWorkerTag } from '$lib/api/client';

	export interface PermissionRequestData {
		request_id: string;
		tool_name: string;
		tool_input: Record<string, unknown>;
		queue_position?: number;
		queue_total?: number;
		// SDK 0.1.81 enrichment fields (Slice D) — optional, server only sends
		// when the underlying SDK populates them.
		title?: string;
		display_name?: string;
		description?: string;
		decision_reason?: string;
		blocked_path?: string;
		/** Set on a subagent's prompt; names the worker of a relayed one. */
		provider_agent?: PermissionWorkerTag | null;
		/** False when the prompt cannot be deferred (Switch model). This card
		 *  offers Approve and Deny only, so it never shows a Defer control. */
		deferrable?: boolean;
	}

	interface Props {
		request: PermissionRequestData;
		queueCount?: number;
		onRespond?: (detail: {
			request_id: string;
			decision: 'allow' | 'deny';
			remember?: 'none' | 'session' | 'profile';
			pattern?: string;
		}) => void;
	}

	let { request, queueCount = 1, onRespond }: Props = $props();

	let showDetails = $state(false);
	/**
	 * "Always do this for this tool" — a single opt-in that applies to whichever
	 * action button is pressed next (Approve → always allow, Deny → always deny).
	 * It maps to the existing profile-scoped remember rule with NO input pattern,
	 * so the saved rule matches every future call of this tool.
	 */
	let alwaysForTool = $state(false);
	// Slice D: collapse/expand state for long descriptions + reason/blocked details
	let descriptionExpanded = $state(false);
	let enrichmentDetailsExpanded = $state(false);
	const DESCRIPTION_TRUNCATE_LEN = 120;

	// Tool display configuration - uses theme CSS variables for colors
	// Maps tools to semantic theme colors: success, warning, info, primary, destructive
	const toolConfig: Record<string, {
		icon: string;
		themeColor: 'success' | 'warning' | 'info' | 'primary' | 'destructive' | 'muted';
		label: string;
	}> = {
		Bash: {
			icon: 'M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z',
			themeColor: 'success',
			label: 'Run Command'
		},
		Read: {
			icon: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z',
			themeColor: 'info',
			label: 'Read File'
		},
		Write: {
			icon: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z',
			themeColor: 'warning',
			label: 'Write File'
		},
		Edit: {
			icon: 'M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z',
			themeColor: 'warning',
			label: 'Edit File'
		},
		Glob: {
			icon: 'M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z',
			themeColor: 'primary',
			label: 'Find Files'
		},
		Grep: {
			icon: 'M10 21h7a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v11m0 5l4.879-4.879m0 0a3 3 0 104.243-4.242 3 3 0 00-4.243 4.242z',
			themeColor: 'primary',
			label: 'Search Content'
		},
		WebFetch: {
			icon: 'M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9',
			themeColor: 'info',
			label: 'Fetch URL'
		},
		WebSearch: {
			icon: 'M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z',
			themeColor: 'info',
			label: 'Web Search'
		},
		Task: {
			icon: 'M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z',
			themeColor: 'primary',
			label: 'Run Agent'
		},
		NotebookEdit: {
			icon: 'M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253',
			themeColor: 'destructive',
			label: 'Edit Notebook'
		}
	};

	function getToolConfig(name: string) {
		return toolConfig[name] || {
			icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065zM15 12a3 3 0 11-6 0 3 3 0 016 0z',
			themeColor: 'muted' as const,
			label: name
		};
	}

	// Get CSS variable for theme color
	function getThemeColorVar(color: string): string {
		return `var(--${color})`;
	}

	// Get tool summary for display
	function getToolSummary(): string {
		const input = request.tool_input;
		switch (request.tool_name) {
			case 'Bash':
				return String(input.command || '');
			case 'Read':
			case 'Write':
			case 'Edit':
				return String(input.file_path || '');
			case 'Glob':
			case 'Grep':
				return String(input.path || input.pattern || '');
			case 'WebFetch':
				return String(input.url || '');
			case 'WebSearch':
				return String(input.query || '');
			case 'Task':
			case 'Agent':
				return String(input.description || input.prompt || '').substring(0, 100);
			default:
				return JSON.stringify(input).substring(0, 100);
		}
	}

	// Format tool input for detailed display
	function formatToolInput(input: Record<string, unknown>): { key: string; value: string; truncated: boolean }[] {
		const result: { key: string; value: string; truncated: boolean }[] = [];
		const maxLength = 500;

		for (const [key, value] of Object.entries(input)) {
			let displayValue: string;
			let truncated = false;

			if (typeof value === 'string') {
				if (value.length > maxLength) {
					displayValue = value.substring(0, maxLength);
					truncated = true;
				} else {
					displayValue = value;
				}
			} else if (typeof value === 'object' && value !== null) {
				const json = JSON.stringify(value, null, 2);
				if (json.length > maxLength) {
					displayValue = json.substring(0, maxLength);
					truncated = true;
				} else {
					displayValue = json;
				}
			} else {
				displayValue = String(value);
			}

			result.push({ key, value: displayValue, truncated });
		}

		return result;
	}

	/** Remember scope for the decision about to be sent. The checkbox is a
	 *  tool-wide "always" — profile-scoped and pattern-less. */
	function currentRemember(): 'none' | 'profile' {
		return alwaysForTool && !relayedFrom && request.tool_name !== 'ModelSwitch' ? 'profile' : 'none';
	}

	/** The worker a relayed prompt came from: a subagent running on another
	 *  worker. Only allow-once or deny travels back there and a person decides
	 *  every one of its requests, so no "always" rule can apply to it. */
	const relayedFrom = $derived.by(() => {
		const tag = request.provider_agent;
		if (!tag || !tag.remote_approval_id) return null;
		return (tag.worker_name ?? '').trim() || 'another worker';
	});

	// Arming. A prompt must never be answered by input meant for something
	// else, so every way of answering it is held back for a moment after it
	// appears, and again whenever a new request replaces it:
	// - A pointer click on Approve / Deny counts only once the request has been
	//   visible for POINTER_ARM_DELAY_MS. The queue shows the next request's
	//   buttons where the last ones were, so the second click of a double-click
	//   would otherwise answer a request the user never saw.
	// - Enter (the page-wide approve shortcut, and Enter or Space on one of the
	//   prompt's focused buttons) counts only once the request has been visible
	//   for SHORTCUT_ARM_DELAY_MS, and only for a fresh press: never an
	//   auto-repeat, and never a key whose keydown predates the request. So an
	//   Enter typed a moment later to send a chat message (focus moves onto the
	//   prompt as it appears) and a held Enter cannot approve. After the window
	//   the first fresh press counts, with or without a keyup seen before it, so
	//   an idle keyboard or screen-reader user's first Enter works; a polite live
	//   region announces when the shortcuts become live.
	// - Escape denies (the safe side), so it also counts before the window once
	//   a keyup has been seen since the request appeared.
	// - A request is answered once: after an answer the buttons are disabled and
	//   further keys and clicks are ignored until the next request (or, if this
	//   one is still shown after ANSWER_RETRY_MS, e.g. the answer was lost on a
	//   dropped connection, until the buttons come back so it can be retried).
	// The page-wide shortcuts also leave alone keys a focused control owns:
	// Enter on a focused Deny / Details button activates that button, and
	// Enter / Escape while typing (the chat composer, a form field) stay with
	// the field instead of approving or denying.
	const SHORTCUT_ARM_DELAY_MS = 500;
	const POINTER_ARM_DELAY_MS = 300;
	const ANSWER_RETRY_MS = 5000;
	let answered = $state(false);
	let shortcutsArmed = $state(false);
	let pointerArmed = false;
	let keyupSeen = false;
	/** When this request appeared, on the clock events' `timeStamp` uses. */
	let shownAt = 0;
	/** An unarmed Enter / Space on one of the prompt's buttons: the click it
	 *  would generate is dropped too. */
	let keyClickBlocked = false;
	let panelEl: HTMLElement | undefined = $state();

	const requestId = $derived(request.request_id);
	$effect(() => {
		void requestId;
		// Everything per-request starts over when the request changes, even if
		// this component instance is reused for the next one.
		untrack(() => {
			alwaysForTool = false;
			showDetails = false;
			descriptionExpanded = false;
			enrichmentDetailsExpanded = false;
			answered = false;
			shortcutsArmed = false;
			pointerArmed = false;
			keyupSeen = false;
			keyClickBlocked = false;
			// A synthetic event's timeStamp reads the same clock as input events.
			shownAt = new Event('permission-shown').timeStamp;
		});
		const pointerTimer = setTimeout(() => (pointerArmed = true), POINTER_ARM_DELAY_MS);
		const shortcutTimer = setTimeout(() => (shortcutsArmed = true), SHORTCUT_ARM_DELAY_MS);
		return () => {
			clearTimeout(pointerTimer);
			clearTimeout(shortcutTimer);
		};
	});

	$effect(() => {
		if (!answered) return;
		const retry = setTimeout(() => (answered = false), ANSWER_RETRY_MS);
		return () => clearTimeout(retry);
	});

	function answer(decision: 'allow' | 'deny', e?: MouseEvent) {
		if (answered) return;
		if (e) {
			if (!pointerArmed) return;
			// A click with no pointer behind it (detail 0) that an unarmed Enter
			// or Space on the button generated.
			if (e.detail === 0 && keyClickBlocked) return;
		}
		answered = true;
		onRespond?.({
			request_id: request.request_id,
			decision,
			remember: currentRemember()
		});
	}

	function handleAllow(e?: MouseEvent) {
		answer('allow', e);
	}

	function handleDeny(e?: MouseEvent) {
		answer('deny', e);
	}

	/** Not an auto-repeat, and not a key that was already down when this
	 *  request appeared. */
	function isFreshPress(e: KeyboardEvent): boolean {
		if (e.repeat) return false;
		return !(e.timeStamp > 0 && e.timeStamp < shownAt);
	}

	function handleKeyup(e: KeyboardEvent) {
		keyupSeen = true;
		if (keyClickBlocked) {
			// Space activates a focused button on keyup: hold that back as well,
			// then let later clicks through once this one would have fired.
			if (isSpaceKey(e)) e.preventDefault();
			setTimeout(() => (keyClickBlocked = false), 0);
		}
	}

	function isSpaceKey(e: KeyboardEvent): boolean {
		return e.key === ' ' || e.key === 'Spacebar';
	}

	function isTextEntry(target: HTMLElement): boolean {
		if (target.isContentEditable || target.closest('textarea, select, [contenteditable="true"], [role="textbox"]')) return true;
		return target instanceof HTMLInputElement && !['checkbox', 'radio', 'button', 'submit', 'reset'].includes(target.type);
	}

	function ownsKey(target: EventTarget | null, key: string): boolean {
		if (!(target instanceof HTMLElement)) return false;
		if (isTextEntry(target)) return true;
		return key === 'Enter' && !!target.closest('button, a[href], input');
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.defaultPrevented || e.isComposing) return;
		const isEnter = e.key === 'Enter' && !e.shiftKey;
		const isSpace = isSpaceKey(e);
		if (!isEnter && !isSpace && e.key !== 'Escape') return;
		const target = e.target instanceof HTMLElement ? e.target : null;
		if (target && isTextEntry(target)) return;
		const inPanel = !!target && !!panelEl?.contains(target);

		const fresh = !answered && isFreshPress(e);
		if (isSpace) {
			// Space is not a shortcut; it only activates a focused button, and on
			// this prompt's buttons it is armed exactly like Enter.
			if (inPanel && target?.closest('button') && !(fresh && shortcutsArmed)) {
				e.preventDefault();
				keyClickBlocked = true;
			}
			return;
		}

		const armed = fresh && (shortcutsArmed || (!isEnter && keyupSeen));
		if (!armed) {
			// A native Enter activation of this prompt's own buttons (a focused
			// Approve after a click, then a held Enter) is held back too.
			if (isEnter && inPanel) {
				e.preventDefault();
				keyClickBlocked = true;
			}
			return;
		}
		if (ownsKey(target, e.key)) return;
		e.preventDefault();
		if (isEnter) handleAllow();
		else handleDeny();
	}

	const config = $derived(getToolConfig(request.tool_name));
	const summary = $derived(getToolSummary());
	const formattedInput = $derived(formatToolInput(request.tool_input));

	// Slice D: SDK 0.1.81 enrichment — prefer title, then display_name, then the
	// existing tool config label. Existing label remains the final fallback.
	const enrichedTitle = $derived(
		(request.title && request.title.trim()) ||
		(request.display_name && request.display_name.trim()) ||
		config.label
	);
	const hasEnrichmentDetails = $derived(
		Boolean(request.decision_reason || request.blocked_path)
	);
	// A Python cell's prompt (the `run_python` tool, titled "Run Python code")
	// carries the first lines of the code as its description. It is shown
	// whole in a scrolling block that replaces both the one-line subhead and
	// the JSON-escaped summary of the input. The block is keyed on the tool,
	// never on the description's shape or the title alone: both are supplied
	// by the caller, and other prompts carry multi-line descriptions too (a
	// ProviderAgent worker's tag line, a computer-use reason), where the
	// command line or path must stay visible.
	const PYTHON_CELL_TOOL = /^(?:mcp__.+__)?run_python$/;
	const codeDescription = $derived.by(() => {
		if (!PYTHON_CELL_TOOL.test(request.tool_name ?? '')) return null;
		// Keep the first line's indentation; drop blank edges only.
		const text = (request.description ?? '').replace(/^(?:[ \t]*\r?\n)+/, '').trimEnd();
		return text.trim() ? text : null;
	});
	const descriptionTruncatedView = $derived.by(() => {
		if (codeDescription !== null) return null;
		const desc = request.description?.trim();
		if (!desc) return null;
		const needsTruncate = desc.length > DESCRIPTION_TRUNCATE_LEN;
		const visible = !needsTruncate || descriptionExpanded
			? desc
			: desc.slice(0, DESCRIPTION_TRUNCATE_LEN).trimEnd() + '…';
		return { visible, needsTruncate };
	});

</script>

<svelte:window onkeydown={handleKeydown} onkeyup={handleKeyup} />


<!-- Permission Request Panel -->
<div
	bind:this={panelEl}
	class="card-system permission-panel rounded-2xl border shadow-lg overflow-hidden backdrop-blur-xs"
	style="
		border-color: var(--border);
		background: var(--card);
	"
	role="dialog"
	aria-labelledby="permission-title"
	tabindex="-1"
>
	<!-- Compact Header -->
	<div class="flex items-center gap-3 px-4 py-3 border-b border-border/40">
		<!-- Tool Icon -->
		<div
			class="shrink-0 w-9 h-9 rounded-xl border flex items-center justify-center shadow-xs"
			style="
				background: color-mix(in oklch, var(--muted) 50%, var(--card));
				border-color: var(--border);
			"
		>
			<svg class="w-4.5 h-4.5 text-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={config.icon} />
			</svg>
		</div>

		<!-- Title -->
		<div class="flex-1 min-w-0">
			<h3 id="permission-title" class="text-sm font-semibold text-foreground flex items-center gap-2">
				<span class="truncate">{enrichedTitle}</span>
				<span
					class="inline-flex items-center px-1.5 py-0.5 text-[10px] font-medium rounded-md border shrink-0"
					style="
						background: color-mix(in oklch, var(--muted-foreground) 15%, transparent);
						color: var(--muted-foreground);
						border-color: color-mix(in oklch, var(--muted-foreground) 20%, transparent);
					"
				>
					{relayedFrom ? `${relayedFrom} subagent` : 'Permission'}
				</span>
			</h3>
			<p class="text-xs text-muted-foreground truncate">{request.tool_name}</p>
		</div>

		<!-- Queue Badge -->
		{#if queueCount > 1}
			<div
				class="shrink-0 flex items-center gap-1.5 px-2 py-1 rounded-lg border"
				style="
					background: color-mix(in oklch, var(--muted-foreground) 10%, transparent);
					border-color: color-mix(in oklch, var(--muted-foreground) 20%, transparent);
				"
			>
				<span class="relative flex h-2 w-2">
					<span class="animate-ping absolute inline-flex h-full w-full rounded-full opacity-75" style="background: color-mix(in oklch, var(--muted-foreground) 60%, transparent);"></span>
					<span class="relative inline-flex rounded-full h-2 w-2" style="background: var(--muted-foreground);"></span>
				</span>
				<span class="text-xs font-medium" style="color: var(--muted-foreground);">{queueCount}</span>
			</div>
		{/if}
	</div>

	<!-- Command/Action Summary -->
	<div class="px-4 py-3">
		<!-- Slice D: SDK-provided description, rendered as supporting subhead. -->
		{#if descriptionTruncatedView}
			<p class="enrichment-description text-xs text-muted-foreground leading-relaxed mb-2">
				<span class="enrichment-description-text">{descriptionTruncatedView.visible}</span>
				{#if descriptionTruncatedView.needsTruncate}
					<button
						type="button"
						class="enrichment-expand-btn"
						onclick={() => (descriptionExpanded = !descriptionExpanded)}
					>
						{descriptionExpanded ? 'Show less' : 'Show more'}
					</button>
				{/if}
			</p>
		{/if}

		{#if codeDescription !== null}
			<pre class="permission-code" aria-label="Code to run">{codeDescription}</pre>
		{:else}
			<div class="relative group">
				<div class="text-sm text-foreground font-mono bg-card/60 rounded-xl px-3 py-2.5 border border-border/40 break-all leading-relaxed">
					{#if request.tool_name === 'Bash'}
						<span class="text-muted-foreground select-none">$ </span>
					{/if}
					{summary || 'No details available'}
				</div>
			</div>
		{/if}

		<!-- Slice D: enrichment details (decision_reason / blocked_path) -->
		{#if hasEnrichmentDetails}
			<button
				type="button"
				onclick={() => (enrichmentDetailsExpanded = !enrichmentDetailsExpanded)}
				class="mt-2 text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors"
				aria-expanded={enrichmentDetailsExpanded}
			>
				<svg
					class="w-3.5 h-3.5 transition-transform duration-200 {enrichmentDetailsExpanded ? 'rotate-90' : ''}"
					fill="none"
					stroke="currentColor"
					viewBox="0 0 24 24"
				>
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
				</svg>
				Details
			</button>
			{#if enrichmentDetailsExpanded}
				<div class="mt-2 space-y-2 animate-expand enrichment-details">
					{#if request.decision_reason}
						<div class="enrichment-row">
							<span class="enrichment-label">Reason</span>
							<p class="enrichment-value">{request.decision_reason}</p>
						</div>
					{/if}
					{#if request.blocked_path}
						<div class="enrichment-row">
							<span class="enrichment-label">Blocked path</span>
							<code class="enrichment-mono">{request.blocked_path}</code>
						</div>
					{/if}
				</div>
			{/if}
		{/if}

		<!-- Expand Details Toggle -->
		<button
			type="button"
			onclick={() => showDetails = !showDetails}
			class="mt-2 text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors"
		>
			<svg
				class="w-3.5 h-3.5 transition-transform duration-200 {showDetails ? 'rotate-90' : ''}"
				fill="none"
				stroke="currentColor"
				viewBox="0 0 24 24"
			>
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
			</svg>
			{showDetails ? 'Hide details' : 'Show details'}
		</button>

		<!-- Expanded Details -->
		{#if showDetails}
			<div class="mt-3 space-y-2 animate-expand">
				{#each formattedInput as { key, value, truncated }}
					<div class="text-sm">
						<span class="text-[10px] uppercase tracking-wider text-muted-foreground font-medium">{key}</span>
						<pre class="mt-1 p-2.5 bg-card/80 rounded-lg text-xs text-foreground overflow-x-auto whitespace-pre-wrap break-all font-mono border border-border/40 max-h-32 overflow-y-auto">{value}{#if truncated}<span class="text-muted-foreground">… (truncated)</span>{/if}</pre>
					</div>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Remember Option — one checkbox that applies to whichever button is pressed.
	     A relayed prompt has none: its answer applies to this request only. -->
	<div class="px-4 py-3 border-t border-border/40 bg-card/30">
		{#if request.tool_name === 'ModelSwitch'}
			<p>A yes covers this model for this chat, including after app restarts.</p>
		{:else if relayedFrom}
			<p class="relayed-note" data-testid="relayed-note">
				Asked by a subagent running on <strong>{relayedFrom}</strong>. Your answer applies
				to this request only; the subagent asks again for each one.
			</p>
		{:else}
		<label class="always-option {alwaysForTool ? 'selected' : ''}">
			<input
				type="checkbox"
				bind:checked={alwaysForTool}
				class="always-checkbox"
				aria-describedby="always-hint-{request.request_id}"
			/>
			<span class="always-label">Always do this for {request.tool_name}</span>
			<span id="always-hint-{request.request_id}" class="always-hint">
				Remembers the next choice — Approve saves an always-allow rule, Deny saves an always-deny rule.
			</span>
		</label>
		{/if}
	</div>

	<!-- Action Buttons -->
	<div class="px-4 py-3 border-t border-border/40 bg-card/20">
		<div class="flex gap-2 flex-wrap sm:flex-nowrap">
			<!-- Deny Button -->
			<button
				type="button"
				onclick={handleDeny}
				disabled={answered}
				class="deny-btn"
			>
				Deny
			</button>

			<!-- Approve Button -->
			<button
				type="button"
				onclick={handleAllow}
				disabled={answered}
				class="allow-btn"
			>
				Approve
			</button>
		</div>

		<!-- Tells screen-reader users when the shortcuts start to count. -->
		<p class="sr-only" role="status" aria-live="polite" aria-atomic="true">
			{shortcutsArmed && !answered ? 'Press Enter to approve, Escape to deny' : ''}
		</p>

		<!-- Keyboard Hints -->
		<div class="mt-2 flex flex-wrap items-center justify-center gap-3 text-[10px] text-muted-foreground">
			<span class="flex items-center gap-1">
				<kbd class="px-1.5 py-0.5 bg-muted/50 rounded-sm text-[9px] font-mono">Enter</kbd>
				<span>Approve</span>
			</span>
			<span class="flex items-center gap-1">
				<kbd class="px-1.5 py-0.5 bg-muted/50 rounded-sm text-[9px] font-mono">Esc</kbd>
				<span>Deny</span>
			</span>
		</div>
	</div>
</div>

<style>
	.permission-panel:focus {
		outline: none;
	}

	.permission-panel {
		animation: panel-enter 0.25s cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes panel-enter {
		from {
			opacity: 0;
			transform: translateY(8px) scale(0.98);
		}
		to {
			opacity: 1;
			transform: translateY(0) scale(1);
		}
	}

	.animate-expand {
		animation: expand 0.2s ease-out;
	}

	@keyframes expand {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	/* Remember Option — single "always for this tool" checkbox row. Mirrors the
	   card convention used by the old remember rows: bordered surface, subtle
	   tinted (never opaque) hover, 44px tap target on touch. */
	.always-option {
		display: grid;
		grid-template-columns: 1rem 1fr;
		grid-template-rows: auto auto;
		gap: 0.25rem 0.75rem;
		align-items: center;
		min-height: 44px;
		padding: 0.625rem 0.75rem;
		border-radius: var(--radius, 0.75rem);
		border: 1px solid var(--border);
		background: var(--card);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.always-option:hover {
		border-color: var(--accent);
		background: color-mix(in oklch, var(--accent) 8%, var(--card));
	}

	.always-option.selected {
		border-color: var(--selection-ring);
		background: color-mix(in oklch, var(--selection) 8%, var(--card));
		box-shadow: var(--selection-shadow-subtle);
	}

	.always-option:focus-within {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.always-checkbox {
		grid-row: 1;
		grid-column: 1;
		width: 1rem;
		height: 1rem;
		margin: 0;
		accent-color: var(--primary);
		cursor: pointer;
	}

	.always-label {
		grid-row: 1;
		grid-column: 2;
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.relayed-note {
		margin: 0;
		padding: 0.625rem 0.75rem;
		border-radius: var(--radius, 0.75rem);
		border: 1px solid var(--border);
		background: var(--card);
		font-size: 0.75rem;
		line-height: 1.5;
		color: var(--muted-foreground);
	}

	.relayed-note strong {
		color: var(--foreground);
		font-weight: 600;
	}

	.always-hint {
		grid-row: 2;
		grid-column: 2;
		font-size: 0.6875rem;
		line-height: 1.4;
		color: var(--muted-foreground);
	}

	/* Action Buttons */
	.deny-btn {
		flex: 1 1 0%;
		padding: 0.625rem 1rem;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: var(--radius, 0.75rem);
		background: var(--secondary);
		color: var(--secondary-foreground);
		border: 1px solid var(--border);
		transition: all 0.15s ease;
	}

	.deny-btn:hover {
		background: color-mix(in oklch, var(--secondary) 80%, var(--foreground));
		border-color: color-mix(in oklch, var(--border) 80%, var(--foreground));
	}

	.deny-btn:active {
		transform: scale(0.98);
	}

	.deny-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.allow-btn {
		flex: 1 1 0%;
		padding: 0.625rem 1.25rem;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: var(--radius, 0.75rem);
		background: var(--button-primary-gradient, var(--primary));
		color: var(--primary-foreground);
		border: 1px solid transparent;
		box-shadow: var(--shadow-s);
		transition: all 0.15s ease;
	}

	.allow-btn:hover {
		filter: brightness(1.1);
		box-shadow: var(--shadow-m), 0 0 20px var(--glow-color-soft);
	}

	.allow-btn:active {
		transform: scale(0.98);
	}

	.allow-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Ensure both action buttons clear the 44px mobile tap target. */
	.deny-btn,
	.allow-btn {
		min-height: 44px;
	}

	/* Answered, waiting for the next request. */
	.deny-btn:disabled,
	.allow-btn:disabled {
		opacity: 0.6;
		cursor: default;
	}

	/* Slice D: SDK enrichment subhead + details disclosure */
	.enrichment-description {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.5;
	}

	/* A multi-line description (a worker's tag line, a reason) keeps its breaks. */
	.enrichment-description-text {
		white-space: pre-line;
	}

	/* A Python cell's code: kept whole, line breaks
	   and indentation intact, scrolling past a readable height. */
	.permission-code {
		margin: 0;
		max-height: 16rem;
		overflow: auto;
		padding: 0.625rem 0.75rem;
		border-radius: var(--radius, 0.75rem);
		border: 1px solid var(--border);
		background: color-mix(in oklch, var(--muted) 35%, var(--card));
		color: var(--foreground);
		font-family: var(--font-mono);
		font-size: 0.75rem;
		line-height: 1.5;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		tab-size: 4;
	}

	.enrichment-expand-btn {
		margin-left: 0.25rem;
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--foreground);
		text-decoration: underline;
		text-underline-offset: 2px;
		background: transparent;
		border: 0;
		padding: 0;
		cursor: pointer;
	}

	.enrichment-expand-btn:hover {
		color: var(--primary);
	}

	.enrichment-row {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		padding: 0.5rem 0.625rem;
		border-radius: var(--radius, 0.75rem);
		background: var(--card);
		border: 1px solid var(--border);
	}

	.enrichment-label {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
		font-weight: 600;
	}

	.enrichment-value {
		font-size: 0.75rem;
		color: var(--foreground);
		line-height: 1.5;
		margin: 0;
		word-break: break-word;
	}

	.enrichment-mono {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--foreground);
		padding: 0.2rem 0.4rem;
		background: var(--muted);
		border-radius: 0.375rem;
		word-break: break-all;
	}

	@media (min-width: 640px) {
		.deny-btn {
			flex: 0 0 auto;
		}
	}

	/* Mobile optimizations */
	@media (max-width: 639px) {
		.permission-panel {
			border-radius: 1.25rem 1.25rem 0 0;
			border-bottom: none;
		}
	}
</style>
