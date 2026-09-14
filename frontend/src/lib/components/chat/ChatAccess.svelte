<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { resourceSync } from '$lib/services/resource-sync';
	import PrivateAccessRequest from './PrivateAccessRequest.svelte';
	let { sessionId }: { sessionId: string } = $props();
	type Grant = { id: string; label: string; kind: string; target: string; expires_at: number; writable: boolean };
	type Action = { id: string; grant_id: string; operation: string; arguments: Record<string, string>; expires_at: number };
	type Request = { id: string; kind: string; purpose: string; expires_at: number };
	type Saved = {id: string; label: string; target: string; fingerprint: string};
	let data = $state<{grants: Grant[]; actions: Action[]; requests: Request[]; saved_connections: Saved[]; vault_available: boolean}>({grants: [], actions: [], requests: [], saved_connections: [], vault_available: false});
	let confirmingDelete = $state<string | null>(null);
	async function removeSaved(id: string) {
		busy = true; error = '';
		try { await api.delete(`${base}/saved/${id}`); confirmingDelete = null; await refresh(); }
		catch (e) { error = getErrorMessage(e, 'Could not remove the saved connection.'); }
		finally { busy = false; }
	}
	let error = $state('');
	let confirmingRevoke = $state<string | null>(null);
	let busy = $state(false);
	let refreshing = false;
	let alive = true;
	let enabled = false;
	let verified = false;
	let dirty = false;
	let failures = 0;
	let retryMs = 30_000;
	let timer: ReturnType<typeof setTimeout> | undefined;
	function schedule() {
		clearTimeout(timer);
		if (alive && !document.hidden && (!verified || enabled)) {
			timer = setTimeout(() => void refresh(), retryMs);
		}
	}
	const base = untrack(() => `/chat-access/${encodeURIComponent(sessionId)}`);
	async function refresh() {
		if (!alive || document.hidden) return;
		if (refreshing) { dirty = true; return; }
		clearTimeout(timer);
		refreshing = true;
		try {
			// Keep the persisted mode check; retries must not expose private controls
			// for an unverified session or bypass endpoint authorization.
			if (!verified) {
				const session = await api.get<{mode: string}>(`/sessions/${encodeURIComponent(sessionId)}`);
				if (!alive) return;
				enabled = session.mode == null || ['code', 'chat', 'voice'].includes(session.mode);
				verified = true;
			}
			if (!enabled) return;
			const next = await api.get<typeof data>(base);
			if (!alive) return;
			data = next;
			error = '';
			failures = 0;
			retryMs = next.requests.length || next.actions.length || next.grants.length ? 2000 : 30_000;
		} catch (e) {
			if (!alive) return;
			error = getErrorMessage(e, 'Private access is unavailable.');
			const status = (e as {status?: number})?.status;
			failures++;
			retryMs = [401, 403, 404, 405].includes(status ?? 0)
				? 300_000 : Math.min(300_000, 5000 * 2 ** Math.min(failures - 1, 6));
		} finally {
			refreshing = false;
			if (dirty && alive && !document.hidden) { dirty = false; void refresh(); }
			else schedule();
		}
	}
	onMount(() => {
		const resume = () => {
			clearTimeout(timer);
			if (!document.hidden) void refresh();
		};
		const unsubscribe = resourceSync.subscribe(['chat-access', `chat-access:${sessionId}`], resume);
		document.addEventListener('visibilitychange', resume);
		window.addEventListener('online', resume);
		void refresh();
		return () => {
			alive = false;
			clearTimeout(timer);
			unsubscribe();
			document.removeEventListener('visibilitychange', resume);
			window.removeEventListener('online', resume);
		};
	});
	async function revoke(id: string) {
		busy = true; error = '';
		try { await api.delete(`${base}/grants/${id}`); confirmingRevoke = null; await refresh(); }
		catch (e) { error = getErrorMessage(e, 'Could not revoke access.'); }
		finally { busy = false; }
	}
	async function decide(id: string, allow: boolean) {
		busy = true; error = '';
		try { await api.post(`${base}/actions/${id}`, {allow}); await refresh(); }
		catch (e) { error = getErrorMessage(e, 'Could not answer this request.'); }
		finally { busy = false; }
	}
</script>

