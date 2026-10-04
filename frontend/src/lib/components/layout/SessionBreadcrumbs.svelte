<script lang="ts">
	import { ChevronRight } from 'lucide-svelte';
	import { api } from '$lib/api/client';
	import type { Session } from '$lib/api/sessions';
	import { openSessionLink } from '$lib/services/session-navigation';
	import { sessions } from '$lib/stores/app-data.svelte';
	import SessionTitleMenu from './SessionTitleMenu.svelte';

	interface Props {
		projectLabel?: string | null;
		title?: string | null;
		sessionId?: string | null;
		onOpenProject?: () => void;
		mobile?: boolean;
	}
	let { projectLabel = null, title = null, sessionId = null, onOpenProject, mobile = false }: Props = $props();
	let loadedChild = $state<Session | null>(null);
	let loadedParent = $state<Session | null>(null);
	const child = $derived($sessions.find(s => s.id === sessionId) ?? (loadedChild?.id === sessionId ? loadedChild : null));
	// Delegated transcripts are list-hidden; a normal fork is not a subagent.
	const parentId = $derived(child?.hidden && child.parent_session_id !== sessionId ? child.parent_session_id : null);
	const parent = $derived($sessions.find(s => s.id === parentId) ?? (loadedParent?.id === parentId ? loadedParent : null));

	$effect(() => {
		const id = sessionId;
		if (!id || child) return;
		let cancelled = false;
		// Hidden transcripts and deep links may have no sidebar row. Limit the
		// history payload: this request needs identity, not another transcript.
		void api.get<Session>(`/sessions/${encodeURIComponent(id)}?message_limit=1`)
			.then(row => { if (!cancelled) loadedChild = row; })
			.catch(() => { /* Keep the current title when metadata is unavailable. */ });
		return () => { cancelled = true; };
	});
	$effect(() => {
		const id = parentId;
		if (!id || parent) return;
		let cancelled = false;
		void api.get<Session>(`/sessions/${encodeURIComponent(id)}?message_limit=1`)
			.then(row => { if (!cancelled) loadedParent = row; })
			.catch(() => { /* A deleted or inaccessible parent is not a navigation target. */ });
		return () => { cancelled = true; };
	});
</script>

<nav class="crumbs" class:mobile class:has-parent={!!parentId} aria-label="Breadcrumb">
	{#if projectLabel}
		{#if onOpenProject}
			<button class="crumb project" title={projectLabel} onclick={onOpenProject}>{projectLabel}</button>
		{:else}
			<span class="crumb project" title={projectLabel}>{projectLabel}</span>
		{/if}
		{#if title}<ChevronRight class="crumb-sep" size={14} aria-hidden="true" />{/if}
	{/if}
	{#if parentId}
		{#if parent}
			<a class="crumb parent" href={`/?session=${encodeURIComponent(parentId)}`} onclick={(event) => openSessionLink(event, parentId!)} data-sveltekit-reload title={parent.title || 'Parent chat'} aria-label={`Open parent chat: ${parent.title || 'Untitled chat'}`}>{parent.title || 'Parent chat'}</a>
		{:else}
			<span class="crumb parent" title="Parent chat unavailable">Parent chat</span>
		{/if}
		<ChevronRight class="crumb-sep" size={14} aria-hidden="true" />
	{/if}
	{#if title}
		<div class="current" aria-current="page">
			<SessionTitleMenu {title} {sessionId} variant={mobile ? 'mobile' : 'crumb'} />
		</div>
	{/if}
</nav>

<style>
	/* The nav spans the strip's free width so the crumb caps below are shares of
	   the space actually available, not of the crumbs' own text. */
	.crumbs { display: flex; align-items: center; gap: 7px; min-width: 0; flex: 1 1 auto; }
	.crumb { min-width: 0; max-width: 220px; border: none; background: transparent; padding: 0; font: inherit; font-size: 13px; color: var(--muted-foreground); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-decoration: none; }
	button.crumb, a.crumb { cursor: pointer; }
	button.crumb:hover, a.crumb:hover { color: var(--foreground); text-decoration: underline; }
	.crumb:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; border-radius: 4px; }
	.crumbs :global(.crumb-sep) { color: var(--subtle-foreground); flex: none; }
	.current { min-width: 0; flex: 0 1 auto; }
	/* The leading crumbs keep their own text width and give way only past a
	   share of the strip; the current title absorbs any shortfall first. A
	   fixed 220px basis on the parent used to reserve empty space after short
	   names and squeeze "Sessions" to "Se…" beside it (#1799). */
	.project, .parent { flex: none; max-width: min(220px, 25%); }
	.project { max-width: min(220px, max(25%, 4.5rem)); }
	.mobile { flex: 1; }
	.mobile:not(.has-parent) { justify-content: center; }
	@media (max-width: 700px) {
		.crumbs { gap: 3px; flex: 1; }
		.project { max-width: 22%; font-size: 11px; }
		.parent { flex: 1 1 0; font-size: 12px; }
		.current { flex: 1 1 0; }
		a.crumb, button.crumb { min-height: 44px; align-content: center; }
		.current :global(button.title-trigger) { min-height: 44px; margin-block: 0; }
		.current :global(.menu) { left: auto; right: 0; }
		.crumbs :global(.crumb-sep) { width: 10px; }
	}
</style>
