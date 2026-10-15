<script lang="ts">
	/** Monitor output is plain text, never a PTY: no input, resize or stop channel. */
	import { tick } from 'svelte';
	import { Eye, RefreshCw, Copy, Check } from 'lucide-svelte';
	import { apiBase, withAuth, wsUrl } from '$lib/api/runtime';
	import { copyText } from '$lib/utils/clipboard';
	let { sessionId }: { sessionId: string } = $props();
	type Task = { id: string; subject: string; status: string; exit_code: number | null };
	let tasks = $state<Task[]>([]);
	let selected = $state('');
	let task = $state<Task | null>(null);
	let lines = $state<string[]>([]);
	let loading = $state(true);
	let error = $state('');
	let streamError = $state('');
	let connection = $state('');
	let truncated = $state(false);
	let refresh = $state(0);
	let retry = $state(0);
	let follow = $state(true);
	let copied = $state(false);
	let output = $state<HTMLPreElement>();

	$effect(() => {
		const session = sessionId;
		void refresh;
		const controller = new AbortController();
		loading = true;
		error = '';
		void (async () => {
			try {
				const res = await fetch(`${apiBase()}/terminals/monitor?session_id=${encodeURIComponent(session)}`, withAuth({ signal: controller.signal }));
				const data = await res.json();
				if (!res.ok) throw new Error(data.detail || 'Unable to load background commands.');
				if (controller.signal.aborted) return;
				tasks = data.tasks;
				if (!tasks.some((entry) => entry.id === selected)) selected = tasks[0]?.id ?? '';
			} catch (cause) {
				if (!controller.signal.aborted) { tasks = []; selected = ''; error = cause instanceof Error ? cause.message : 'Could not reach the worker.'; }
			} finally { if (!controller.signal.aborted) loading = false; }
		})();
		return () => controller.abort();
	});

	$effect(() => {
		const id = selected;
		const session = sessionId;
		void retry;
		lines = []; task = null; streamError = ''; copied = false; truncated = false;
		if (!id) { connection = ''; return; }
		connection = 'Connecting…';
		let disposed = false;
		const socket = new WebSocket(wsUrl(`/api/v1/ws/terminals/monitor/${encodeURIComponent(id)}`, { session_id: session }));
		socket.onmessage = (event) => {
			if (disposed) return;
			try {
				const frame = JSON.parse(event.data);
				if (frame.type !== 'output' || frame.readonly !== true || !Array.isArray(frame.lines)) return;
				lines = frame.lines.filter((line: unknown) => typeof line === 'string');
				task = frame.task;
				truncated = frame.truncated === true;
				connection = frame.finished ? 'Output complete' : 'Watching';
			} catch { streamError = 'The worker sent unreadable output. Reconnect to try again.'; }
		};
		socket.onclose = (event) => {
			if (disposed) return;
			connection = event.code === 1000 ? 'Output complete' : 'Disconnected';
			if (event.code !== 1000) streamError = event.code === 4001 ? 'Only the owner can watch this command. Sign in again.' : 'The output connection closed. Reconnect to continue watching.';
		};
		socket.onerror = () => { if (!disposed) streamError = 'Could not connect to this command. Reconnect to try again.'; };
		return () => { disposed = true; socket.close(); };
	});

	$effect(() => {
		void lines;
		if (follow) void tick().then(() => { if (output) output.scrollTop = output.scrollHeight; });
	});
	async function copy() {
		try { await copyText(lines.join('\n')); copied = true; }
		catch { streamError = 'Could not copy output. Select the text and copy it manually.'; }
	}
</script>

