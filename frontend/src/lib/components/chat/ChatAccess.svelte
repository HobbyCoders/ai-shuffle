<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { auth } from '$lib/stores/auth.svelte';
	import { resourceSync } from '$lib/services/resource-sync';
	import PrivateAccessRequest from './PrivateAccessRequest.svelte';
	import '../deck/cards/card-design-system.css';
	import './private-access.css';
	let { sessionId }: { sessionId: string } = $props();
	type Grant = { id: string; label: string; kind: string; target: string; expires_at: number; writable: boolean; fingerprint?: string };
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
	let retryAt = 0;
	let timer: ReturnType<typeof setTimeout> | undefined;
	function schedule() {
		clearTimeout(timer);
		if (alive && !document.hidden && (!verified || enabled)) {
			timer = setTimeout(() => void refresh(), Math.max(0, retryAt - Date.now()));
		}
	}
	const base = untrack(() => `/chat-access/${encodeURIComponent(sessionId)}`);
	async function refresh() {
		if (!alive || document.hidden) return;
		// Resource broadcasts cannot shorten an authorization/error backoff.
		if (failures && Date.now() < retryAt) { schedule(); return; }
		// Preview cookies cannot use private resources, even on the owner's PC.
		// This is a capability limit, not a failure of the conversation.
		if (auth.current.status?.preview_access) {
			clearTimeout(timer);
			enabled = false;
			verified = true;
			data = {grants: [], actions: [], requests: [], saved_connections: [], vault_available: false};
			error = '';
			return;
		}
		if (refreshing) { dirty = true; return; }
		clearTimeout(timer);
		refreshing = true;
		try {
			// Keep the persisted mode check; retries must not expose private controls
			// for an unverified session or bypass endpoint authorization.
			if (!verified) {
				const session = await api.get<{mode: string; is_remote?: boolean}>(`/sessions/${encodeURIComponent(sessionId)}`);
				if (!alive) return;
				// Mirrored conversations can run remotely, but private grants stay
				// on the owner. Do not probe this worker's private-access endpoint.
				enabled = !session.is_remote && (session.mode == null || ['code', 'chat', 'voice'].includes(session.mode));
				verified = true;
			}
			if (!enabled) {
				data = {grants: [], actions: [], requests: [], saved_connections: [], vault_available: false};
				error = '';
				return;
			}
			const next = await api.get<typeof data>(base);
			if (!alive) return;
			data = next;
			error = '';
			failures = 0;
			retryMs = next.requests.length || next.actions.length || next.grants.length ? 2000 : 30_000;
			retryAt = Date.now() + retryMs;
		} catch (e) {
			if (!alive) return;
			error = getErrorMessage(e, 'Private access is unavailable.');
			const status = (e as {status?: number})?.status;
			failures++;
			retryMs = [401, 403, 404, 405].includes(status ?? 0)
				? 300_000 : Math.min(300_000, 5000 * 2 ** Math.min(failures - 1, 6));
			retryAt = Date.now() + retryMs;
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
		const reverify = () => { verified = false; resume(); };
		const recover = () => { if (!document.hidden) retryAt = 0; reverify(); };
		// Access mutations already carry a session-scoped invalidation. Refresh the
		// snapshot directly so approvals/grants do not also query the session row.
		// Session invalidations still re-check ownership and persisted mode before
		// touching the owner-only endpoint.
		const unsubscribeSession = resourceSync.subscribe(
			['sessions', `sessions:${sessionId}`], reverify
		);
		const unsubscribeAccess = resourceSync.subscribe(
			['chat-access', `chat-access:${sessionId}`], resume
		);
		document.addEventListener('visibilitychange', recover);
		window.addEventListener('online', recover);
		void refresh();
		return () => {
			alive = false;
			clearTimeout(timer);
			unsubscribeSession();
			unsubscribeAccess();
			document.removeEventListener('visibilitychange', recover);
			window.removeEventListener('online', recover);
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

{#snippet chevron()}
	<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7" /></svg>
{/snippet}
{#snippet serverIcon()}
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="4" width="18" height="7" rx="2" /><rect x="3" y="13" width="18" height="7" rx="2" /><path stroke-linecap="round" d="M7 7.5h.01M7 16.5h.01" /></svg>
{/snippet}
{#snippet folderIcon()}
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linejoin="round" d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" /></svg>
{/snippet}

{#if data.requests.length || data.actions.length || data.grants.length || data.saved_connections.length || error}
	<div class="private-access card-system" aria-label="Temporary access in this conversation">
		{#if error}<p class="pa-error" role="alert">{error}</p>{/if}
		{#each data.requests as request (request.id)}
			<PrivateAccessRequest {sessionId} {request} savedConnections={data.saved_connections} vaultAvailable={data.vault_available} onComplete={() => void refresh()} />
		{/each}
		{#each data.actions as action (action.id)}
			{@const grant = data.grants.find(g => g.id === action.grant_id)}
			<section class="pa-card" aria-label="One-shot action approval">
				<header class="pa-head">
					<div class="pa-icon" aria-hidden="true">
						{#if action.operation === 'command'}
							<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>
						{:else}
							<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.4-9.4a2 2 0 112.8 2.8L11.8 15H9v-2.8l8.6-8.6z" /></svg>
						{/if}
					</div>
					<div class="pa-title">
						<h3><span>Approve this {action.operation === 'command' ? 'remote command' : 'file write'}?</span><span class="card-badge card-badge--muted">Once</span></h3>
						<p class="pa-subtitle">{grant ? `${grant.label} · ${grant.target}` : 'Temporary resource'}</p>
					</div>
				</header>
				<div class="pa-body">
					{#if action.operation === 'command'}
						<pre class="pa-mono"><span class="prompt">$ </span>{action.arguments.command}</pre>
					{:else}
						<pre class="pa-mono"><span class="prompt">{action.arguments.path}</span>{`\n\n${action.arguments.content}`}</pre>
					{/if}
					<p class="pa-hint">{action.operation === 'command' ? 'Runs once on the server with this account’s authority. It can change files and use the server’s network.' : 'Writes this text once. Later writes ask again.'}</p>
				</div>
				<div class="pa-section">
					<div class="pa-actions">
						<button class="card-btn-secondary" disabled={busy} onclick={() => decide(action.id, false)}>Deny</button>
						<button class="card-btn-primary" disabled={busy} onclick={() => decide(action.id, true)}>Approve once</button>
					</div>
				</div>
			</section>
		{/each}
		{#if data.grants.length || data.saved_connections.length}
			<details class="pa-card access-panel" open={data.grants.length > 0}>
				<summary class="pa-summary">
					<span class="pa-icon">
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m5.6-4.6A12 12 0 0112 3a12 12 0 01-8.6 3.4A12 12 0 003 9c0 5.6 3.8 10.3 9 11.6 5.2-1.3 9-6 9-11.6 0-1.2-.2-2.4-.4-3.6z" /></svg>
					</span>
					<span class="pa-summary-lead">
						Private access
						{#if data.grants.length}<span class="card-badge card-badge--primary">{data.grants.length} active</span>{/if}
						{#if data.saved_connections.length}<span class="card-badge card-badge--muted">{data.saved_connections.length} saved</span>{/if}
					</span>
					{@render chevron()}
				</summary>
				{#each data.grants as grant (grant.id)}
					<div class="pa-row">
						<span class="pa-icon row-icon">{#if grant.kind === 'machine'}{@render serverIcon()}{:else}{@render folderIcon()}{/if}</span>
						<div class="pa-row-main">
							<strong>{grant.label}</strong>
							<code>{grant.target}</code>
							<small>{grant.kind === 'machine' ? 'Commands need approval' : grant.writable ? 'Read and approved writes' : 'Read only'} · until {new Date(grant.expires_at * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})}{#if grant.kind === 'machine' && grant.fingerprint} · host key {grant.fingerprint.slice(0, 19)}…{/if}</small>
						</div>
						<div class="pa-row-actions">
							{#if confirmingRevoke === grant.id}
								<button class="pa-small-btn" onclick={() => confirmingRevoke = null}>Keep</button>
								<button class="pa-small-btn danger solid" disabled={busy} onclick={() => revoke(grant.id)}>Revoke</button>
							{:else}
								<button class="pa-small-btn danger" onclick={() => confirmingRevoke = grant.id}>Revoke</button>
							{/if}
						</div>
					</div>
				{/each}
				{#if data.saved_connections.length}
					<p class="pa-group">Saved connections</p>
					{#each data.saved_connections as saved (saved.id)}
						<div class="pa-row">
							<span class="pa-icon row-icon">{@render serverIcon()}</span>
							<div class="pa-row-main">
								<strong>{saved.label}</strong>
								<code>{saved.target}</code>
								<small>Encrypted on this worker{#if saved.fingerprint} · host key {saved.fingerprint.slice(0, 19)}…{/if}</small>
							</div>
							<div class="pa-row-actions">
								{#if confirmingDelete === saved.id}
									<button class="pa-small-btn" onclick={() => confirmingDelete = null}>Keep</button>
									<button class="pa-small-btn danger solid" disabled={busy} onclick={() => removeSaved(saved.id)}>Delete</button>
								{:else}
									<button class="pa-small-btn danger" onclick={() => confirmingDelete = saved.id}>Delete</button>
								{/if}
							</div>
						</div>
					{/each}
				{/if}
				<p class="pa-hint panel-foot">Worker restart or expiry ends access. Revoking closes open connections but can’t undo changes already made. Deleting a saved connection also revokes access that uses it.</p>
			</details>
		{/if}
	</div>
{/if}

<style>
	.private-access { min-width: 0; display: grid; gap: .75rem; margin-block: .5rem; }
	.access-panel { box-shadow: none; animation: none; }
	.row-icon { width: 2rem; height: 2rem; border-radius: .5rem; }
	.row-icon :global(svg) { width: .9rem; height: .9rem; }
	.pa-group { margin: 0; padding: .5rem 1rem .25rem; border-top: 1px solid color-mix(in oklch, var(--border) 40%, transparent); font-size: .6875rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted-foreground); }
	.panel-foot { padding: .625rem 1rem .75rem; border-top: 1px solid color-mix(in oklch, var(--border) 40%, transparent); }
</style>
