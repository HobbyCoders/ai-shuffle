<script lang="ts">
	/**
	 * Voice test page — standalone dev demo for the Pipecat pipeline.
	 * Not linked from main nav; visit /voice-test directly.
	 */
	import { onDestroy, onMount } from 'svelte';
	import {
		RealtimeVoiceSession,
		fetchVoiceCapabilities,
		type VoiceCapabilities,
		type VoiceState
	} from '$lib/services/realtimeVoice';

	let session: RealtimeVoiceSession | null = null;
	let state = $state<VoiceState>('idle');
	let transcripts = $state<Array<{ role: 'user' | 'assistant'; text: string; at: number }>>([]);
	let errorMsg = $state('');
	let capabilities = $state<VoiceCapabilities | null>(null);
	let muted = $state(false);
	let metrics = $state<Record<string, number>>({});

	const startedAt = $state({ value: 0 });
	let elapsed = $state(0);
	let tickTimer: ReturnType<typeof setInterval> | null = null;

	onMount(async () => {
		try {
			capabilities = await fetchVoiceCapabilities();
		} catch (e) {
			errorMsg = e instanceof Error ? e.message : String(e);
		}
	});

	onDestroy(() => {
		stopTicker();
		session?.disconnect();
	});

	function startTicker() {
		startedAt.value = Date.now();
		tickTimer = setInterval(() => {
			elapsed = Math.round((Date.now() - startedAt.value) / 1000);
		}, 500);
	}

	function stopTicker() {
		if (tickTimer) clearInterval(tickTimer);
		tickTimer = null;
	}

	async function connect() {
		errorMsg = '';
		transcripts = [];
		metrics = {};
		session = new RealtimeVoiceSession(
			{},
			{
				onState: (s) => {
					state = s;
					if (s === 'connected' && !tickTimer) startTicker();
					if (s === 'disconnected' || s === 'error') stopTicker();
				},
				onTranscript: (text, isFinal, role) => {
					if (!isFinal) return;
					transcripts = [...transcripts, { role, text, at: Date.now() }];
				},
				onError: (e) => {
					errorMsg = e.message;
				},
				onMetric: (name, value) => {
					metrics = { ...metrics, [name]: value };
				}
			}
		);
		try {
			await session.connect();
		} catch (e) {
			errorMsg = e instanceof Error ? e.message : String(e);
		}
	}

	async function disconnect() {
		await session?.disconnect();
		session = null;
	}

	function toggleMute() {
		muted = !muted;
		session?.setMuted(muted);
	}

	let connectDisabled = $derived(
		state === 'connecting' || state === 'connected' || state === 'listening' || state === 'speaking'
	);
</script>