<section class="monitor" aria-label="Background command output">
	<div class="readonly"><Eye size={16} aria-hidden="true" /><strong>Read-only</strong><span>No input or process control</span></div>
	<div class="picker">
		<label for="monitor-task">Background command</label>
		<div class="choices">
			<select id="monitor-task" bind:value={selected} disabled={loading || !tasks.length}>
				{#if !tasks.length}<option value="">{loading ? 'Loading commands…' : 'No commands'}</option>{/if}
				{#each tasks as entry (entry.id)}<option value={entry.id}>{entry.subject} · {entry.status}</option>{/each}
			</select>
			<button aria-label="Refresh commands" title="Refresh commands" disabled={loading} onclick={() => refresh++}><RefreshCw size={16} /></button>
		</div>
	</div>
	{#if error}
		<div class="message" role="alert"><p>{error}</p><button onclick={() => refresh++}>Try again</button></div>
	{:else if loading}
		<p class="message" role="status">Loading background commands…</p>
	{:else if !tasks.length}
		<div class="message"><Eye size={24} aria-hidden="true" /><strong>No background commands in this chat</strong><p>Commands started with Monitor appear here, including retained output from finished commands.</p></div>
	{:else}
		<div class="controls">
			<span role="status">{task?.status ?? connection}{task?.exit_code != null ? ` · exit ${task.exit_code}` : ''}</span>
			<label><input type="checkbox" bind:checked={follow} /> Follow output</label>
			<button aria-label="Copy command output" title="Copy command output" disabled={!lines.length} onclick={copy}>{#if copied}<Check size={16} />{:else}<Copy size={16} />{/if}</button>
		</div>
		{#if streamError}<div class="stream-error" role="alert"><span>{streamError}</span><button onclick={() => retry++}>Reconnect</button></div>{/if}
		<!-- Keyboard focus is required to scroll and copy this read-only output. -->
		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<pre bind:this={output} role="region" tabindex="0" aria-label="Read-only command output">{lines.length ? lines.join('\n') : connection === 'Connecting…' ? 'Connecting to output…' : 'No output yet.'}</pre>
		<p class="footnote">{connection} · {truncated ? 'Earlier output was trimmed (256 KiB limit).' : 'Retained output only.'} Closing this view does not stop the command.</p>
	{/if}
</section>

<style>
	.monitor { display: flex; flex-direction: column; flex: 1; min-height: 0; min-width: 0; color: var(--foreground); }
	.readonly { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 12px 16px; background: var(--muted); border-bottom: 1px solid var(--border); font-size: .75rem; }
	.readonly strong { letter-spacing: .04em; text-transform: uppercase; }
	.readonly span { color: var(--muted-foreground); }
	.picker { padding: 12px 16px; }
	.picker > label { display: block; margin-bottom: 6px; color: var(--muted-foreground); font-size: .75rem; }
	.choices { display: flex; gap: 8px; }
	select { flex: 1; min-width: 0; padding: 8px; }
	select, button { background: var(--card); color: var(--foreground); border: 1px solid var(--border); border-radius: 6px; font: inherit; font-size: .8125rem; }
	button { min-width: 44px; min-height: 44px; padding: 8px; display: inline-flex; justify-content: center; align-items: center; cursor: pointer; transition: background .2s ease; }
	button:hover { background: var(--hover-overlay); }
	button:active { transform: translateY(1px); }
	button:disabled, select:disabled { opacity: .5; cursor: default; }
	button:focus-visible, select:focus-visible, pre:focus-visible, input:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.controls { padding: 0 16px 8px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: .75rem; }
	.controls > span { flex: 1; }
	.controls label { display: flex; align-items: center; gap: 6px; min-height: 44px; }
	input { accent-color: var(--primary); width: 16px; height: 16px; }
	pre { flex: 1; min-height: 120px; margin: 0; padding: 16px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--font-mono); font-size: .8125rem; line-height: 1.6; background: var(--background); border-block: 1px solid var(--border); user-select: text; }
	.message { padding: 24px; display: flex; flex-direction: column; align-items: flex-start; gap: 12px; font-size: .8125rem; color: var(--muted-foreground); }
	.message p { margin: 0; line-height: 1.6; }
	.stream-error { padding: 12px 16px; display: flex; align-items: center; gap: 12px; font-size: .8125rem; }
	.footnote { margin: 0; padding: 8px 16px; font-size: .6875rem; line-height: 1.5; color: var(--muted-foreground); }
	@media (prefers-reduced-motion: reduce) { button { transition: none; } }
</style>
