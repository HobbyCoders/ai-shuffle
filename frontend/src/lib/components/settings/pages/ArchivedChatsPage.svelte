<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { ArchiveRestore, ArrowUpRight, RefreshCw } from 'lucide-svelte';
	import { api } from '$lib/api/client';
	import type { Session, CursorPaginatedSessions } from '$lib/api/sessions';
	import { appData } from '$lib/stores/app-data.svelte';
	import { openSession } from '$lib/services/session-navigation';
	import { getSettingsAccess, isForbidden, FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import PreferencePage from '../shell/PreferencePage.svelte';

	const access = getSettingsAccess();
	let chats = $state<Session[]>([]);
	let loading = $state(false);
	let loaded = $state(false);
	let hasMore = $state(false);
	let cursor: string | null = null;
	let cursorId: string | null = null;
	let error = $state('');
	let restoring = $state<string | null>(null);
	let disposed = false;
	onDestroy(() => { disposed = true; });
	onMount(() => { void load(true); });

	async function load(reset = false) {
		if (loading || restoring) return;
		loading = true;
		error = '';
		const query = new URLSearchParams({ status: 'archived', admin_only: 'true', limit: '50', sync_claude_sessions: 'false' });
		if (!reset && cursor && cursorId) {
			query.set('cursor', cursor);
			query.set('cursor_id', cursorId);
		}
		try {
			const result = await api.get<CursorPaginatedSessions>(`/sessions/cursor?${query}`);
			if (disposed) return;
			const rows = reset ? [] : chats;
			chats = [...new Map([...rows, ...result.items].map(chat => [chat.id, chat])).values()];
			hasMore = result.has_more && !!result.next_cursor && !!result.next_cursor_id &&
				(reset || result.next_cursor !== cursor || result.next_cursor_id !== cursorId);
			cursor = result.next_cursor;
			cursorId = result.next_cursor_id;
			loaded = true;
		} catch (err) {
			if (!disposed) error = isForbidden(err) ? FORBIDDEN_TEXT : 'Could not load archived chats. Please try again.';
		} finally {
			if (!disposed) loading = false;
		}
	}

	// The shared path sets the chat's mode and closes an overlaid sidebar
	// before switching, like every other way of opening a chat.
	function openChat(chat: Session) {
		openSession(chat.id, chat);
	}

	async function restore(chat: Session) {
		if (restoring || loading || access.readOnly) return;
		restoring = chat.id;
		error = '';
		try {
			await appData.setSessionArchived(chat.id, false);
			if (!disposed) chats = chats.filter(row => row.id !== chat.id);
		} catch (err) {
			if (!disposed) {
				const detail = (err as { detail?: unknown })?.detail;
				error = typeof detail === 'string' ? detail : 'Could not restore this chat. Please try again.';
			}
		} finally {
			if (!disposed) restoring = null;
		}
	}
</script>

<PreferencePage category="archived">
	<p class="intro">Read archived conversations here. Restore a chat to move it back to Settled.</p>
	<div class="toolbar">
		<span>{chats.length} loaded{hasMore ? ' · more available' : ''}</span>
		<button onclick={() => load(true)} disabled={loading || !!restoring} aria-label="Refresh archived chats"><RefreshCw size={15} /> Refresh</button>
	</div>
	{#if error}<p role="alert" class="error">{error}</p>{/if}
	{#if loading}<p role="status">Loading archived chats…</p>{/if}
	{#if loaded && !loading && !error && chats.length === 0}<p class="empty">No archived chats.</p>{/if}
	<ul aria-label="Archived chats">
		{#each chats as chat (chat.id)}
			<li>
				<button class="chat" onclick={() => openChat(chat)} aria-label={`Read ${chat.title || 'Untitled chat'}`}>
					<span><strong>{chat.title || 'Untitled chat'}</strong><small>{[chat.project_name, chat.device_name, chat.worktree_branch || chat.branch].filter(Boolean).join(' · ') || 'Conversation'}</small></span>
					<ArrowUpRight size={17} />
				</button>
				<button class="restore" onclick={() => restore(chat)} disabled={loading || !!restoring || access.readOnly} aria-label={`Restore ${chat.title || 'Untitled chat'}`}>
					<ArchiveRestore size={16} /> {restoring === chat.id ? 'Restoring…' : 'Restore'}
				</button>
			</li>
		{/each}
	</ul>
	{#if hasMore}<button class="more" onclick={() => load()} disabled={loading || !!restoring}>Load older archived chats</button>{/if}
</PreferencePage>

<style>
	.intro, .empty, .toolbar, small { color: var(--muted-foreground); }
	.intro { font-size: 13px; line-height: 1.6; margin: 0 0 20px; }
	.toolbar { display: flex; justify-content: space-between; align-items: center; gap: 8px; font-size: 12px; margin-bottom: 12px; }
	button { display: flex; align-items: center; justify-content: center; gap: 6px; min-height: 44px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: transparent; color: var(--foreground); cursor: pointer; font-size: 12px; }
	button:hover:not(:disabled) { background: var(--muted); }
	button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	button:disabled { opacity: .5; cursor: default; }
	ul { list-style: none; padding: 0; margin: 0; }
	li { display: flex; gap: 8px; align-items: center; border-bottom: 1px solid var(--border); padding: 8px 0; }
	.chat { flex: 1; min-width: 0; justify-content: space-between; text-align: left; border-color: transparent; }
	.chat span { min-width: 0; }
	strong, small { display: block; overflow-wrap: anywhere; }
	strong { font-size: 13px; font-weight: 550; }
	small { font-size: 11px; margin-top: 4px; }
	.restore { flex-shrink: 0; }
	.more { width: 100%; margin-top: 16px; }
	.error { color: var(--destructive); font-size: 13px; }
</style>
