<script lang="ts">
	/**
	 * ProfileList — List view with search/filter for profiles
	 */
	import type { Profile } from '$lib/api/client';
	import { Search, Download, Trash2, ChevronRight, RotateCcw } from 'lucide-svelte';

	interface GroupState {
		profiles?: {
			groups?: { name: string }[];
			assignments?: Record<string, string>;
		};
	}

	interface Subagent {
		id: string;
		name: string;
		description: string;
		model?: string;
		is_builtin: boolean;
	}

	interface Props {
		profiles: Profile[];
		groups: GroupState;
		allSubagents: Subagent[];
		builtinAgentIds: string[];
		onEditProfile: (profile: Profile) => void;
		onExportProfile: (profileId: string) => void;
		onDeleteProfile: (profileId: string) => void;
		onRevertProfile: (profileId: string) => void;
		getModelDisplay: (model?: string) => string;
		getProfileToolCount: (profile: Profile) => number;
	}

	let {
		profiles,
		groups,
		allSubagents,
		builtinAgentIds,
		onEditProfile,
		onExportProfile,
		onDeleteProfile,
		onRevertProfile,
		getModelDisplay,
		getProfileToolCount
	}: Props = $props();

	let searchQuery = $state('');
	let deletingId = $state<string | null>(null);
	let deleteLoading = $state(false);

	const filteredProfiles = $derived(
		searchQuery.trim()
			? profiles.filter(
					(p) =>
						p.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
						(p.description && p.description.toLowerCase().includes(searchQuery.toLowerCase())) ||
						p.id.toLowerCase().includes(searchQuery.toLowerCase())
				)
			: profiles
	);

	async function confirmDeleteProfile() {
		if (!deletingId) return;
		deleteLoading = true;
		try {
			await onDeleteProfile(deletingId);
			deletingId = null;
		} catch (err: any) {
			console.error('Failed to delete profile:', err);
		} finally {
			deleteLoading = false;
		}
	}
</script>

<div class="fp-search-bar">
	<div class="fph-search-wrapper">
		<Search size={14} />
		<input type="text" bind:value={searchQuery} placeholder="Search profiles..." class="fph-search-input" />
	</div>
</div>

{#if filteredProfiles.length === 0}
	<div class="card-empty-state">
		{#if searchQuery}
			<p class="card-empty-title">No profiles match your search</p>
		{:else}
			<svg class="card-empty-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
			</svg>
			<p class="card-empty-title">No profiles yet</p>
			<p class="card-empty-description">Create your first profile to get started</p>
		{/if}
	</div>
{:else}
	<div class="fp-list">
		{#each filteredProfiles as profile, index}
			{#if index > 0}<div class="fp-list-divider"></div>{/if}
			{#if deletingId === profile.id}
				<!-- Inline delete confirmation -->
				<div class="fp-list-item fp-list-item--deleting">
					<div class="fp-item-content">
						<p class="delete-confirm-text">
							Delete <strong>{profile.name}</strong>? This cannot be undone.
						</p>
						<div class="delete-confirm-actions">
							<button onclick={() => (deletingId = null)} disabled={deleteLoading} class="fp-btn fp-btn--secondary fp-btn--sm">Cancel</button>
							<button onclick={confirmDeleteProfile} disabled={deleteLoading} class="fp-btn fp-btn--destructive fp-btn--sm">
								{#if deleteLoading}<div class="card-spinner card-spinner--small"></div>{/if}
								Delete
							</button>
						</div>
					</div>
				</div>
			{:else}
				<div
					class="fp-list-item"
					onclick={() => onEditProfile(profile)}
					role="button"
					tabindex="0"
					onkeydown={(e) => e.key === 'Enter' && onEditProfile(profile)}
				>
					<div class="fp-item-indicator"></div>
					<div class="fp-item-content">
						<div class="fp-item-header">
							<span class="fp-item-name">{profile.name}</span>
							{#if profile.is_builtin}
								<span class="fp-item-badge">Built-in</span>
							{/if}
							{#if groups?.profiles?.assignments?.[profile.id]}
								<span class="fp-item-badge fp-item-badge--accent">{groups.profiles.assignments[profile.id]}</span>
							{/if}
							<!-- Slice F: highlight profiles that opt into SDK 0.1.81 strict MCP. -->
							{#if profile.config?.strict_mcp_config}
								<span class="fp-item-badge fp-item-badge--strict" title="MCP servers must validate cleanly or the session fails to start.">Strict MCP</span>
							{/if}
						</div>
						<div class="fp-item-meta">
							<span class="fp-meta-model">{getModelDisplay(profile.config?.model)}</span>
							<span class="fp-meta-sep">&middot;</span>
							<span class="fp-meta-chip">{getProfileToolCount(profile)} tools</span>
							<span class="fp-meta-sep">&middot;</span>
							<span class="fp-meta-chip">{Math.max((profile.config?.enabled_agents?.filter((id: string) => allSubagents.some(a => a.id === id))?.length) || 0, builtinAgentIds.length)} agents</span>
							<span class="fp-meta-sep">&middot;</span>
							<span class="fp-meta-chip">{profile.session_count || 0} sessions</span>
						</div>
					</div>
					<div class="fp-item-actions" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
						{#if profile.is_builtin && profile.is_modified}
							<button
								class="card-action-btn"
								title="Revert to defaults"
								onclick={() => onRevertProfile(profile.id)}
							>
								<RotateCcw size={16} />
							</button>
						{/if}
						<button
							class="card-action-btn"
							title="Export"
							onclick={() => onExportProfile(profile.id)}
						>
							<Download size={16} />
						</button>
						{#if !profile.is_builtin}
							<button
								class="card-action-btn card-action-btn--destructive"
								title="Delete"
								onclick={() => { deletingId = profile.id; }}
							>
								<Trash2 size={16} />
							</button>
						{/if}
					</div>
					<div class="fp-item-chevron"><ChevronRight size={16} /></div>
				</div>
			{/if}
		{/each}
	</div>
{/if}

<style>
	.fp-search-bar {
		max-width: 640px;
		width: 100%;
		margin: 0 auto;
	}

	.fp-search-bar :global(.fph-search-wrapper) {
		width: 100%;
	}

	.fp-search-bar :global(.fph-search-input) {
		width: 100% !important;
	}

	.fp-btn--sm {
		padding: 4px 10px;
		font-size: 0.75rem;
	}

	/* Slice F: strict-MCP badge — uses warning hue to signal "fail-fast on misconfig". */
	:global(.fp-item-badge--strict) {
		background: color-mix(in oklch, var(--warning, #d97706) 14%, transparent);
		color: var(--warning, #d97706);
		border-color: color-mix(in oklch, var(--warning, #d97706) 38%, transparent);
	}
</style>
