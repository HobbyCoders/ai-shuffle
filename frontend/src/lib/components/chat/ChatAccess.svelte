<script lang="ts">
	import { onDestroy, onMount, untrack } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { auth } from '$lib/stores/auth.svelte';
	import { resourceSync } from '$lib/services/resource-sync';
	import { chatAccessSummary, setPrivateAccessPending, type PendingAccessItem } from '$lib/stores/chat-access';
	import PrivateAccessRequest from './PrivateAccessRequest.svelte';
	import '../deck/cards/card-design-system.css';
	import './private-access.css';
	/** `trigger: 'none'` renders only the dialog; the chat header's git panel
	 *  opens it through the chatAccessSummary store instead of a button here. */
	let { sessionId, trigger = 'button' }: { sessionId: string; trigger?: 'button' | 'none' } = $props();
	type Grant = { id: string; label: string; kind: string; target: string; expires_at: number; writable: boolean; fingerprint?: string };
	type Action = { id: string; grant_id: string; operation: string; arguments: Record<string, string>; expires_at: number };
	type Request = { id: string; kind: string; purpose: string; expires_at: number; suggested_path?: string; service_provider?: string; suggested_url?: string; plugin?: string; service?: string };
	type Saved = {id: string; label: string; target: string; fingerprint: string; kind?: string; writable?: boolean};
	let data = $state<{grants: Grant[]; actions: Action[]; requests: Request[]; saved_connections: Saved[]; vault_available: boolean}>({grants: [], actions: [], requests: [], saved_connections: [], vault_available: false});
	let confirmingDelete = $state<string | null>(null);
	/** The device a mirrored conversation runs on, where its grants and saved connections live. */
	let owner = $state<string | null>(null);
	async function removeSaved(id: string) {
		busy = true; error = '';
		try { await api.delete(`${base}/saved/${id}`); confirmingDelete = null; await refresh(); }
		catch (e) { error = getErrorMessage(e, 'Could not remove the saved connection.'); }
		finally { busy = false; }
	}
	// Disclosure belongs to the user, not to a freshly fetched grants array.
	// Streaming/resource invalidations must never reopen either surface.
	let accessExpanded = $state(false);
	let dialog = $state<HTMLDialogElement>();
	let clearSecretsKey = $state(0);
	const pendingCount = $derived(data.requests.length + data.actions.length);
	let error = $state('');
	// The header reads the pending count for its button label and the git
	// panel's "Private access" row opens the dialog from outside this component.
	const openDialog = () => { if (dialog && !dialog.open) dialog.showModal(); };
	// What is waiting, for the chat's own prompt (PrivateAccessPrompt, #1807).
	const items = $derived<PendingAccessItem[]>([
		...data.requests.map((r) => ({
			id: r.id,
			kind: (['machine', 'folder', 'service'].includes(r.kind) ? r.kind : 'machine') as PendingAccessItem['kind'],
			detail: r.purpose
		})),
		...data.actions.map((a) => ({
			id: a.id,
			kind: 'action' as const,
			detail: a.operation === 'command' ? `$ ${a.arguments.command ?? ''}`
				: a.operation === 'http' ? `${a.arguments.method ?? ''} ${a.arguments.path ?? ''}`
				: `Write ${a.arguments.path ?? ''}`
		}))
	]);
	$effect(() => {
		chatAccessSummary.set({
			sessionId,
			pending: pendingCount,
			items,
			active: data.grants.length,
			saved: data.saved_connections.length,
			error: !!error,
			open: openDialog
		});
	});
	onDestroy(() => chatAccessSummary.update((cur) => (cur?.sessionId === sessionId ? null : cur)));
	let confirmingRevoke = $state<string | null>(null);
	let busy = $state(false);
	// Refreshes are event-driven: the broker sends a session-scoped
	// chat_access_changed on every request, action and grant change (including
	// expiry), plus the owner-wide `chat-access` resource when a saved connection
	// is saved or deleted (every session's panel lists them), and live-sync
	// invalidates every resource after a reconnect or on
	// return to the tab. A timer runs only as a safety net while a request or
	// approval is waiting (2 s doubling to 30 s), and after transient failures.
	// An empty or grant-only snapshot is not polled. An authorization denial
	// stops all requests until the sign-in changes or the browser comes online.
	const PENDING_MIN_MS = 2000, PENDING_MAX_MS = 30_000;
	let refreshing = false;
	let sent = false;
	let alive = true;
	let enabled = false;
	let verified = false;
	let dirty = false;
	let denied = false;
	let failures = 0;
	let retryAt = 0;
	let pollMs = PENDING_MIN_MS;
	let timer: ReturnType<typeof setTimeout> | undefined;
	function schedule(ms: number | undefined) {
		clearTimeout(timer);
		if (ms !== undefined && alive && !document.hidden && !denied && (!verified || enabled)) {
			timer = setTimeout(() => void refresh(true), Math.max(0, ms));
		}
	}
	const base = untrack(() => `/chat-access/${encodeURIComponent(sessionId)}`);
	async function refresh(poll = false) {
		if (!alive || document.hidden || denied) return;
		// Resource broadcasts cannot shorten an error backoff.
		if (failures && Date.now() < retryAt) { schedule(retryAt - Date.now()); return; }
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
		// A change seen before the snapshot request is sent is covered by it.
		if (refreshing) { if (sent) dirty = true; return; }
		clearTimeout(timer);
		refreshing = true;
		dirty = false;
		let wait: number | undefined;
		try {
			// Keep the persisted mode check; retries must not expose private controls
			// for an unverified session or bypass endpoint authorization.
			if (!verified) {
				const session = await api.get<{mode: string; is_remote?: boolean; device_name?: string | null}>(`/sessions/${encodeURIComponent(sessionId)}`);
				if (!alive) return;
				// A mirrored conversation's grants stay on the device that runs it;
				// this worker forwards the panel's requests there.
				enabled = session.mode == null || ['code', 'chat', 'voice'].includes(session.mode);
				owner = session.is_remote ? (session.device_name || 'the device that runs this conversation') : null;
				verified = true;
			}
			if (!enabled) {
				data = {grants: [], actions: [], requests: [], saved_connections: [], vault_available: false};
				error = '';
				return;
			}
			sent = true;
			const next = await api.get<typeof data>(base);
			if (!alive) return;
			data = next;
			// The sidebar's "Needs you" mark follows the authoritative snapshot.
			setPrivateAccessPending(sessionId, next.requests.length + next.actions.length);
			error = '';
			failures = 0;
			if (next.requests.length || next.actions.length) {
				pollMs = poll ? Math.min(PENDING_MAX_MS, pollMs * 2) : PENDING_MIN_MS;
				wait = pollMs;
			}
		} catch (e) {
			if (!alive) return;
			error = getErrorMessage(e, 'Private access is unavailable.');
			const status = (e as {status?: number})?.status;
			if ([401, 403, 404, 405].includes(status ?? 0)) {
				denied = true;
			} else {
				failures++;
				wait = Math.min(300_000, 5000 * 2 ** Math.min(failures - 1, 6));
				retryAt = Date.now() + wait;
			}
		} finally {
			refreshing = false;
			sent = false;
			if (dirty && alive && !document.hidden && !denied) void refresh();
			else schedule(wait);
		}
	}
	onMount(() => {
		const resume = () => {
			clearTimeout(timer);
			if (!document.hidden) void refresh();
		};
		const reverify = () => { verified = false; if (refreshing) dirty = true; resume(); };
		// Coalesce with live-sync's own resume invalidation (same 100 ms batch)
		// so a return to the tab or reconnect checks the session and snapshot once.
		const recover = () => {
			if (document.hidden) { clearTimeout(timer); return; }
			retryAt = 0;
			resourceSync.invalidate([`sessions:${sessionId}`, `chat-access:${sessionId}`]);
		};
		const online = () => { denied = false; recover(); };
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
		// A denial only changes with the signed-in principal or preview status.
		let principal: string | undefined;
		const unsubscribeAuth = auth.subscribe(({status: s}) => {
			const next = JSON.stringify([s?.authenticated, s?.is_admin, s?.username, s?.preview_access, s?.preference_identity]);
			if (principal !== undefined && next !== principal) { denied = false; failures = 0; retryAt = 0; reverify(); }
			principal = next;
		});
		document.addEventListener('visibilitychange', recover);
		window.addEventListener('online', online);
		void refresh();
		return () => {
			alive = false;
			clearTimeout(timer);
			unsubscribeSession();
			unsubscribeAccess();
			unsubscribeAuth();
			document.removeEventListener('visibilitychange', recover);
			window.removeEventListener('online', online);
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

<svelte:window onkeydowncapture={(event) => {
	if (event.key === 'Escape' && dialog?.open) {
		event.preventDefault();
		event.stopImmediatePropagation();
		dialog.close();
	}
}} />

{#snippet chevron()}
	<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7" /></svg>
{/snippet}
{#snippet serverIcon()}
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="4" width="18" height="7" rx="2" /><rect x="3" y="13" width="18" height="7" rx="2" /><path stroke-linecap="round" d="M7 7.5h.01M7 16.5h.01" /></svg>
{/snippet}
{#snippet serviceIcon()}
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.7 5.7L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.6a1 1 0 01.3-.7l6-6A6 6 0 1121 9z" /></svg>
{/snippet}
{#snippet folderIcon()}
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linejoin="round" d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" /></svg>
{/snippet}

{#if data.requests.length || data.actions.length || data.grants.length || data.saved_connections.length || error}
	{#if trigger === 'button'}
	<button class="access-trigger" class:needs-attention={pendingCount > 0 || !!error}
		onclick={() => dialog?.showModal()} aria-label="Private access" aria-haspopup="dialog"
		title="Private access for this conversation">
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m5.6-4.6A12 12 0 0112 3a12 12 0 01-8.6 3.4A12 12 0 003 9c0 5.6 3.8 10.3 9 11.6 5.2-1.3 9-6 9-11.6 0-1.2-.2-2.4-.4-3.6z" /></svg>
		<span class="access-label">Access</span>
		<span class="access-status" aria-live="polite">
			{#if pendingCount}{pendingCount} pending{:else if error}!{:else if data.grants.length}{data.grants.length} active{/if}
		</span>
	</button>
	{/if}
	<dialog bind:this={dialog} onclose={() => clearSecretsKey++} class="access-dialog card-system" aria-label="Private access for this conversation">
		<header class="access-dialog-head">
			<h2>Private access</h2>
			<button class="access-close" aria-label="Close private access" onclick={() => dialog?.close()}>×</button>
		</header>
		<div class="private-access" aria-label="Temporary access in this conversation">
		{#if error}<p class="pa-error" role="alert">{error}</p>{/if}
		{#each data.requests as request (request.id)}
			<PrivateAccessRequest {sessionId} {request} savedConnections={data.saved_connections} vaultAvailable={data.vault_available} {owner} {clearSecretsKey} onComplete={() => void refresh()} />
		{/each}
		{#each data.actions as action (action.id)}
			{@const grant = data.grants.find(g => g.id === action.grant_id)}
			{@const sandboxed = action.operation === 'command' && grant?.kind === 'folder'}
			<section class="pa-card" aria-label="One-shot action approval">
				<header class="pa-head">
					<div class="pa-icon" aria-hidden="true">
						{#if action.operation === 'command'}
							<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>
						{:else if action.operation === 'http'}
							{@render serviceIcon()}
						{:else}
							<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.4-9.4a2 2 0 112.8 2.8L11.8 15H9v-2.8l8.6-8.6z" /></svg>
						{/if}
					</div>
					<div class="pa-title">
						<h3><span>Approve this {sandboxed ? 'sandboxed command' : action.operation === 'command' ? 'remote command' : action.operation === 'http' ? 'API request' : 'file write'}?</span><span class="card-badge card-badge--muted">Once</span></h3>
						<p class="pa-subtitle">{grant ? `${grant.label} · ${grant.target}` : 'Temporary resource'}</p>
					</div>
				</header>
				<div class="pa-body">
					{#if action.operation === 'command'}
						<pre class="pa-mono"><span class="prompt">$ </span>{action.arguments.command}</pre>
					{:else if action.operation === 'http'}
						<pre class="pa-mono"><span class="prompt">{action.arguments.method} </span>{action.arguments.path}{action.arguments.body ? `\n${action.arguments.content_type}\n\n${action.arguments.body}` : ''}</pre>
					{:else}
						<pre class="pa-mono"><span class="prompt">{action.arguments.path}</span>{`\n\n${action.arguments.content}`}</pre>
					{/if}
					<p class="pa-hint">{sandboxed ? `Runs once in an isolated sandbox that sees only this folder and has no network. ${grant?.writable ? 'It can change files in the folder.' : 'The folder is read-only to it.'}` : action.operation === 'command' ? 'Runs once on the server with this account’s authority. It can change files and use the server’s network.' : action.operation === 'http' ? 'Sends this request once with the granted credential attached. Later changes ask again.' : 'Writes this text once. Later writes ask again.'}</p>
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
			<details class="pa-card access-panel" bind:open={accessExpanded}>
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
						<span class="pa-icon row-icon">{#if grant.kind === 'machine'}{@render serverIcon()}{:else if grant.kind === 'service'}{@render serviceIcon()}{:else}{@render folderIcon()}{/if}</span>
						<div class="pa-row-main">
							<strong>{grant.label}</strong>
							<code>{grant.target}</code>
							<small>{grant.kind === 'machine' ? 'Remote commands · session permission mode applies' : grant.kind === 'service' ? (grant.writable ? 'HTTPS API · changes follow session permission mode' : 'HTTPS API · GET and HEAD only') : grant.writable ? 'Read/write · session permission mode applies' : 'Read only'} · until {new Date(grant.expires_at * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})}{#if grant.kind === 'machine' && grant.fingerprint} · host key {grant.fingerprint.slice(0, 19)}…{/if}</small>
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
							<span class="pa-icon row-icon">{#if saved.kind === 'service'}{@render serviceIcon()}{:else}{@render serverIcon()}{/if}</span>
							<div class="pa-row-main">
								<strong>{saved.label}</strong>
								<code>{saved.target}</code>
								<small>Encrypted on {owner ?? 'this worker'}{#if saved.fingerprint} · host key {saved.fingerprint.slice(0, 19)}…{/if}</small>
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
	</dialog>
{/if}

<style>
	.access-trigger { display: inline-flex; align-items: center; justify-content: center; gap: .375rem; min-height: 44px; padding: .375rem .625rem; border: 1px solid var(--border); border-radius: .5rem; background: var(--card); color: var(--muted-foreground); font-size: .75rem; cursor: pointer; white-space: nowrap; }
	.access-trigger svg { width: 1rem; height: 1rem; flex-shrink: 0; }
	.access-trigger:hover, .access-trigger.needs-attention { color: var(--foreground); border-color: var(--primary); }
	.access-status { font-size: .6875rem; font-variant-numeric: tabular-nums; }
	.access-status:empty { display: none; }
	.needs-attention .access-status { font-weight: 600; }
	.access-dialog { box-sizing: border-box; width: min(38rem, calc(100vw - 1.5rem)); max-width: calc(100vw - 1.5rem); max-height: calc(100dvh - 1.5rem); margin: auto; padding: 0; border: 1px solid var(--border); border-radius: 1rem; background: var(--background); color: var(--foreground); box-shadow: var(--shadow-l); overflow: hidden; }
	.access-dialog[open] { display: flex; flex-direction: column; }
	.access-dialog::backdrop { background: rgb(0 0 0 / .45); }
	.access-dialog-head { flex: none; display: flex; align-items: center; justify-content: space-between; padding: .5rem 1rem; background: var(--background); border-bottom: 1px solid var(--border); }
	.access-dialog-head h2 { margin: 0; font-size: .9375rem; font-weight: 600; }
	.access-close { min-width: 44px; min-height: 44px; border: 0; border-radius: .5rem; background: transparent; color: var(--muted-foreground); font-size: 1.5rem; cursor: pointer; }
	.access-trigger:focus-visible, .access-close:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.private-access { min-width: 0; min-height: 0; display: grid; grid-template-columns: minmax(0, 1fr); grid-auto-rows: max-content; align-content: start; gap: .75rem; padding: .75rem; overflow: auto; overscroll-behavior: contain; }
	@media (max-width: 480px) {
		.access-label { display: none; }
		.access-dialog-head { padding: .375rem .75rem; }
		.private-access { gap: .625rem; padding: .625rem; }
	}
	.access-panel { box-shadow: none; animation: none; }
	.row-icon { width: 2rem; height: 2rem; border-radius: .5rem; }
	.row-icon :global(svg) { width: .9rem; height: .9rem; }
	.pa-group { margin: 0; padding: .5rem 1rem .25rem; border-top: 1px solid color-mix(in oklch, var(--border) 40%, transparent); font-size: .6875rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted-foreground); }
	.panel-foot { padding: .625rem 1rem .75rem; border-top: 1px solid color-mix(in oklch, var(--border) 40%, transparent); }
</style>
