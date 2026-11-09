<script lang="ts">
	/**
	 * Devices and terminals on this worker (#674): a device asks to link, the
	 * owner enters the code that device shows, and from then on that device can
	 * open shells here with its own signature. The code is never listed, so it
	 * has to be read off the device itself.
	 *
	 * `full` is the Settings view with the linked devices and Revoke. Without it
	 * this is the Terminal panel's short form, only while a device is waiting.
	 */
	import { onMount } from 'svelte';
	import { MonitorSmartphone, ShieldCheck } from 'lucide-svelte';
	import { peerTerminals } from '$lib/stores/peer-terminals.svelte';
	import { age } from '$lib/terminal/peer';

	let { workerName, full = false }: { workerName: string; full?: boolean } = $props();

	onMount(() => peerTerminals.watch());

	const waiting = $derived(peerTerminals.requests);
	const links = $derived(peerTerminals.links);
	let code = $state('');
	let error = $state('');
	const fieldId = $derived(full ? 'device-link-code-full' : 'device-link-code');

	const when = (value: string | null) => (value ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'Never');

	async function approve(event: SubmitEvent) {
		event.preventDefault();
		const text = code.trim();
		if (!text || peerTerminals.approving) return;
		error = '';
		const refused = await peerTerminals.approve(text);
		if (refused) error = refused;
		else code = '';
	}
</script>

{#if full || waiting.length || peerTerminals.error}
	<section class="device-links" class:full aria-label="Devices linked to {workerName}">
		{#if peerTerminals.error && !waiting.length && !links.length}<p class="err" role="alert">{peerTerminals.error}</p>{/if}

		{#if waiting.length}
			<div class="group">
				<h4>Waiting to link</h4>
				{#each waiting as request (request.request_id)}
					{@const timing = peerTerminals.timing(request)}
					{@const busy = peerTerminals.working(request.request_id)}
					{@const notice = peerTerminals.notice(request.request_id)}
					<article class="row">
						<span class="glyph" aria-hidden="true"><MonitorSmartphone size={15} /></span>
						<div class="text">
							<strong>{request.label || 'A device'}</strong>
							<span class="meta">Through {request.worker_name} · asked {age(timing.age)}</span>
							{#if notice}<span class="err" role="alert">{notice}</span>{/if}
						</div>
						<button type="button" class="st-btn" disabled={!!busy} onclick={() => peerTerminals.decline(request.request_id)}>
							{busy === 'decline' ? 'Declining…' : 'Decline'}
						</button>
					</article>
				{/each}
				<form class="approve" onsubmit={approve}>
					<label class="code-label" for={fieldId}>Code shown on the device</label>
					<div class="code-row">
						<input id={fieldId} class="code" bind:value={code} spellcheck="false" autocomplete="off" autocapitalize="characters" placeholder="ABCD EFGH IJKL" aria-describedby="device-link-why" />
						<button type="submit" class="st-btn primary" disabled={!code.trim() || peerTerminals.approving}>{peerTerminals.approving ? 'Linking…' : 'Link device'}</button>
					</div>
					{#if error}<p class="err" role="alert">{error}</p>{/if}
					<p class="why" id="device-link-why">Read the code from the device itself. Linking lets it open shells on {workerName} until you revoke it here, so never enter a code someone sends you.</p>
				</form>
			</div>
		{/if}

		{#if full}
			<div class="group">
				<h4>Linked devices</h4>
				{#each links as link (link.key_id)}
					{@const busy = peerTerminals.working(link.key_id)}
					{@const notice = peerTerminals.notice(link.key_id)}
					<article class="row">
						<span class="glyph ok" aria-hidden="true"><ShieldCheck size={15} /></span>
						<div class="text">
							<strong>{link.label || 'A device'}</strong>
							<span class="meta">Through {link.worker_name} · last used {when(link.last_used_at)}</span>
							{#if notice}<span class="err" role="alert">{notice}</span>{/if}
						</div>
						<button type="button" class="st-btn danger" disabled={!!busy} onclick={() => peerTerminals.revoke(link.key_id)}>
							{busy === 'revoke' ? 'Revoking…' : 'Revoke'}
						</button>
					</article>
				{:else}
					<p class="empty">No devices linked yet.</p>
				{/each}
				{#if links.length}<p class="why">Revoking closes any shell that device has open here, straight away.</p>{/if}
			</div>
		{/if}
	</section>
{/if}

<style>
	.device-links { display: grid; grid-auto-rows: max-content; gap: 12px; padding: 10px; max-height: 60%; overflow-y: auto; flex-shrink: 0; overscroll-behavior: contain;
		border-bottom: 1px solid var(--border); background: var(--background); font-family: var(--font-sans); }
	.device-links.full { padding: 12px 0 0; max-height: none; border-bottom: 0; background: transparent; }
	.group { display: grid; gap: 8px; }
	h4 { margin: 0; font-size: 12px; font-weight: 600; color: var(--subtle-foreground); }
	.row { display: flex; gap: 10px; align-items: center; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--card); }
	.glyph { display: grid; place-items: center; width: 28px; height: 28px; flex-shrink: 0; border-radius: 8px; color: var(--muted-foreground); background: var(--muted); }
	.glyph.ok { color: var(--success); background: color-mix(in oklch, var(--success) 12%, transparent); }
	.text { flex: 1; min-width: 0; display: grid; gap: 1px; }
	strong { font-size: 13px; font-weight: 600; color: var(--foreground); overflow-wrap: anywhere; }
	.meta, .empty { font-size: 11.5px; color: var(--muted-foreground); margin: 0; overflow-wrap: anywhere; }
	.approve { display: grid; gap: 6px; }
	.code-label { font: 500 12px/1.4 var(--font-sans); color: var(--subtle-foreground); }
	.code-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
	.code { flex: 1 1 180px; min-width: 0; min-height: 34px; padding: 0 10px; box-sizing: border-box; border: 1px solid var(--input); border-radius: calc(var(--radius) - 4px);
		background: var(--code-bg); color: var(--foreground); outline: 0; font: 13px var(--st-mono); letter-spacing: 0.04em; text-transform: uppercase; }
	.code::placeholder { letter-spacing: 0.04em; color: var(--subtle-foreground); }
	.code:focus { border-color: var(--ring); box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 20%, transparent); }
	.why { margin: 0; font-size: 11.5px; line-height: 1.5; color: var(--muted-foreground); }
	.err { margin: 0; font-size: 12px; color: var(--destructive); }
	@media (max-width: 700px) {
		.device-links { max-height: 75%; padding: 8px; }
		.device-links.full { padding: 12px 0 0; max-height: none; }
		button, .code { min-height: 44px; }
		.code { font-size: 16px; }
	}
</style>
