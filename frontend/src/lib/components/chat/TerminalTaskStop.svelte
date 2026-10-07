<script lang="ts">
	/** Owner Stop for scheduler work retained after an agent task finished (#2039). */
	import { getSessionTaskStopState, stopSessionTask, type TaskStopState } from '$lib/api/sessions';

	let { parentSessionId, taskId, canControl = false }: {
		parentSessionId: string;
		taskId: string;
		canControl?: boolean;
	} = $props();

	let visible = $state(false);
	let snapshot = $state<TaskStopState | null>(null);
	let stopping = $state(false);
	let error = $state('');
	let refresh = $state(0);
	let epoch = 0;
	let snapshotRevision = 0;

	// The transcript keeps historical cards mounted. Only visible cards poll,
	// with one request at a time, and never while the document is hidden.
	function observe(node: HTMLElement) {
		const observer = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; });
		observer.observe(node);
		return { destroy: () => observer.disconnect() };
	}

	$effect(() => {
		const parent = parentSessionId;
		const task = taskId;
		const allowed = canControl;
		const inView = visible;
		const revision = refresh;
		void revision;
		const generation = ++epoch;
		const abort = new AbortController();
		let timer: ReturnType<typeof setTimeout> | undefined;
		let requestGeneration = 0;
		let activeRequest: AbortController | undefined;
		snapshot = null;
		stopping = false;
		error = '';

		async function check() {
			if (activeRequest || abort.signal.aborted || document.hidden) return;
			const request = new AbortController();
			activeRequest = request;
			const requestId = ++requestGeneration;
			const sampledRevision = snapshotRevision;
			try {
				const state = await getSessionTaskStopState(parent, task, { signal: request.signal });
				if (!abort.signal.aborted && !request.signal.aborted && requestId === requestGeneration
					&& epoch === generation && sampledRevision === snapshotRevision && !document.hidden) snapshot = state;
			} catch {
				// Fail closed: lack of a fresh owner capability is not permission to Stop.
				if (!abort.signal.aborted && !request.signal.aborted && requestId === requestGeneration
					&& epoch === generation) snapshot = null;
			} finally {
				if (requestId === requestGeneration) {
					activeRequest = undefined;
					if (!abort.signal.aborted && !document.hidden) timer = setTimeout(check, 10000);
				}
			}
		}

		function visibilityChanged() {
			clearTimeout(timer);
			// Hide→show invalidates the old sample even if transport ignores abort.
			requestGeneration += 1;
			activeRequest?.abort();
			activeRequest = undefined;
			snapshot = null;
			if (!document.hidden) void check();
		}
		if (allowed && inView && parent && task) {
			void check();
			document.addEventListener('visibilitychange', visibilityChanged);
		}
		return () => {
			abort.abort();
			activeRequest?.abort();
			clearTimeout(timer);
			document.removeEventListener('visibilitychange', visibilityChanged);
		};
	});

	async function stop() {
		const token = snapshot?.eligible ? snapshot.turn_id : undefined;
		if (!canControl || !token || stopping) return;
		const parent = parentSessionId;
		const task = taskId;
		const generation = epoch;
		stopping = true;
		error = '';
		try {
			// Send the displayed snapshot, not a new turn: server rechecks freshness
			// under admission lock, so a later owner continuation wins.
			const result = await stopSessionTask(parent, task, token);
			if (epoch !== generation) return;
			if (!result.ok) {
				error = "Couldn't stop pending work. Check again and retry.";
			} else {
				// A GET started before Stop succeeded cannot revive its old action.
				snapshotRevision += 1;
				snapshot = null;
			}
		} catch {
			if (epoch === generation) error = "Couldn't stop pending work. Check again and retry.";
		} finally {
			if (epoch === generation) stopping = false;
		}
	}
</script>

<div class="pending-stop" use:observe>
	{#if canControl && snapshot?.eligible && snapshot.turn_id}
		<div class="pending-line">
			<span>Pending activity after this task finished</span>
			<button type="button" onclick={stop} disabled={stopping} aria-busy={stopping}>
				<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
				{stopping ? 'Stopping…' : 'Stop pending work'}
			</button>
		</div>
	{/if}
	{#if error}
		<div class="stop-error" role="alert">
			<span>{error}</span>
			<button type="button" class="retry" onclick={() => { refresh += 1; }}>Check again</button>
		</div>
	{/if}
</div>

<style>
	.pending-stop { min-height: 1px; }
	.pending-line, .stop-error { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; margin: 4px 8px 8px 31px; font-size: 11px; line-height: 1.5; color: var(--muted-foreground); }
	button { display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-height: 28px; padding: 4px 9px; border: 1px solid color-mix(in oklch, var(--destructive) 35%, transparent); border-radius: 6px; background: var(--status-error-bg); color: var(--destructive); font: inherit; font-weight: 600; cursor: pointer; transition: background-color .2s ease-out, transform .2s ease-out; }
	button:hover:not(:disabled) { background: color-mix(in oklch, var(--destructive) 18%, transparent); }
	button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	button:active:not(:disabled) { transform: translateY(1px); }
	button:disabled { opacity: .6; cursor: wait; }
	.stop-error { color: var(--destructive); }
	.retry { background: transparent; }
	@media (max-width: 639px) { button { min-height: 44px; } .pending-line { align-items: flex-start; } }
	@media (prefers-reduced-motion: reduce) { button { transition: none; } }
</style>
