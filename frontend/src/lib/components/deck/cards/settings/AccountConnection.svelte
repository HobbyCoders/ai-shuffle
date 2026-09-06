<script lang="ts">
	import { onMount } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { Smartphone, Check, Copy } from 'lucide-svelte';
	type Connection = { status: string; code: string | null; qr_svg: string | null; error: string | null };
	let connection = $state<Connection | null>(null);
	let name = $state('My worker');
	let busy = $state(false);
	let error = $state('');
	let copied = $state(false);
	let stopped = false;
	let poll: ReturnType<typeof setTimeout>;
	async function refresh() {
		try { connection = await api.get<Connection>('/mobile/hub/connection'); }
		catch (e) { error = getErrorMessage(e); }
		if (!stopped) poll = setTimeout(refresh, 4000);
	}
	onMount(() => {
		void refresh();
		return () => { stopped = true; clearTimeout(poll); };
	});
	async function connect() {
		busy = true; error = ''; copied = false;
		try { connection = await api.post<Connection>('/mobile/hub/connection', { name: name.trim() }); }
		catch (e) { error = getErrorMessage(e); }
		finally { busy = false; }
	}
	async function copyCode() {
		try { await navigator.clipboard.writeText(connection?.code ?? ''); copied = true; }
		catch { error = 'Select the code to copy it.'; }
	}
</script>

<section class="account-connection" aria-label="Account connection">
	<div class="intro"><Smartphone size={20} /><div>
		<h4>Use this worker on your phone</h4>
		<p>Connect it to your Shuffle account to reach it wherever you are.</p>
	</div></div>
	{#if connection?.status === 'connected'}
		<p class="connected" role="status"><Check size={16} /> Connected to your account</p>
		<p>Open Shuffle on your phone and choose this worker. Remote access reconnects automatically when Shuffle starts.</p>
	{:else if connection?.code}
		<p>In the Shuffle app, sign in, choose <strong>Add worker</strong>, then scan this code.</p>
		{#if connection.qr_svg}<div class="qr" aria-label="Worker connection QR code">{@html connection.qr_svg}</div>{/if}
		<div class="code"><code>{connection.code.match(/.{1,4}/g)?.join(' ')}</code><button class="btn" onclick={copyCode} aria-label="Copy connection code"><Copy size={14} />{copied ? 'Copied' : 'Copy'}</button></div>
		<p class="hint">Or enter the code in Add worker. It expires after ten minutes. Approve only a worker you own.</p>
	{:else if connection?.status === 'connecting' || connection?.status === 'reconnecting'}
		<p role="status">Connecting your worker… Shuffle will retry automatically.</p>
	{:else}
		<label>Worker name<input class="input" bind:value={name} maxlength={80} placeholder="My worker" /></label>
		<button class="btn btn-primary" disabled={busy || !name.trim()} onclick={connect}>{busy ? 'Preparing…' : 'Connect to my account'}</button>
	{/if}
	{#if error || connection?.error}<p class="error" role="alert">{error || connection?.error}</p>{/if}
</section>

<style>
	.account-connection { margin: 12px 16px 20px; padding: 20px; border: 1px solid var(--border-color, #454550); border-radius: 16px; }
	.intro { display: flex; align-items: flex-start; gap: 12px; }
	h4 { margin: 0 0 6px; font-size: 15px; }
	p { font-size: 13px; line-height: 1.6; margin: 6px 0 14px; }
	label { display: block; font-size: 12px; margin: 12px 0; }
	input { display: block; width: min(100%, 320px); margin-top: 6px; }
	.qr { width: 200px; max-width: 100%; padding: 12px; background: white; border-radius: 12px; margin: 16px 0; }
	.qr :global(svg) { display: block; width: 100%; height: auto; }
	.code, .connected { display: flex; align-items: center; gap: 12px; }
	.code code { font-size: 19px; letter-spacing: .08em; user-select: all; }
	.hint { opacity: .7; margin-top: 12px; }
	.error { color: var(--color-danger, #ed7777); }
</style>
