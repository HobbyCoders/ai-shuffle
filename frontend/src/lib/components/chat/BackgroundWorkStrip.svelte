<script lang="ts">
	/**
	 * BackgroundWorkStrip — the assistant's running jobs, beside its composer.
	 *
	 * Up to three plain lines ("Fixing the sidebar bug, 6 min"). A line opens
	 * the job's chat when it has one and carries one Stop action. More than three
	 * collapse to "and N more". Renders nothing while no work runs.
	 */
	import { onDestroy } from 'svelte';
	import { formatJobAge, type BackgroundJob } from '$lib/utils/background-work';

	interface Props {
		jobs: BackgroundJob[];
		onOpen?: (job: BackgroundJob) => void;
		/** Resolves false when the stop did not take, so the line can be retried. */
		onStop?: (job: BackgroundJob) => boolean | void | Promise<boolean | void>;
	}

	let { jobs, onOpen, onStop }: Props = $props();

	const VISIBLE = 3;
	let showAll = $state(false);
	const shown = $derived(showAll ? jobs : jobs.slice(0, VISIBLE));
	const hidden = $derived(Math.max(0, jobs.length - VISIBLE));
	$effect(() => { if (jobs.length <= VISIBLE) showAll = false; });

	let stopping = $state<Record<string, boolean>>({});
	async function stop(job: BackgroundJob) {
		if (stopping[job.key]) return;
		stopping = { ...stopping, [job.key]: true };
		let ok = false;
		try { ok = (await onStop?.(job)) !== false; } catch { ok = false; }
		if (!ok) {
			const next = { ...stopping };
			delete next[job.key];
			stopping = next;
		}
	}

	// Minutes are the finest unit shown, so a slow clock is enough.
	let now = $state(Date.now());
	let timer: ReturnType<typeof setInterval> | null = null;
	$effect(() => {
		if (jobs.length && timer === null) {
			now = Date.now();
			timer = setInterval(() => { now = Date.now(); }, 15_000);
		} else if (!jobs.length && timer !== null) {
			clearInterval(timer);
			timer = null;
		}
	});
	onDestroy(() => { if (timer !== null) clearInterval(timer); });

	function line(job: BackgroundJob): string {
		const age = formatJobAge(job.startedAt, now);
		return age ? `${job.label}, ${age}` : job.label;
	}
</script>

{#if jobs.length}
	<section class="work-strip" aria-label="Background work">
		<ul>
			{#each shown as job (job.key)}
				<li class:stopping={stopping[job.key]}>
					<span class="beat" aria-hidden="true"></span>
					{#if job.sessionId && onOpen}
						<button type="button" class="job" onclick={() => onOpen?.(job)} title={line(job)} aria-label={line(job)}>
							<span class="what">{job.label}</span>{#if formatJobAge(job.startedAt, now)}<span class="age">, {formatJobAge(job.startedAt, now)}</span>{/if}
						</button>
					{:else}
						<span class="job static" title={line(job)}>
							<span class="what">{job.label}</span>{#if formatJobAge(job.startedAt, now)}<span class="age">, {formatJobAge(job.startedAt, now)}</span>{/if}
						</span>
					{/if}
					{#if onStop}
						<button
							type="button"
							class="stop"
							disabled={stopping[job.key]}
							onclick={() => stop(job)}
							aria-label={`Stop ${job.label}`}
							title="Stop"
						>
							<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><rect x="1" y="1" width="8" height="8" rx="1.5" fill="currentColor" /></svg>
							<span class="stop-word">{stopping[job.key] ? 'Stopping' : 'Stop'}</span>
						</button>
					{/if}
				</li>
			{/each}
		</ul>
		{#if hidden > 0}
			<button type="button" class="more" aria-expanded={showAll} onclick={() => (showAll = !showAll)}>
				{showAll ? 'Show fewer' : `and ${hidden} more`}
			</button>
		{/if}
	</section>
{/if}

<style>
	.work-strip {
		max-width: 48rem;
		margin: 0 auto 8px;
		padding: 0 0.75rem;
		animation: strip-in 220ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
	}

	@media (min-width: 768px) {
		.work-strip {
			padding: 0 1.5rem;
		}
	}

	ul {
		display: flex;
		flex-direction: column;
		margin: 0;
		padding: 2px 0;
		list-style: none;
		border-left: 2px solid color-mix(in oklch, var(--primary) 55%, transparent);
	}

	li {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		padding: 0 0 0 10px;
		transition: opacity 180ms ease-out;
	}

	li.stopping {
		opacity: 0.5;
	}

	.beat {
		flex-shrink: 0;
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--primary);
		animation: beat 1.6s ease-in-out infinite;
	}

	.job {
		display: flex;
		flex: 1 1 auto;
		min-width: 0;
		min-height: 30px;
		padding: 4px 6px;
		margin-left: -6px;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--foreground);
		font-size: 13px;
		line-height: 1.4;
		text-align: left;
		align-items: center;
	}

	button.job {
		cursor: pointer;
		transition: background-color 160ms ease-out;
	}

	button.job:hover {
		background: var(--scrim);
	}

	button.job:hover .what {
		text-decoration: underline;
		text-decoration-color: color-mix(in oklch, var(--foreground) 35%, transparent);
		text-underline-offset: 3px;
	}

	.what {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.age {
		flex-shrink: 0;
		white-space: nowrap;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
	}

	.stop {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		flex-shrink: 0;
		min-height: 28px;
		padding: 3px 9px;
		border: 1px solid color-mix(in oklch, var(--border) 85%, transparent);
		border-radius: 999px;
		background: transparent;
		color: var(--muted-foreground);
		font-size: 11.5px;
		cursor: pointer;
		transition: color 160ms ease-out, border-color 160ms ease-out, background-color 160ms ease-out;
	}

	.stop:hover:not(:disabled) {
		color: var(--destructive);
		border-color: color-mix(in oklch, var(--destructive) 50%, transparent);
		background: color-mix(in oklch, var(--destructive) 8%, transparent);
	}

	.stop:active:not(:disabled) {
		background: color-mix(in oklch, var(--destructive) 14%, transparent);
	}

	.stop:disabled {
		cursor: default;
		opacity: 0.7;
	}

	.more {
		margin: 2px 0 0 12px;
		padding: 3px 6px;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		font-size: 12px;
		cursor: pointer;
	}

	.more:hover {
		color: var(--foreground);
		background: var(--scrim);
	}

	.job:focus-visible,
	.stop:focus-visible,
	.more:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	/* Touch: 44px targets without inflating the compact pills. */
	@media (pointer: coarse) {
		.job {
			min-height: 44px;
		}

		.stop,
		.more {
			position: relative;
		}

		.stop::after,
		.more::after {
			content: '';
			position: absolute;
			inset: -8px -4px;
		}
	}

	@keyframes beat {
		0%, 100% { opacity: 0.35; }
		50% { opacity: 1; }
	}

	@keyframes strip-in {
		from { opacity: 0; transform: translateY(4px); }
		to { opacity: 1; transform: translateY(0); }
	}

	@media (prefers-reduced-motion: reduce) {
		.work-strip, .beat { animation: none; }
	}
</style>