{#if data.requests.length || data.actions.length || data.grants.length || data.saved_connections.length || error}
	<div class="private-access" aria-label="Temporary access in this conversation">
		{#if error}<p class="error" role="alert">{error}</p>{/if}
		{#each data.requests as request (request.id)}
			<PrivateAccessRequest {sessionId} {request} savedConnections={data.saved_connections} vaultAvailable={data.vault_available} onComplete={() => void refresh()} />
		{/each}
		{#each data.actions as action (action.id)}
			<section class="card" aria-label="One-shot action approval">
				<h3>Approve this {action.operation === 'command' ? 'remote command' : 'file write'}?</h3>
				<p>{data.grants.find(g => g.id === action.grant_id)?.label ?? 'Temporary resource'}</p>
				<pre>{action.operation === 'command' ? action.arguments.command : `${action.arguments.path}\n\n${action.arguments.content}`}</pre>
				<p class="muted">This approval applies only to the action shown. Remote commands can change files and use the server’s network.</p>
				<div class="buttons"><button disabled={busy} onclick={() => decide(action.id, false)}>Deny</button><button disabled={busy} onclick={() => decide(action.id, true)}>Approve once</button></div>
			</section>
		{/each}
		{#if data.saved_connections.length}
			<details class="card"><summary>Saved connections · {data.saved_connections.length}</summary>
				<p class="muted">Encrypted on this worker, not synced to other devices. Saving credentials does not grant session access. Deleting also revokes active grants using this saved connection; backups may retain encrypted copies.</p>
				{#each data.saved_connections as saved (saved.id)}
					<div class="grant"><strong>{saved.label}</strong><span>{saved.target}</span>
						{#if confirmingDelete === saved.id}<div class="buttons"><button disabled={busy} onclick={() => removeSaved(saved.id)}>Delete saved connection and revoke access</button><button onclick={() => confirmingDelete = null}>Keep connection</button></div>{:else}<button onclick={() => confirmingDelete = saved.id}>Delete saved connection</button>{/if}
					</div>
				{/each}
			</details>
		{/if}
		{#if data.grants.length}
			<details class="card"><summary>Temporary access · {data.grants.length} active</summary>
				<p class="muted">Connection details stay out of chat history. Worker restart or expiry removes access. Revocation closes active connections; it cannot undo changes or guarantee detached remote processes stop.</p>
				{#each data.grants as grant (grant.id)}
					<div class="grant"><strong>{grant.label}</strong><span>{grant.target}</span><span class="muted">{grant.kind === 'machine' ? 'Machine administration' : grant.writable ? 'Read/write text' : 'Read-only text'} · expires {new Date(grant.expires_at * 1000).toLocaleTimeString()}</span>
						{#if confirmingRevoke === grant.id}<div class="buttons"><button disabled={busy} onclick={() => revoke(grant.id)}>Confirm revoke</button><button onclick={() => confirmingRevoke = null}>Keep access</button></div>{:else}<button onclick={() => confirmingRevoke = grant.id}>Revoke access</button>{/if}
					</div>
				{/each}
			</details>
		{/if}
	</div>
{/if}

<style>
	.private-access { min-width: 0; display: grid; gap: .75rem; margin-block: .5rem; }
	.card { min-width: 0; padding: 1rem; border: 1px solid var(--border); border-radius: 12px; background: var(--card); color: var(--foreground); }
	h3 { font-size: .95rem; margin: 0; }
	p { font-size: .85rem; line-height: 1.5; }
	pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 240px; overflow: auto; padding: .7rem; background: var(--muted); border-radius: 6px; font-size: .8rem; }
	.buttons { display: flex; gap: .5rem; justify-content: flex-end; flex-wrap: wrap; }
	button { font: inherit; font-size: .8rem; padding: .5rem .75rem; border: 1px solid var(--border); border-radius: 6px; cursor: pointer; background: var(--background); color: var(--foreground); }
	button:disabled { opacity: .5; cursor: default; }
	.grant { display: grid; gap: .35rem; overflow-wrap: anywhere; border-top: 1px solid var(--border); padding-top: .7rem; margin-top: .7rem; font-size: .85rem; }
	.grant > button { justify-self: start; }
	.muted { color: var(--muted-foreground); font-size: .8rem; }
	.error { color: var(--destructive); }
	summary { cursor: pointer; font-size: .85rem; }
</style>
