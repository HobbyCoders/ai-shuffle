<script lang="ts">
	/**
	 * TaskChipTray: work an agent suggested for a separate chat, beside the composer.
	 *
	 * Up to three chips, then "and N more". A chip says what the work is, who
	 * raised it and which model it will run on, because Start is the owner's
	 * approval of that model. Start never touches this chat: no navigation, and
	 * a pointer press leaves focus in the composer. It renders from the chip list
	 * alone, so a chip from a subagent that has ended is as usable as any other.
	 */
	import type { TaskChip, TaskChipSource } from '$lib/api/sessions';
	import { taskChipModel, type TaskChipAction } from '$lib/stores/session/task-chips';
	import { uiSessionTag } from '$lib/utils/ui-signature';

	interface Props {
		sessionId: string | null;
		chips: TaskChip[];
		source?: TaskChipSource | null;
		busy?: Record<string, TaskChipAction>;
		errors?: Record<string, string>;
		/** The list has been read for this chat, so a chip that appears now is new. */
		loaded?: boolean;
		/** Something else is waiting beside the composer (an open question), so the chips take less room. */
		crowded?: boolean;
		hrefFor: (sessionId: string) => string;
		onStart: (chip: TaskChip) => void;
		onDismiss: (chip: TaskChip) => void;
		/** Takes a started chip out of the tray. */
		onClear: (chip: TaskChip) => void;
		/** A link to another chat was followed; the handler decides how it opens. */
		onOpenChat: (event: MouseEvent, sessionId: string, chip?: TaskChip) => void;
	}

	let { sessionId, chips, source = null, busy = {}, errors = {}, loaded = true, crowded = false, hrefFor, onStart, onDismiss, onClear, onOpenChat }: Props = $props();

	const VISIBLE = 3;
	let showAll = $state(false);
	let openChip = $state<string | null>(null);
	const shown = $derived(showAll ? chips : chips.slice(0, VISIBLE));
	const hidden = $derived(chips.slice(VISIBLE));
	const hiddenWaiting = $derived(hidden.filter((chip) => chip.state === 'pending' || chip.state === 'failed').length);
	const moreLabel = $derived(`and ${hidden.length} more${hiddenWaiting ? `, ${hiddenWaiting} to start` : ''}`);

	let known: Set<string> | null = null;
	let announcement = $state('');

	// One tray instance serves every chat.
	let lastSession: string | null = null;
	$effect.pre(() => {
		if (sessionId === lastSession) return;
		lastSession = sessionId;
		showAll = false;
		openChip = null;
		known = null;
		announcement = '';
	});
	$effect(() => { if (chips.length <= VISIBLE) showAll = false; });

	const baseLine = (chip: TaskChip) =>
		chip.base === 'current' ? 'Branches from the current work' : 'Branches from the default branch';
	const isStarting = (chip: TaskChip) => chip.state === 'starting' || busy[chip.id] === 'start';
	const actionLabel = (chip: TaskChip) => (isStarting(chip) ? 'Starting…' : chip.state === 'failed' ? 'Retry' : 'Start');
	const canRetry = (chip: TaskChip) => chip.state !== 'failed' || chip.retryable;
	const problem = (chip: TaskChip) => errors[chip.id] ?? (chip.state === 'failed' ? chip.error || 'This could not start.' : null);

	function start(chip: TaskChip) {
		if (!isStarting(chip) && !busy[chip.id]) onStart(chip);
	}

	/** A pointer press on a control leaves focus where it was (the composer, and a phone's keyboard). */
	function keepFocus(event: MouseEvent) {
		if ((event.target as Element | null)?.closest?.('button')) event.preventDefault();
	}

	// Keyboard focus survives the chip changing under it: to the same chip's main
	// control when it changes state, else to the chip that takes its place, else
	// back to the composer. A press never put focus here, so this leaves it alone.
	let list = $state<HTMLElement | null>(null);
	let focusBefore: { id: string; index: number } | null = null;
	const focusKey = $derived(shown.map((chip) => `${chip.id}:${chip.state}`).join());
	$effect.pre(() => {
		void focusKey;
		const row = list?.contains(document.activeElement) ? document.activeElement?.closest<HTMLElement>('[data-chip-id]') : null;
		focusBefore = row?.dataset.chipId
			? { id: row.dataset.chipId, index: Array.from(list!.children).indexOf(row) }
			: null;
	});
	$effect(() => {
		void focusKey;
		const before = focusBefore;
		focusBefore = null;
		if (!before || (document.activeElement && document.activeElement !== document.body)) return;
		const rows = list ? Array.from(list.querySelectorAll<HTMLElement>('[data-chip-id]')) : [];
		const row = rows.find((item) => item.dataset.chipId === before.id) ?? rows[before.index] ?? rows[before.index - 1];
		const target = row?.querySelector<HTMLElement>('[data-chip-main]')
			?? document.querySelector<HTMLElement>('[data-composer-editor], .ProseMirror');
		target?.focus();
	});

	// Say a new suggestion out loud once; opening a chat full of them says nothing.
	$effect(() => {
		if (!loaded) return;
		const fresh = known ? chips.filter((chip) => !known!.has(chip.id) && chip.state === 'pending') : [];
		known = new Set(chips.map((chip) => chip.id));
		if (fresh.length) announcement = `Suggested task: ${fresh.map((chip) => chip.title).join(', ')}`;
	});