<div class="wrap">
	<header>
		<h1>Realtime Voice — Test</h1>
		<p class="sub">
			Standalone demo of the Pipecat pipeline. Uses Deepgram + Cartesia by default (configure keys
			in Settings → Integrations).
		</p>
	</header>

	{#if capabilities && !capabilities.pipecat_available}
		<div class="banner error">
			Pipecat not installed on backend. Run <code>pip install -r requirements.txt</code> and restart.
			{#if capabilities.error}<br /><code>{capabilities.error}</code>{/if}
		</div>
	{/if}

	{#if capabilities}
		<section class="capabilities">
			<h2>Providers</h2>
			<div class="cap-grid">
				<div>
					<h3>STT</h3>
					<ul>
						{#each capabilities.stt_providers as p}
							<li class:available={p.available}>
								{p.name}
								<span class="tag">{p.streaming ? 'streaming' : 'batch'}</span>
								{#if !p.available}<span class="missing">key missing</span>{/if}
							</li>
						{/each}
					</ul>
				</div>
				<div>
					<h3>TTS</h3>
					<ul>
						{#each capabilities.tts_providers as p}
							<li class:available={p.available}>
								{p.name}
								<span class="tag">{p.streaming ? 'streaming' : 'batch'}</span>
								{#if !p.available}<span class="missing">key missing</span>{/if}
							</li>
						{/each}
					</ul>
				</div>
			</div>
		</section>
	{/if}

	<section class="controls">
		<button class="btn primary" disabled={connectDisabled} onclick={connect}>
			{state === 'connecting' ? 'Connecting…' : 'Start call'}
		</button>
		<button
			class="btn"
			disabled={state !== 'connected' && state !== 'listening' && state !== 'speaking'}
			onclick={toggleMute}
		>
			{muted ? 'Unmute' : 'Mute'}
		</button>
		<button
			class="btn destructive"
			disabled={state === 'idle' || state === 'disconnected' || state === 'error'}
			onclick={disconnect}
		>
			Hang up
		</button>
		<div class="status">
			<span class="state-dot state-{state}"></span>
			<span>{state}</span>
			{#if elapsed > 0}
				<span class="elapsed">{Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, '0')}</span>
			{/if}
		</div>
	</section>

	{#if errorMsg}
		<div class="banner error">{errorMsg}</div>
	{/if}

	<section class="transcript">
		<h2>Transcript</h2>
		{#if transcripts.length === 0}
			<p class="muted">Nothing yet — speak to start.</p>
		{:else}
			<ol>
				{#each transcripts as t}
					<li class={t.role}>
						<span class="role">{t.role}</span>
						<span class="text">{t.text}</span>
					</li>
				{/each}
			</ol>
		{/if}
	</section>

	{#if Object.keys(metrics).length > 0}
		<section class="metrics">
			<h2>Metrics</h2>
			<dl>
				{#each Object.entries(metrics) as [k, v]}
					<dt>{k}</dt>
					<dd>{v.toFixed(1)} ms</dd>
				{/each}
			</dl>
		</section>
	{/if}
</div>

<style>
	.wrap {
		max-width: 820px;
		margin: 0 auto;
		padding: 32px 20px 80px;
		font-family: system-ui, sans-serif;
		color: var(--foreground, #1a1a1a);
	}
	header h1 {
		margin: 0 0 6px;
		font-size: 1.5rem;
		font-weight: 700;
	}
	.sub {
		margin: 0 0 24px;
		color: var(--muted-foreground, #666);
		font-size: 0.875rem;
	}
	.banner {
		padding: 10px 14px;
		border-radius: 8px;
		font-size: 0.875rem;
		margin: 12px 0;
	}
	.banner.error {
		background: color-mix(in oklch, var(--destructive, #ef4444) 10%, transparent);
		color: var(--destructive, #ef4444);
		border: 1px solid color-mix(in oklch, var(--destructive, #ef4444) 30%, transparent);
	}
	.capabilities {
		margin: 24px 0;
	}
	.capabilities h2,
	.transcript h2,
	.metrics h2 {
		font-size: 0.9375rem;
		font-weight: 600;
		margin: 0 0 10px;
	}
	.cap-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 24px;
	}
	.cap-grid h3 {
		font-size: 0.8125rem;
		margin: 0 0 6px;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground, #888);
	}
	.cap-grid ul {
		list-style: none;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.cap-grid li {
		font-size: 0.875rem;
		color: var(--muted-foreground, #888);
	}
	.cap-grid li.available {
		color: var(--foreground, #1a1a1a);
	}
	.cap-grid .tag {
		font-size: 0.6875rem;
		background: var(--secondary, #eee);
		padding: 1px 6px;
		border-radius: 4px;
		margin-left: 6px;
	}
	.cap-grid .missing {
		font-size: 0.6875rem;
		color: var(--destructive, #ef4444);
		margin-left: 6px;
	}
	.controls {
		display: flex;
		gap: 10px;
		align-items: center;
		flex-wrap: wrap;
		margin: 24px 0;
	}
	.btn {
		padding: 8px 16px;
		border: 1px solid var(--border, #ddd);
		background: var(--card, #fff);
		color: var(--foreground, #1a1a1a);
		border-radius: 8px;
		cursor: pointer;
		font-size: 0.875rem;
		font-weight: 500;
	}
	.btn:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}
	.btn.primary {
		background: var(--primary, #000);
		color: var(--primary-foreground, #fff);
		border-color: transparent;
	}
	.btn.destructive {
		background: var(--destructive, #ef4444);
		color: #fff;
		border-color: transparent;
	}
	.status {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.875rem;
		color: var(--muted-foreground, #888);
		margin-left: auto;
	}
	.state-dot {
		width: 10px;
		height: 10px;
		border-radius: 50%;
		background: var(--muted-foreground, #888);
	}
	.state-dot.state-connected,
	.state-dot.state-listening {
		background: #22c55e;
	}
	.state-dot.state-connecting,
	.state-dot.state-reconnecting {
		background: #f59e0b;
	}
	.state-dot.state-speaking {
		background: #3b82f6;
	}
	.state-dot.state-error {
		background: #ef4444;
	}
	.elapsed {
		font-variant-numeric: tabular-nums;
	}
	.transcript ol {
		list-style: none;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.transcript li {
		display: flex;
		gap: 10px;
		font-size: 0.875rem;
	}
	.transcript .role {
		min-width: 72px;
		color: var(--muted-foreground, #888);
		text-transform: uppercase;
		font-size: 0.6875rem;
		letter-spacing: 0.05em;
		padding-top: 2px;
	}
	.transcript li.assistant .role {
		color: var(--primary, #2563eb);
	}
	.muted {
		color: var(--muted-foreground, #888);
		font-size: 0.875rem;
	}
	.metrics dl {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 4px 16px;
		font-size: 0.875rem;
	}
	.metrics dt {
		color: var(--muted-foreground, #888);
	}
</style>
