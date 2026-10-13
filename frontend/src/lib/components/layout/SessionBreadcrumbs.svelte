<script lang="ts">
	import { ChevronRight } from 'lucide-svelte';
	import { api } from '$lib/api/client';
	import type { Session } from '$lib/api/sessions';
	import { openSessionLink, sessionHref } from '$lib/services/session-navigation';
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

<!--
	`mobile` is the phone bar's second line: the trail only (project › parent).
	The bar draws the chat title on its first line and the chat-actions chevron
	beside it (SessionTitleMenu variant="sheet").
-->
<nav class="crumbs" class:mobile class:has-parent={!!parentId} aria-label="Breadcrumb">
	{#if projectLabel}
		{#if onOpenProject}
			<button class="crumb project" title={projectLabel} onclick={onOpenProject}>{projectLabel}</button>
		{:else}
			<span class="crumb project" title={projectLabel}>{projectLabel}</span>
		{/if}
		{#if mobile ? !!parentId : !!title}<ChevronRight class="crumb-sep" size={mobile ? 11 : 14} aria-hidden="true" />{/if}
	{/if}
	{#if parentId}
		{#if parent}
			<a class="crumb parent" href={mobile ? sessionHref(parentId, '/mobile/') : sessionHref(parentId)} onclick={(event) => openSessionLink(event, parentId!)} data-sveltekit-reload title={parent.title || 'Parent chat'} aria-label={`Open parent chat: ${parent.title || 'Untitled chat'}`}>{parent.title || 'Parent chat'}</a>
		{:else}
			<span class="crumb parent" title="Parent chat unavailable">Parent chat</span>
		{/if}
		{#if !mobile}<ChevronRight class="crumb-sep" size={14} aria-hidden="true" />{/if}
	{/if}
	{#if title && !mobile}
		<div class="current" aria-current="page">
			<SessionTitleMenu {title} {sessionId} variant="crumb" />
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
	/* Phone bar subline: 11.5px muted text sitting on the bar's baseline. The
	   parent link stays a 44px-tall tap target whose text rides at the bottom. */
	.crumbs.mobile { flex: 0 1 auto; gap: 4px; align-items: flex-end; height: 44px; min-width: 0; }
	.crumbs.mobile .crumb { font-size: 11.5px; line-height: 16px; max-width: none; margin-bottom: 7px; }
	.crumbs.mobile .project { flex: 0 1 auto; min-width: 2.5em; }
	/* Beside a parent link the project keeps its name (up to 45% of the
	   trail) and the parent's title absorbs the shortfall. */
	.crumbs.mobile.has-parent .project { flex: 0 0 auto; max-width: 45%; }
	.crumbs.mobile .parent { flex: 0 1 auto; min-width: 3em; }
	.crumbs.mobile.has-parent { flex: 1 1 0; }
	.crumbs.mobile a.crumb { display: block; min-height: 44px; margin-bottom: 0; padding: 21px 0 7px; box-sizing: border-box; color: var(--foreground); font-weight: 500; }
	.crumbs.mobile a.crumb:hover { text-decoration: underline; }
	.crumbs.mobile :global(.crumb-sep) { width: 11px; height: 16px; margin-bottom: 7px; }
</style>