</script>

{#if source}
	<p class="started-from">
		Started from
		<a
			href={hrefFor(source.session_id)}
			data-sveltekit-reload
			onclick={(event) => onOpenChat(event, source!.session_id)}
			title={source.title || undefined}
		>{source.title || 'the chat it came from'}</a>
	</p>
{/if}

<div class="sr-only" role="status">{announcement}</div>

{#if chips.length}
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<section
		class="chip-tray"
		class:crowded
		aria-label="Suggested tasks"
		data-keeps-composer
		data-ui-state="task-chips"
		data-ui-chip-session={uiSessionTag(sessionId)}
		data-ui-chips={chips.map((chip) => (isStarting(chip) ? 'starting' : errors[chip.id] ? 'not-sent' : chip.state)).join(',')}
		onmousedown={keepFocus}
	>
		<ul bind:this={list}>
			{#each shown as chip (chip.id)}
				{@const open = openChip === chip.id}
				{@const trouble = problem(chip)}
				<li class="chip" class:leaving={busy[chip.id] === 'dismiss'} data-chip-id={chip.id}>
					<div class="chip-row">
						{#if chip.state === 'started'}
							<div class="about is-static">
								<span class="title" title={chip.title}>{chip.title}</span>
								<span class="meta"><span class="started">
									<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
									Started
								</span></span>
							</div>
							<div class="actions">
								{#if chip.started_session_id}
									<a
										class="open-chat"
										data-chip-main
										href={hrefFor(chip.started_session_id)}
										data-sveltekit-reload
										onclick={(event) => onOpenChat(event, chip.started_session_id!, chip)}
										aria-label={`Open chat: ${chip.title}`}
									>Open chat</a>
								{/if}
								<button type="button" class="dismiss" onclick={() => onClear(chip)} aria-label={`Clear ${chip.title}`} title="Clear">
									<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>
								</button>
							</div>
						{:else}
							<button
								type="button"
								class="about"
								aria-expanded={open}
								aria-controls={`chip-brief-${chip.id}`}
								onclick={() => (openChip = open ? null : chip.id)}
							>
								<span class="title-line">
									<span class="title" class:is-open={open} title={chip.title}>{chip.title}</span>
									<svg class="chev" class:is-open={open} width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
								</span>
								<span class="summary" class:is-open={open}>{chip.summary}</span>
								<span class="meta">
									<span class="model" title={chip.model}>{taskChipModel(chip)}</span>
									{#if chip.origin_label}<span class="origin" title={`From ${chip.origin_label}`}>From {chip.origin_label}</span>{/if}
								</span>
							</button>
							<div class="actions">
								{#if canRetry(chip)}
									<button
										type="button"
										class="start"
										data-chip-main
										aria-disabled={isStarting(chip) || !!busy[chip.id]}
										aria-label={`${actionLabel(chip)} ${chip.title}`}
										title="Starts in a separate chat"
										onclick={() => start(chip)}
									>
										{#if isStarting(chip)}<span class="spinner" aria-hidden="true"></span>{/if}
										{actionLabel(chip)}
									</button>
								{/if}
								{#if !isStarting(chip)}
									<button
										type="button"
										class="dismiss"
										data-chip-main={canRetry(chip) ? undefined : ''}
										aria-disabled={!!busy[chip.id]}
										aria-label={`Dismiss ${chip.title}`}
										title="Dismiss"
										onclick={() => { if (!busy[chip.id]) onDismiss(chip); }}
									>
										<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>
									</button>
								{/if}
							</div>
						{/if}
					</div>
					{#if trouble}
						<p class="trouble" role="alert">{trouble}</p>
					{/if}
					{#if chip.state !== 'started'}
						<div class="brief" id={`chip-brief-${chip.id}`} hidden={!open}>
							<p class="base">{baseLine(chip)}</p>
							<p class="prompt">{chip.prompt}</p>
						</div>
					{/if}
				</li>
			{/each}
		</ul>
		{#if hidden.length}
			<button type="button" class="more" aria-expanded={showAll} onclick={() => (showAll = !showAll)}>
				{showAll ? 'Show fewer' : moreLabel}
			</button>
		{/if}
	</section>
{/if}

<style>
	.chip-tray,
	.started-from {
		max-width: 48rem;
		margin: 0 auto 8px;
		padding: 0 0.75rem;
	}

	.chip-tray {
		animation: chip-tray-in 220ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
	}

	@media (min-width: 768px) {
		.chip-tray,
		.started-from {
			padding: 0 1.5rem;
		}
	}

	.started-from {
		overflow: hidden;
		color: var(--muted-foreground);
		font-size: 12px;
		line-height: 1.4;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.started-from a {
		color: var(--foreground);
		text-decoration: underline;
		text-decoration-color: color-mix(in oklch, var(--foreground) 35%, transparent);
		text-underline-offset: 3px;
	}

	ul {
		display: flex;
		flex-direction: column;
		gap: 2px;
		max-height: min(38dvh, 24rem);
		margin: 0;
		padding: 2px 0;
		overflow-y: auto;
		overscroll-behavior: contain;
		list-style: none;
		border-left: 2px solid color-mix(in oklch, var(--primary) 55%, transparent);
	}

	/* An open question is the owner's next job: the chips scroll so the chat keeps a few lines. */
	.crowded ul {
		max-height: min(18dvh, 8rem);
	}

	/* One scrolling row on a short screen: a small phone with a question open, or any phone on its side. */
	@media (max-height: 700px) {
		.crowded ul {
			max-height: 4rem;
		}
	}

	@media (max-height: 500px) {
		ul {
			max-height: 4rem;
		}
	}

	.chip {
		min-width: 0;
		padding: 0 0 0 8px;
		transition: opacity 180ms ease-out;
	}

	.chip.leaving {
		opacity: 0.5;
	}

	.chip-row {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}

	.about {
		display: flex;
		flex: 1 1 auto;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
		padding: 4px 6px;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--foreground);
		font: inherit;
		text-align: left;
	}

	button.about {
		cursor: pointer;
		transition: background-color 160ms ease-out;
	}

	button.about:hover {
		background: var(--scrim);
	}

	.title-line {
		display: flex;
		align-items: center;
		gap: 4px;
		min-width: 0;
	}

	.title {
		min-width: 0;
		overflow: hidden;
		color: var(--foreground);
		font-size: 13px;
		font-weight: 500;
		line-height: 1.35;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.summary {
		display: block;
		overflow: hidden;
		color: var(--muted-foreground);
		font-size: 12px;
		line-height: 1.35;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.title.is-open,
	.summary.is-open {
		overflow: visible;
		white-space: normal;
		overflow-wrap: anywhere;
	}

	.chev {
		flex-shrink: 0;
		color: var(--muted-foreground);
		transition: transform 160ms ease-out;
	}

	.chev.is-open {
		transform: rotate(180deg);
	}

	.meta {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		color: var(--muted-foreground);
		font-size: 11.5px;
		line-height: 1.4;
	}

	.model {
		flex-shrink: 0;
		max-width: 70%;
		overflow: hidden;
		color: var(--foreground);
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.origin {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.started {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		color: var(--success);
	}

	.actions {
		display: flex;
		flex-shrink: 0;
		align-items: center;
		gap: 4px;
	}

	.start,
	.open-chat {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		min-height: 28px;
		padding: 3px 12px;
		border-radius: 999px;
		font-size: 12px;
		font-weight: 500;
		line-height: 1.2;
		white-space: nowrap;
		cursor: pointer;
		text-decoration: none;
		transition: background-color 160ms ease-out, border-color 160ms ease-out, opacity 160ms ease-out;
	}

	.start {
		border: 1px solid transparent;
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.start:hover:not([aria-disabled='true']) {
		background: color-mix(in oklch, var(--primary) 88%, var(--foreground));
	}

	.start:active:not([aria-disabled='true']) {
		background: color-mix(in oklch, var(--primary) 78%, var(--foreground));
	}

	.start[aria-disabled='true'] {
		cursor: default;
		opacity: 0.7;
	}

	.open-chat {
		border: 1px solid color-mix(in oklch, var(--border) 85%, transparent);
		background: transparent;
		color: var(--foreground);
	}

	.open-chat:hover {
		background: var(--scrim);
	}

	.spinner {
		width: 10px;
		height: 10px;
		border: 1.5px solid currentColor;
		border-right-color: transparent;
		border-radius: 50%;
		animation: chip-spin 0.8s linear infinite;
	}

	.dismiss {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		border: 0;
		border-radius: 999px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 160ms ease-out, background-color 160ms ease-out;
	}

	.dismiss:hover:not([aria-disabled='true']) {
		background: var(--scrim);
		color: var(--foreground);
	}

	.dismiss[aria-disabled='true'] {
		cursor: default;
		opacity: 0.6;
	}

	.trouble {
		margin: 2px 6px 4px;
		color: var(--destructive);
		font-size: 12px;
		line-height: 1.4;
		overflow-wrap: anywhere;
	}

	.brief {
		margin: 2px 6px 6px;
		padding: 6px 8px;
		border-radius: 6px;
		background: var(--muted);
		font-size: 12px;
		line-height: 1.45;
	}

	.brief[hidden] {
		display: none;
	}

	.base {
		margin: 0 0 4px;
		color: var(--muted-foreground);
	}

	.prompt {
		max-height: 11rem;
		margin: 0;
		overflow-y: auto;
		overscroll-behavior: contain;
		color: var(--foreground);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}

	.more {
		margin: 2px 0 0 10px;
		padding: 3px 6px;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		font-size: 12px;
		cursor: pointer;
	}

	.more:hover {
		background: var(--scrim);
		color: var(--foreground);
	}

	.about:focus-visible,
	.start:focus-visible,
	.open-chat:focus-visible,
	.dismiss:focus-visible,
	.more:focus-visible,
	.started-from a:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	/* The list scrolls, so an outward ring would be clipped at its edges. */
	.about:focus-visible,
	.start:focus-visible,
	.open-chat:focus-visible,
	.dismiss:focus-visible {
		outline-offset: -2px;
	}

	/* Touch: 44px targets. A chip's two text lines are already that tall, so the row keeps its height. */
	@media (pointer: coarse) {
		.start,
		.open-chat {
			min-height: 44px;
			padding: 3px 16px;
		}

		.dismiss {
			width: 44px;
			height: 44px;
		}

		.more {
			min-height: 44px;
		}
	}

	@keyframes chip-tray-in {
		from { opacity: 0; transform: translateY(4px); }
		to { opacity: 1; transform: translateY(0); }
	}

	@keyframes chip-spin {
		to { transform: rotate(360deg); }
	}

	@media (prefers-reduced-motion: reduce) {
		.chip-tray,
		.spinner {
			animation: none;
		}

		.chip,
		.chev,
		.start,
		.open-chat,
		.dismiss,
		button.about {
			transition: none;
		}
	}
</style>
