<script lang="ts">
	/**
	 * Private access for the open chat: a quiet icon in the chat header and the
	 * panel it opens. The panel follows the header's other secondary panels (the
	 * Git panel): anchored under the icon on desktop, a bottom sheet on phones
	 * and touch screens, and only one header panel open at a time
	 * (openHeaderDropdown). Waiting requests also show in the chat above the
	 * composer (PrivateAccessPrompt / the mobile prompt dock), whose Review opens
	 * this panel through chatAccessSummary.open.
	 *
	 * A subagent's chat mounts none of this. Its requests, one-shot approvals
	 * and grants arrive in its main chat's snapshot carrying `session_id` (the
	 * subagent's session, where every answer is sent) and `subagent` (its
	 * title, shown on the card).
	 */
	import { onDestroy, onMount, tick, untrack } from 'svelte';
	import { Shield, ShieldAlert, ShieldCheck, X } from 'lucide-svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { auth } from '$lib/stores/auth.svelte';
	import { resourceSync } from '$lib/services/resource-sync';
	import { chatAccessSummary, setPrivateAccessPending, type PendingAccessItem } from '$lib/stores/chat-access';
	import { subagentChats } from '$lib/stores/subagent-chats';
	import { openHeaderDropdown, openDropdown, closeDropdown } from '$lib/stores/header-dropdowns';
	import { buildShell } from '$lib/utils/platform';
	import { focusableWithin } from '$lib/components/ui/focus-trap';
	import PrivateAccessRequest from './PrivateAccessRequest.svelte';
	import '../deck/cards/card-design-system.css';
	import './private-access.css';
	let { sessionId }: { sessionId: string } = $props();
	/** Set on a subagent's item shown in its main chat: its session and chat title. */
	type FromSubagent = { session_id?: string; subagent?: string };
	type Grant = FromSubagent & { id: string; label: string; kind: string; target: string; expires_at: number; writable: boolean; fingerprint?: string };
	type Action = FromSubagent & { id: string; grant_id: string; operation: string; arguments: Record<string, string>; expires_at: number };
	type Request = FromSubagent & { id: string; kind: string; purpose: string; expires_at: number; suggested_path?: string; service_provider?: string; suggested_url?: string; plugin?: string; service?: string };
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
	let clearSecretsKey = $state(0);
	const pendingCount = $derived(data.requests.length + data.actions.length);
	let error = $state('');
	const hasContent = $derived(pendingCount > 0 || data.grants.length > 0 || data.saved_connections.length > 0 || !!error);

	// ── Panel ──────────────────────────────────────────────────────────────
	// Opening belongs to the user: refreshes, streaming and new requests never
	// open it (a waiting request shows in the chat and on the icon instead).
	const panelOpen = $derived($openHeaderDropdown === 'private-access');
	const panelId = $props.id();
	let triggerRef = $state<HTMLButtonElement | null>(null);
	let panelRef = $state<HTMLDivElement | null>(null);
	// An open from outside the panel (the chat's Review button) happens during
	// that click, before the window click listener runs; this marks the event
	// turn so the outside-click check does not close what was just opened (or,
	// when it is already open, what Review asked to show).
	let openedFromOutside = false;
	function openPanel() {
		if (!hasContent) return;
		openedFromOutside = true;
		setTimeout(() => { openedFromOutside = false; }, 0);
		openDropdown('private-access');
	}
	function closePanel(restoreFocus = false) {
		cancelSheetDrag();
		const focusInside = !!panelRef && panelRef.contains(document.activeElement);
		closeDropdown('private-access');
		if (restoreFocus || focusInside) triggerRef?.focus({ preventScroll: true });
	}
	const togglePanel = () => (panelOpen ? closePanel() : openPanel());
	// Every way of closing (button, Escape, outside click, another header panel
	// opening) drops typed secrets and unconfirmed consent, as the old dialog's
	// close did. Non-secret progress (a service setup step) is kept so a pending
	// preparation can be reopened where it was left.
	let wasOpen = false;
	$effect(() => {
		const open = panelOpen;
		untrack(() => {
			if (wasOpen && !open) { clearSecretsKey++; cancelSheetDrag(); }
			if (!wasOpen && open) void tick().then(() => { if (panelOpen) { placePanel(); panelRef?.focus({ preventScroll: true }); } });
			wasOpen = open;
		});
	});
	// Nothing left to show (the last request answered, access revoked): close.
	$effect(() => {
		if (!hasContent && untrack(() => panelOpen)) closeDropdown('private-access');
	});
	onDestroy(() => closeDropdown('private-access'));

	// Native mobile builds are always a sheet; web windows use one on phones
	// and coarse-pointer tablets (the same rule as the Git panel).
	let sheetLayout = $state(buildShell() === 'mobile');
	onMount(() => {
		if (typeof window.matchMedia !== 'function') return;
		const media = window.matchMedia('(max-width: 700px), (pointer: coarse)');
		const update = () => { sheetLayout = buildShell() === 'mobile' || media.matches; };
		update();
		media.addEventListener('change', update);
		return () => media.removeEventListener('change', update);
	});
	// The anchored panel hangs right-aligned under the icon, which sits left of
	// the Git button. On a narrow desktop window a wide Git label can push its
	// left edge off-screen: shift it right just enough to stay 12px inside.
	let panelShift = $state(0);
	function placePanel() {
		const anchor = panelRef?.parentElement;
		if (!panelRef || !anchor || !panelOpen || sheetLayout) { panelShift = 0; return; }
		panelShift = Math.max(0, Math.round(12 - (anchor.getBoundingClientRect().right - panelRef.offsetWidth)));
	}
	$effect(() => { void sheetLayout; untrack(placePanel); });
	let sheetOffset = $state(0);
	let draggingSheet = $state(false);
	let dragPointer: number | null = null;
	let dragStartY = 0;
	let suppressHandleClick = false;
	function startSheetDrag(e: PointerEvent) {
		if (!e.isPrimary || e.button !== 0) return;
		dragPointer = e.pointerId;
		dragStartY = e.clientY;
		suppressHandleClick = false;
		draggingSheet = true;
		(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
	}
	function moveSheetDrag(e: PointerEvent) {
		if (e.pointerId !== dragPointer) return;
		const distance = e.clientY - dragStartY;
		if (Math.abs(distance) > 8) suppressHandleClick = true;
		sheetOffset = Math.max(0, distance);
	}
	function endSheetDrag(e: PointerEvent) {
		if (e.pointerId !== dragPointer) return;
		const dismiss = sheetOffset >= Math.min(120, (panelRef?.offsetHeight ?? 600) * 0.2);
		cancelSheetDrag();
		if (dismiss) closePanel(true);
	}
	function cancelSheetDrag() {
		dragPointer = null;
		draggingSheet = false;
		sheetOffset = 0;
	}
	function activateSheetHandle(e: MouseEvent) {
		// Suppress the click synthesized after a drag, not keyboard activation.
		if (suppressHandleClick && e.detail > 0) return;
		closePanel(true);
	}
	function handleClickOutside(e: MouseEvent) {
		if (!panelOpen || openedFromOutside) return;
		// The path captured at dispatch: a Keep/Revoke swap can detach the
		// clicked button from the panel before this window listener runs.
		const path = e.composedPath();
		if ((panelRef && path.includes(panelRef)) || (triggerRef && path.includes(triggerRef))) return;
		closePanel();
	}
	function handleKeydown(event: KeyboardEvent) {
		if (!panelOpen) return;
		if (event.key === 'Escape') {
			// Keep Escape away from global chat shortcuts (stop generation etc.).
			event.preventDefault();
			event.stopImmediatePropagation();
			closePanel(true);
			return;
		}
		// The phone sheet is modal (scrim behind it): keep Tab inside it.
		if (event.key === 'Tab' && sheetLayout && panelRef) {
			const focusable = focusableWithin(panelRef);
			if (!focusable.length) return;
			const first = focusable[0], last = focusable[focusable.length - 1];
			const active = document.activeElement;
			const outside = !panelRef.contains(active);
			if (event.shiftKey && (active === first || active === panelRef || outside)) { event.preventDefault(); last.focus(); }
			else if (!event.shiftKey && (active === last || active === panelRef || outside)) { event.preventDefault(); first.focus(); }
		}
	}

	// ── Indicator ──────────────────────────────────────────────────────────
	const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
	const status = $derived(
		pendingCount ? `${pendingCount} waiting for you`
			: error ? 'unavailable'
			: data.grants.length ? `${data.grants.length} active`
			: ''
	);
	const triggerLabel = $derived(status ? `Private access: ${status}` : 'Private access');
	const panelSummary = $derived(
		[
			pendingCount && `${pendingCount} waiting for you`,
			data.grants.length && `${data.grants.length} active`,
			data.saved_connections.length && plural(data.saved_connections.length, 'saved connection')
		].filter(Boolean).join(' · ') || (error ? 'Unavailable right now' : 'Nothing active')
	);
	const expiry = (at: number) => new Date(at * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
	// What is waiting, for the chat's own prompt (PrivateAccessPrompt, #1807).
	const items = $derived<PendingAccessItem[]>([
		...data.requests.map((r) => ({
			id: r.id,
			kind: (['machine', 'folder', 'service'].includes(r.kind) ? r.kind : 'machine') as PendingAccessItem['kind'],
			detail: r.purpose,
			subagent: r.subagent
		})),
		...data.actions.map((a) => ({
			id: a.id,
			kind: 'action' as const,
			detail: a.operation === 'command' ? `$ ${a.arguments.command ?? ''}`
				: a.operation === 'http' ? `${a.arguments.method ?? ''} ${a.arguments.path ?? ''}`
				: `Write ${a.arguments.path ?? ''}`,
			subagent: a.subagent
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
			open: openPanel,
			decide
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
	/** Where an item's answer goes: a subagent's item belongs to the subagent's session. */
	const baseFor = (owning: string | undefined) => (owning && owning !== sessionId ? `/chat-access/${encodeURIComponent(owning)}` : base);
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
				const session = await api.get<{mode: string; is_remote?: boolean; device_name?: string | null; is_worker?: boolean}>(`/sessions/${encodeURIComponent(sessionId)}`);
				if (!alive) return;
				// A subagent's chat shows no private access: its items show in its
				// main chat. A deep link can mount this before /live says so.
				if (session.is_worker) subagentChats.mark(sessionId, true);
				// A mirrored conversation's grants stay on the device that runs it;
				// this worker forwards the panel's requests there.
				enabled = !session.is_worker && (session.mode == null || ['code', 'chat', 'voice'].includes(session.mode));
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
		try { await api.delete(`${baseFor(data.grants.find((g) => g.id === id)?.session_id)}/grants/${id}`); confirmingRevoke = null; await refresh(); }
		catch (e) { error = getErrorMessage(e, 'Could not revoke access.'); }
		finally { busy = false; }
	}
	async function decide(id: string, allow: boolean): Promise<boolean> {
		busy = true; error = '';
		try { await api.post(`${baseFor(data.actions.find((a) => a.id === id)?.session_id)}/actions/${id}`, {allow}); await refresh(); return true; }
		catch (e) { error = getErrorMessage(e, 'Could not answer this request.'); return false; }
		finally { busy = false; }
	}
</script>


<svelte:window onkeydowncapture={handleKeydown} onclick={handleClickOutside} onresize={placePanel} />

{#snippet serverIcon()}
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="4" width="18" height="7" rx="2" /><rect x="3" y="13" width="18" height="7" rx="2" /><path stroke-linecap="round" d="M7 7.5h.01M7 16.5h.01" /></svg>
{/snippet}
{#snippet serviceIcon()}
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.7 5.7L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.6a1 1 0 01.3-.7l6-6A6 6 0 1121 9z" /></svg>
{/snippet}
{#snippet fromSubagent(name: string | undefined)}
	{#if name}<p class="pa-from" title={`Asked by subagent ${name}`}>Asked by subagent <strong>{name}</strong></p>{/if}
{/snippet}
{#snippet folderIcon()}
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linejoin="round" d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" /></svg>
{/snippet}

{#if hasContent}
<div class="pa-anchor" class:sheet-layout={sheetLayout}>
	<!-- Icon-sized like the header's other chat controls. It speaks only when
	     something is happening: a count for requests/approvals waiting, a dot
	     for active access or an error, nothing for saved connections alone. -->
	<button
		bind:this={triggerRef}
		type="button"
		class="pa-trigger"
		class:open={panelOpen}
		class:waiting={pendingCount > 0}
		onclick={togglePanel}
		aria-label={triggerLabel}
		title={triggerLabel}
		aria-haspopup="dialog"
		aria-expanded={panelOpen}
		aria-controls={panelId}
	>
		<span class="pa-glyph">
			{#if error && !pendingCount}<ShieldAlert size={16} strokeWidth={1.5} />
			{:else if data.grants.length}<ShieldCheck size={16} strokeWidth={1.5} />
			{:else}<Shield size={16} strokeWidth={1.5} />{/if}
			{#if pendingCount}
				<span class="pa-count" aria-hidden="true">{pendingCount > 9 ? '9+' : pendingCount}</span>
			{:else if error}
				<span class="pa-dot error" aria-hidden="true"></span>
			{:else if data.grants.length}
				<span class="pa-dot active" aria-hidden="true"></span>
			{/if}
		</span>
	</button>

	{#if panelOpen}
		<!-- Only shown in the bottom-sheet layout (see CSS). -->
		<div class="pa-scrim" aria-hidden="true"></div>
	{/if}
	<!-- Stays mounted while closed so a half-finished service setup can be
	     reopened where it was left; closing clears secrets (clearSecretsKey). -->
	<div
		bind:this={panelRef}
		id={panelId}
		class="pa-pop card-system"
		class:dragging-sheet={draggingSheet}
		style:--sheet-offset={`${sheetOffset}px`}
		style:right={panelShift ? `${-panelShift}px` : undefined}
		hidden={!panelOpen}
		role="dialog"
		aria-modal={sheetLayout ? 'true' : undefined}
		aria-labelledby="{panelId}-title"
		tabindex="-1"
	>
		<button
			type="button"
			class="sheet-handle"
			aria-label="Close private access (drag down or tap)"
			onpointerdown={startSheetDrag}
			onpointermove={moveSheetDrag}
			onpointerup={endSheetDrag}
			onpointercancel={cancelSheetDrag}
			onlostpointercapture={cancelSheetDrag}
			onclick={activateSheetHandle}
		><span></span></button>
		<header class="pa-pop-head">
			<span class="pa-pop-icon" aria-hidden="true"><Shield size={15} strokeWidth={1.6} /></span>
			<div class="pa-pop-titles">
				<h2 id="{panelId}-title">Private access</h2>
				<p>{panelSummary}{#if owner} · on {owner}{/if}</p>
			</div>
			<button type="button" class="pa-pop-close" aria-label="Close private access" onclick={() => closePanel(true)}><X size={16} /></button>
		</header>

		<div class="pa-pop-body" aria-label="Temporary access in this conversation">
			{#if error}<p class="pa-error" role="alert">{error}</p>{/if}

			{#if pendingCount}
				<h3 class="pa-group">Waiting for you</h3>
			{/if}
			{#each data.requests as request (request.id)}
				{@render fromSubagent(request.subagent)}
				<PrivateAccessRequest sessionId={request.session_id ?? sessionId} {request} savedConnections={data.saved_connections} vaultAvailable={data.vault_available} {owner} {clearSecretsKey} onComplete={() => void refresh()} />
			{/each}
			{#each data.actions as action (action.id)}
				{@const grant = data.grants.find(g => g.id === action.grant_id)}
				{@const sandboxed = action.operation === 'command' && grant?.kind === 'folder'}
				{@render fromSubagent(action.subagent)}
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

			{#if data.grants.length}
				<h3 class="pa-group">Active in this chat</h3>
				<ul class="pa-list" aria-label="Active access">
					{#each data.grants as grant (grant.id)}
						<li class="pa-row">
							<span class="pa-icon row-icon">{#if grant.kind === 'machine'}{@render serverIcon()}{:else if grant.kind === 'service'}{@render serviceIcon()}{:else}{@render folderIcon()}{/if}</span>
							<div class="pa-row-main">
								<strong>{grant.label}</strong>
								<code>{grant.target}</code>
								<small>{#if grant.subagent}{`Subagent ${grant.subagent} · `}{/if}{grant.kind === 'machine' ? 'Remote commands · session permission mode applies' : grant.kind === 'service' ? (grant.writable ? 'HTTPS API · changes follow session permission mode' : 'HTTPS API · GET and HEAD only') : grant.writable ? 'Read/write · session permission mode applies' : 'Read only'} · until {expiry(grant.expires_at)}{#if grant.kind === 'machine' && grant.fingerprint}{` · host key ${grant.fingerprint.slice(0, 19)}…`}{/if}</small>
							</div>
							<div class="pa-row-actions">
								{#if confirmingRevoke === grant.id}
									<button class="pa-small-btn" onclick={() => confirmingRevoke = null}>Keep</button>
									<button class="pa-small-btn danger solid" disabled={busy} onclick={() => revoke(grant.id)}>Revoke</button>
								{:else}
									<button class="pa-small-btn danger" aria-label="Revoke {grant.label}" onclick={() => confirmingRevoke = grant.id}>Revoke</button>
								{/if}
							</div>
						</li>
					{/each}
				</ul>
			{:else if !pendingCount && !error}
				<p class="pa-empty">Nothing is shared with this chat right now. When the agent needs a machine, folder or service, it asks here and in the chat.</p>
			{/if}

			{#if data.saved_connections.length}
				<h3 class="pa-group">Saved connections</h3>
				<ul class="pa-list" aria-label="Saved connections">
					{#each data.saved_connections as saved (saved.id)}
						<li class="pa-row">
							<span class="pa-icon row-icon">{#if saved.kind === 'service'}{@render serviceIcon()}{:else}{@render serverIcon()}{/if}</span>
							<div class="pa-row-main">
								<strong>{saved.label}</strong>
								<code>{saved.target}</code>
								<small>Encrypted on {owner ?? 'this worker'}{#if saved.fingerprint}{` · host key ${saved.fingerprint.slice(0, 19)}…`}{/if}</small>
							</div>
							<div class="pa-row-actions">
								{#if confirmingDelete === saved.id}
									<button class="pa-small-btn" onclick={() => confirmingDelete = null}>Keep</button>
									<button class="pa-small-btn danger solid" disabled={busy} onclick={() => removeSaved(saved.id)}>Delete</button>
								{:else}
									<button class="pa-small-btn danger" aria-label="Delete {saved.label}" onclick={() => confirmingDelete = saved.id}>Delete</button>
								{/if}
							</div>
						</li>
					{/each}
				</ul>
			{/if}

			{#if data.grants.length || data.saved_connections.length}
				<p class="pa-hint pa-foot">Worker restart or expiry ends access. Revoking closes open connections but can’t undo changes already made. Deleting a saved connection also revokes access that uses it.</p>
			{/if}
		</div>
	</div>
</div>
{/if}

<style>
	/* ── Entry point: the header's icon-button size (ChatExportActions) ──── */
	.pa-anchor { position: relative; display: flex; align-items: center; }
	/* Who asked, above a subagent's card in its main chat. */
	.pa-from { margin: 0 0 -0.25rem; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.75rem; color: var(--muted-foreground); }
	.pa-from strong { font-weight: 600; color: var(--foreground); }
	.pa-trigger {
		display: grid;
		place-items: center;
		width: 28px;
		height: 28px;
		flex-shrink: 0;
		padding: 0;
		border: 0;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background-color 150ms ease, color 150ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.pa-trigger:hover { background: var(--hover-overlay); color: var(--foreground); }
	.pa-trigger:active { transform: translateY(1px); }
	.pa-trigger:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.pa-trigger.waiting { color: var(--foreground); }
	/* Open reads like the header's other icon toggles (the notification bell):
	   a neutral overlay. Theme accents (cyan, neon, saturated blue in several
	   presets) filled the whole icon square and hid the glyph. */
	.pa-trigger.open, .pa-trigger.open:hover { background: var(--active-overlay, var(--hover-overlay)); color: var(--foreground); }
	.pa-glyph { position: relative; display: grid; place-items: center; width: 16px; height: 16px; }
	/* The ring in the header's own colour keeps the mark legible on the icon. */
	.pa-count {
		position: absolute;
		top: -7px;
		left: 9px;
		box-sizing: border-box;
		min-width: 15px;
		height: 15px;
		padding: 0 4px;
		border-radius: 999px;
		background: var(--warning);
		color: var(--warning-foreground);
		box-shadow: 0 0 0 2px var(--background);
		font-size: 0.59375rem;
		font-weight: 700;
		line-height: 15px;
		text-align: center;
		font-variant-numeric: tabular-nums;
	}
	/* A short ping when something starts waiting; it settles, never loops. */
	.pa-count::after {
		content: '';
		position: absolute;
		inset: -2px;
		border-radius: inherit;
		border: 1.5px solid var(--warning);
		opacity: 0;
		animation: pa-ping 1.4s ease-out 3;
	}
	@keyframes pa-ping {
		0% { opacity: 0.8; transform: scale(1); }
		100% { opacity: 0; transform: scale(1.8); }
	}
	.pa-dot {
		position: absolute;
		top: -2px;
		right: -3px;
		width: 7px;
		height: 7px;
		border-radius: 50%;
		box-shadow: 0 0 0 2px var(--background);
	}
	.pa-dot.active { background: var(--success); }
	.pa-dot.error { background: var(--destructive); }

	/* ── Panel: anchored under the icon (the Git panel's shape) ──────────── */
	.pa-pop {
		position: absolute;
		top: calc(100% + 8px);
		right: 0;
		z-index: 150;
		box-sizing: border-box;
		display: flex;
		flex-direction: column;
		width: min(30rem, calc(100vw - 24px));
		max-height: min(calc(100dvh - 80px), 46rem);
		overflow: hidden;
		background: var(--popover, var(--card));
		color: var(--popover-foreground, var(--foreground));
		border: 1px solid var(--border-strong, var(--border));
		border-radius: calc(var(--radius, 0.75rem) * 1.2);
		box-shadow: var(--shadow-l);
		text-align: left;
		animation: pa-pop-in 160ms cubic-bezier(0.2, 0.7, 0.2, 1);
		transform-origin: top right;
	}
	.pa-pop[hidden] { display: none; }
	.pa-pop:focus { outline: none; }
	@keyframes pa-pop-in {
		from { opacity: 0; transform: translateY(-4px) scale(0.985); }
		to { opacity: 1; transform: none; }
	}
	.pa-scrim, .sheet-handle { display: none; }

	.pa-pop-head {
		flex: none;
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 8px 10px 14px;
		border-bottom: 1px solid var(--border);
	}
	.pa-pop-icon {
		flex: none;
		display: grid;
		place-items: center;
		width: 28px;
		height: 28px;
		border-radius: calc(var(--radius, 0.75rem) * 0.6);
		background: var(--muted);
		color: var(--foreground);
	}
	.pa-pop-titles { flex: 1; min-width: 0; }
	.pa-pop-titles h2 { margin: 0; font-size: 0.8125rem; font-weight: 600; line-height: 1.3; }
	.pa-pop-titles p {
		margin: 1px 0 0;
		font-size: 0.6875rem;
		line-height: 1.4;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pa-pop-close {
		flex: none;
		display: grid;
		place-items: center;
		width: 32px;
		height: 32px;
		border: 0;
		border-radius: 0.5rem;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
	}
	.pa-pop-close:hover { background: var(--hover-overlay); color: var(--foreground); }
	.pa-pop-close:focus-visible, .sheet-handle:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	.pa-pop-body {
		min-width: 0;
		min-height: 0;
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		grid-auto-rows: max-content;
		align-content: start;
		gap: 0.625rem;
		padding: 0.75rem;
		overflow: auto;
		overscroll-behavior: contain;
	}
	/* Cards sit on the panel, not floating over the chat: no second shadow. */
	.pa-pop-body :global(.pa-card) { box-shadow: none; }
	.pa-group {
		margin: 0.25rem 0.25rem -0.125rem;
		font-size: 0.6875rem;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--muted-foreground);
	}
	.pa-group:first-of-type { margin-top: 0; }
	.pa-list {
		margin: 0;
		padding: 0;
		list-style: none;
		border: 1px solid var(--border);
		border-radius: 0.875rem;
		background: var(--card);
		overflow: hidden;
	}
	.pa-list > .pa-row:first-child { border-top: 0; }
	.row-icon { width: 2rem; height: 2rem; border-radius: 0.5rem; }
	.row-icon :global(svg) { width: 0.9rem; height: 0.9rem; }
	.pa-empty {
		margin: 0;
		padding: 0.75rem 0.875rem;
		border: 1px dashed var(--border);
		border-radius: 0.875rem;
		font-size: 0.75rem;
		line-height: 1.5;
		color: var(--muted-foreground);
	}
	.pa-foot { padding: 0 0.25rem; }

	/* ── Phones, touch screens and the native shell: a bottom sheet ──────── */
	@media (max-width: 639px) { .pa-trigger { width: 44px; height: 44px; } }
	.sheet-layout .pa-trigger { width: 44px; height: 44px; }
	.sheet-layout .pa-scrim {
		display: block;
		position: fixed;
		inset: 0;
		z-index: 140;
		background: rgb(0 0 0 / 0.5);
		animation: pa-scrim-in 180ms ease;
	}
	@keyframes pa-scrim-in { from { opacity: 0; } to { opacity: 1; } }
	.sheet-layout .pa-pop {
		position: fixed;
		top: auto;
		left: 0;
		right: 0;
		bottom: 0;
		width: auto;
		max-height: 88dvh;
		border-bottom: 0;
		border-radius: 18px 18px 0 0;
		padding-bottom: env(safe-area-inset-bottom);
		transform-origin: bottom center;
		transform: translateY(var(--sheet-offset, 0px));
		transition: transform 180ms ease;
		animation: pa-sheet-in 220ms cubic-bezier(0.2, 0.7, 0.2, 1);
	}
	.sheet-layout .pa-pop.dragging-sheet { animation: none; transition: none; }
	@keyframes pa-sheet-in {
		from { transform: translateY(24px); opacity: 0; }
		to { transform: none; opacity: 1; }
	}
	/* A real pointer target, separate from the scrollable content. */
	.sheet-layout .sheet-handle {
		flex: none;
		display: grid;
		place-items: center;
		width: 100%;
		height: 28px;
		padding: 0;
		border: 0;
		background: transparent;
		cursor: grab;
		touch-action: none;
		user-select: none;
	}
	.sheet-layout .dragging-sheet .sheet-handle { cursor: grabbing; }
	.sheet-layout .sheet-handle span { width: 36px; height: 4px; border-radius: 2px; background: var(--border-strong, var(--border)); }
	.sheet-layout .pa-pop-head { padding: 2px 8px 10px 16px; }
	.sheet-layout .pa-pop-titles h2 { font-size: 0.9375rem; }
	.sheet-layout .pa-pop-titles p { font-size: 0.75rem; white-space: normal; }
	.sheet-layout .pa-pop-close { width: 44px; height: 44px; }
	.sheet-layout .pa-pop-body { padding: 0.75rem 0.75rem 1rem; }

	@media (prefers-reduced-motion: reduce) {
		.pa-trigger { transition: none; }
		.pa-count::after { animation: none; }
		.pa-pop, .pa-scrim, .sheet-layout .pa-pop, .sheet-layout .pa-scrim { animation: none; transition: none; }
	}
</style>
