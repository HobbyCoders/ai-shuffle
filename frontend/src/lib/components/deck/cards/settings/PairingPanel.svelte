<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { Check, Copy, Link2, MonitorSmartphone, Plus, QrCode, ShieldCheck, Trash2, X } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		createPairingGrant,
		listPairedClients,
		listPairingEndpoints,
		revokePairedClient,
		revokePairingGrant,
		type CreatedPairingGrant,
		type PairedClient,
		type PairingEndpoint
	} from '$lib/api/pairing';

	let endpoints = $state<PairingEndpoint[]>([]);
	let clients = $state<PairedClient[]>([]);
	let selectedEndpoint = $state('');
	let activeGrant = $state<CreatedPairingGrant | null>(null);
	let loading = $state(true);
	let creating = $state(false);
	let error = $state('');
	let copied = $state<'code' | 'link' | null>(null);
	let revokeClientId = $state<string | null>(null);
	let now = $state(Date.now());
	let timer: ReturnType<typeof setInterval> | null = null;

	const secondsLeft = $derived(
		activeGrant ? Math.max(0, Math.ceil((new Date(activeGrant.grant.expires_at).getTime() - now) / 1000)) : 0
	);

	onMount(async () => {
		timer = setInterval(() => {
			now = Date.now();
			if (activeGrant && secondsLeft <= 0) activeGrant = null;
		}, 1000);
		await load();
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	async function load() {
		loading = true;
		error = '';
		try {
			[endpoints, clients] = await Promise.all([listPairingEndpoints(), listPairedClients()]);
			selectedEndpoint = endpoints.find((endpoint) => endpoint.qr_eligible)?.url ?? endpoints[0]?.url ?? '';
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to load device pairing');
		} finally {
			loading = false;
		}
	}

	async function create(purpose: 'controller' | 'runner') {
		creating = true;
		error = '';
		try {
			const endpoint = endpoints.find((item) => item.url === selectedEndpoint);
			activeGrant = await createPairingGrant(
				purpose === 'controller' && endpoint?.qr_eligible ? endpoint.url : null,
				undefined,
				purpose
			);
			now = Date.now();
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to create pairing code');
		} finally {
			creating = false;
		}
	}

	async function cancelGrant() {
		if (!activeGrant) return;
		const grant = activeGrant;
		activeGrant = null;
		try {
			await revokePairingGrant(grant.grant.id);
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to revoke pairing code');
		}
	}

	async function copy(value: string, kind: 'code' | 'link') {
		try {
			await navigator.clipboard.writeText(value);
			copied = kind;
			setTimeout(() => { if (copied === kind) copied = null; }, 1800);
		} catch {
			error = 'Clipboard access is unavailable. Select and copy the value manually.';
		}
	}

	async function revokeClient(clientId: string) {
		error = '';
		try {
			await revokePairedClient(clientId);
			clients = clients.filter((client) => client.id !== clientId);
			revokeClientId = null;
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to revoke paired device');
		}
	}

	function relTime(value: string | null): string {
		if (!value) return 'never';
		const diff = Date.now() - new Date(value).getTime();
		if (diff < 60_000) return 'just now';
		if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
		if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
		return `${Math.floor(diff / 86_400_000)}d ago`;
	}
</script>

<section class="pair-panel">
	<div class="pair-head">
		<div>
			<h4><Link2 size={15} /> Pair a controller</h4>
			<p>Connect a phone, tablet, or browser to this worker. Codes work once and expire after five minutes.</p>
		</div>
		<span class="count">{clients.length} paired</span>
	</div>

	{#if loading}
		<p class="muted">Loading pairing…</p>
	{:else if activeGrant}
		<div class="grant">
			<div class="grant-code-side">
				<div class="grant-label">{activeGrant.grant.purpose === 'runner' ? 'Runner enrollment code' : 'One-time code'}</div>
				<div class="code-line">
					<code>{activeGrant.display_code}</code>
					<button class="icon" title="Copy code" onclick={() => copy(activeGrant!.display_code, 'code')}>
						{#if copied === 'code'}<Check size={14} />{:else}<Copy size={14} />{/if}
					</button>
				</div>
				<p>Expires in {Math.floor(secondsLeft / 60)}:{String(secondsLeft % 60).padStart(2, '0')}</p>
				{#if activeGrant.grant.purpose === 'runner'}
					<p class="runner-help">On the configured runner, run:</p>
					<button class="copy-link command" onclick={() => copy(`python run_local.py --join ${activeGrant!.display_code}`, 'link')}>
						{#if copied === 'link'}<Check size={13} /> Copied{:else}<Copy size={13} /> python run_local.py --join {activeGrant.display_code}{/if}
					</button>
				{:else if activeGrant.pairing_url}
					<button class="copy-link" onclick={() => copy(activeGrant!.pairing_url!, 'link')}>
						{#if copied === 'link'}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy pairing link{/if}
					</button>
				{:else}
					<p class="warning">No non-loopback address is available. Enter the code manually in a client already configured for this server.</p>
				{/if}
				<button class="cancel" onclick={cancelGrant}><X size={13} /> Cancel code</button>
			</div>
			{#if activeGrant.qr_svg}
				<div class="qr" aria-label="Pairing QR code">{@html activeGrant.qr_svg}</div>
			{/if}
		</div>
	{:else}
		<div class="create-row">
			<div class="endpoint">
				<label for="pair-endpoint">Address encoded in the QR</label>
				<select id="pair-endpoint" bind:value={selectedEndpoint}>
					{#each endpoints as endpoint (endpoint.url)}
						<option value={endpoint.url}>{endpoint.label} · {endpoint.url}{endpoint.loopback ? ' (code only)' : ''}</option>
					{/each}
					{#if endpoints.length === 0}<option value="">Code only — no address detected</option>{/if}
				</select>
			</div>
			<button class="secondary" onclick={() => create('runner')} disabled={creating} title="Approve a new runner after it has been configured for this database">
				<Plus size={15} /> Enroll runner
			</button>
			<button class="primary" onclick={() => create('controller')} disabled={creating}>
				{#if creating}<span>Creating…</span>{:else}<QrCode size={15} /> Pair device{/if}
			</button>
		</div>
	{/if}

	<div class="safety"><ShieldCheck size={14} /><span>Pairing creates a scoped controller session. Admin credentials, connectors, network keys, and future device pairing remain admin-only.</span></div>
	{#if error}<p class="error" role="alert">{error}</p>{/if}

	{#if clients.length > 0}
		<div class="client-list">
			{#each clients as client (client.id)}
				<div class="client-row">
					<div class="client-icon">
						{#if client.client_type === 'mobile'}<MonitorSmartphone size={15} />{:else}<Link2 size={15} />{/if}
					</div>
					<div class="client-main">
						<strong>{client.device_name}</strong>
						<span>{client.platform.replace('-browser', '')} · paired {relTime(client.paired_at)} · seen {relTime(client.last_seen)}</span>
					</div>
					{#if revokeClientId === client.id}
						<button class="icon danger" title="Confirm revoke" onclick={() => revokeClient(client.id)}><Check size={14} /></button>
						<button class="icon" title="Cancel" onclick={() => (revokeClientId = null)}><X size={14} /></button>
					{:else}
						<button class="icon" title="Revoke device" onclick={() => (revokeClientId = client.id)}><Trash2 size={13} /></button>
					{/if}
				</div>
			{/each}
		</div>
	{:else if !loading}
		<p class="empty">No controller devices have been paired yet.</p>
	{/if}
</section>

<style>
	.pair-panel { margin: 8px 12px 12px; border: 1px solid color-mix(in oklch, var(--primary) 22%, var(--border)); border-radius: 13px; overflow: hidden; background: color-mix(in oklch, var(--primary) 3%, transparent); }
	.pair-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 15px 16px 11px; }
	h4 { display: flex; align-items: center; gap: 7px; margin: 0; font-size: 13.5px; }
	.pair-head p { margin: 5px 0 0; max-width: 680px; color: var(--muted-foreground); font-size: 12px; line-height: 1.45; }
	.count { flex: 0 0 auto; padding: 3px 8px; border-radius: 999px; font: 600 10px var(--font-mono, monospace); text-transform: uppercase; color: var(--muted-foreground); background: var(--muted); }
	.create-row { display: flex; align-items: end; gap: 10px; padding: 8px 16px 14px; }
	.endpoint { display: grid; gap: 5px; flex: 1; min-width: 0; }
	.endpoint label, .grant-label { font-size: 10.5px; font-weight: 650; color: var(--muted-foreground); text-transform: uppercase; letter-spacing: .05em; }
	select { width: 100%; height: 34px; padding: 0 9px; border: 1px solid var(--border); border-radius: 8px; color: var(--foreground); background: var(--background); font-size: 12px; }
	button { font: inherit; }
	.primary, .secondary, .copy-link, .cancel { display: inline-flex; align-items: center; justify-content: center; gap: 6px; border-radius: 8px; cursor: pointer; }
	.primary { height: 34px; padding: 0 13px; border: 0; color: var(--primary-foreground); background: var(--primary); font-size: 12px; font-weight: 650; white-space: nowrap; }
	.secondary { height: 34px; padding: 0 11px; border: 1px solid var(--border); color: var(--foreground); background: var(--background); font-size: 12px; white-space: nowrap; }
	.primary:disabled, .secondary:disabled { opacity: .55; }
	.grant { display: flex; align-items: center; justify-content: center; gap: 28px; padding: 10px 18px 17px; }
	.grant-code-side { min-width: 245px; }
	.code-line { display: flex; align-items: center; gap: 7px; margin-top: 5px; }
	.code-line code { font-size: 18px; font-weight: 750; letter-spacing: .08em; color: var(--foreground); background: transparent; padding: 0; }
	.grant-code-side > p { margin: 5px 0 10px; font-size: 11.5px; color: var(--muted-foreground); }
	.copy-link, .cancel { padding: 6px 9px; border: 1px solid var(--border); background: var(--background); color: var(--foreground); font-size: 11.5px; }
	.copy-link.command { max-width: 100%; font-family: var(--font-mono, monospace); font-size: 10.5px; overflow: hidden; text-overflow: ellipsis; }
	.runner-help { margin-bottom: 5px !important; }
	.cancel { margin-left: 5px; color: var(--muted-foreground); }
	.qr { width: 150px; height: 150px; padding: 7px; box-sizing: border-box; border-radius: 10px; background: white; }
	.qr :global(svg) { display: block; width: 100%; height: 100%; }
	.icon { display: inline-grid; place-items: center; width: 28px; height: 28px; padding: 0; border: 0; border-radius: 7px; color: var(--muted-foreground); background: transparent; cursor: pointer; }
	.icon:hover { color: var(--foreground); background: var(--hover-overlay); }
	.icon.danger { color: var(--destructive); }
	.safety { display: flex; align-items: flex-start; gap: 7px; margin: 0 16px 12px; color: var(--muted-foreground); font-size: 11.5px; line-height: 1.4; }
	.safety :global(svg) { flex: 0 0 auto; margin-top: 1px; color: var(--primary); }
	.client-list { border-top: 1px solid var(--border); padding: 5px 8px; }
	.client-row { display: flex; align-items: center; gap: 9px; padding: 8px; border-radius: 8px; }
	.client-row:hover { background: var(--hover-overlay); }
	.client-icon { display: grid; place-items: center; width: 29px; height: 29px; border-radius: 8px; color: var(--primary); background: color-mix(in oklch, var(--primary) 11%, transparent); }
	.client-main { display: grid; flex: 1; min-width: 0; }
	.client-main strong { font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.client-main span { margin-top: 2px; font-size: 10.5px; color: var(--muted-foreground); text-transform: capitalize; }
	.empty, .muted { margin: 2px 16px 14px; color: var(--muted-foreground); font-size: 12px; }
	.error { margin: 0 16px 12px; color: var(--destructive); font-size: 12px; }
	.warning { color: var(--warning, #d97706) !important; max-width: 330px; }
	@media (max-width: 620px) { .create-row { align-items: stretch; flex-direction: column; } .primary { width: 100%; } .grant { flex-direction: column-reverse; gap: 14px; } .grant-code-side { min-width: 0; width: 100%; text-align: center; } .code-line { justify-content: center; } .pair-head { flex-direction: column; } }
</style>
