<script lang="ts">
	/**
	 * AgentDetail: one agent on the Agents page, in the Settings DetailPane
	 * anatomy. Header (avatar, name, badges, the visible actions: Duplicate,
	 * Export or Restore defaults), a sideways-scrolling tab strip whose tabs
	 * carry the counts, then the tab's row groups. The
	 * Profile tab ends with the danger zone and its inline confirm.
	 *
	 * ProfileCard owns the data and autosave; this component only renders and
	 * reports.
	 */
	import { tick } from 'svelte';
	import { Check, Copy, Download, LoaderCircle, Lock, RotateCcw } from 'lucide-svelte';
	import type { Profile } from '$lib/api/client';
	import type { AvatarId } from '$lib/api/agent-identity';
	import type { ToolSurface, ToolSurfaceGroup } from '$lib/api/toolSurface';
	import type { SubagentDefault } from '$lib/api/subagentDefault';
	import DetailPane from '$lib/components/settings/shell/DetailPane.svelte';
	import SettingRow from '$lib/components/settings/rows/SettingRow.svelte';
	import InlineConfirm from '$lib/components/settings/rows/InlineConfirm.svelte';
	import InlineBanner from '$lib/components/settings/rows/InlineBanner.svelte';
	import AgentAvatar from '$lib/components/agents/AgentAvatar.svelte';
	import ProfileToolConfig from './ProfileToolConfig.svelte';
	import AgentProfileTab from './AgentProfileTab.svelte';
	import AgentInstructionsTab from './AgentInstructionsTab.svelte';
	import AgentSubagentsTab from './AgentSubagentsTab.svelte';
	import AgentModelsTab from './AgentModelsTab.svelte';
	import AgentAdvancedTab from './AgentAdvancedTab.svelte';
	import AgentApprovalsTab from './AgentApprovalsTab.svelte';
	import type { ToolSelectionMode } from './toolSurface';
	import {
		AGENT_TABS,
		invokeModePatch,
		type AgentForm,
		type AgentInvokeMode,
		type AgentTab,
		type AgentTemplate,
		type GroupState,
		type RosterAgent
	} from './agentForm';

	interface Props {
		form: AgentForm;
		toolSelectionMode: ToolSelectionMode;
		/** null while the agent is an unsaved draft. */
		editingProfile: Profile | null;
		/** Created in this visit (or still a draft): the template row shows. */
		isNew: boolean;
		avatar: AvatarId;
		isAdmin: boolean;
		colorSupported: boolean;
		folder: string;
		saveState: 'idle' | 'saving' | 'saved' | 'error';
		remoteChange: string;
		tab: AgentTab;
		counts: { tools: number | undefined; subagents: number; chats: number | null };
		groups: GroupState;
		templates: AgentTemplate[];
		toolSurface: ToolSurface;
		toolsLoading: boolean;
		toolsRefreshing: boolean;
		others: RosterAgent[];
		subagentDefault: SubagentDefault | null | undefined;
		defaultError: string;
		identityError: string;
		avatarOf: (id: string) => AvatarId;
		colorOf: (id: string) => string | null;
		onTab: (tab: AgentTab) => void;
		onBack: () => void;
		onFormChange: (field: keyof AgentForm, value: unknown) => void;
		onToolSelectionModeChange: (mode: ToolSelectionMode) => void;
		onToggleTool: (group: ToolSurfaceGroup, toolName: string) => void;
		onToggleGroup: (group: ToolSurfaceGroup) => void;
		onRefreshTools: () => void;
		onToggleAgent: (id: string) => void;
		onApplyTemplate: (id: string) => void;
		onAssignToGroup: (profileId: string, groupName: string) => void;
		onRemoveFromGroup: (profileId: string) => void;
		onCreateGroup: (category: string, name: string) => void;
		onDeleteGroup: (category: string, name: string) => void;
		onRetryIdentity: () => void;
		onDefaultModel: (model: string) => void | Promise<void>;
		onDuplicate: () => void;
		onExport: () => void;
		onDelete: () => void | Promise<void>;
		onRestore: () => void | Promise<void>;
		onReloadRemote: () => void;
		onDismissRemote: () => void;
	}

	let {
		form,
		toolSelectionMode,
		editingProfile,
		isNew,
		avatar,
		isAdmin,
		colorSupported,
		folder,
		saveState,
		remoteChange,
		tab,
		counts,
		groups,
		templates,
		toolSurface,
		toolsLoading,
		toolsRefreshing,
		others,
		subagentDefault,
		defaultError,
		identityError,
		avatarOf,
		colorOf,
		onTab,
		onBack,
		onFormChange,
		onToolSelectionModeChange,
		onToggleTool,
		onToggleGroup,
		onRefreshTools,
		onToggleAgent,
		onApplyTemplate,
		onAssignToGroup,
		onRemoveFromGroup,
		onCreateGroup,
		onDeleteGroup,
		onRetryIdentity,
		onDefaultModel,
		onDuplicate,
		onExport,
		onDelete,
		onRestore,
		onReloadRemote,
		onDismissRemote
	}: Props = $props();

	const draft = $derived(!editingProfile);
	const builtin = $derived(!!editingProfile?.is_builtin);
	const modified = $derived(!!editingProfile?.is_modified);
	const title = $derived(form.name.trim() || 'New agent');

	let root: HTMLDivElement | undefined = $state();
	let restoreArmed = $state(false);

	const kind = $derived(builtin ? 'Built-in' : 'Yours');
	const updated = $derived.by(() => {
		if (draft) return 'Not saved yet';
		const at = editingProfile?.updated_at;
		if (!at) return '';
		const ms = Date.parse(at);
		if (!Number.isFinite(ms)) return '';
		const diff = Date.now() - ms;
		if (diff < 60_000) return 'Updated just now';
		if (diff < 3_600_000) return `Updated ${Math.floor(diff / 60_000)}m ago`;
		if (diff < 86_400_000) return `Updated ${Math.floor(diff / 3_600_000)}h ago`;
		if (diff < 30 * 86_400_000) return `Updated ${Math.floor(diff / 86_400_000)}d ago`;
		return `Updated ${new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`;
	});

	// Counts live on the tabs. Approvals (saved permission rules) is owner only.
	const tabs = $derived(
		AGENT_TABS.filter((t) => t.id !== 'approvals' || isAdmin).map((t) =>
			t.id === 'tools' ? { ...t, count: counts.tools } : t.id === 'subagents' ? { ...t, count: counts.subagents } : { ...t }
		)
	);
	const chats = $derived(counts.chats === null ? '' : `${counts.chats.toLocaleString()} ${counts.chats === 1 ? 'chat' : 'chats'}`);

	function pickInvokeMode(mode: AgentInvokeMode) {
		for (const [field, value] of Object.entries(invokeModePatch(mode))) {
			onFormChange(field as keyof AgentForm, value);
		}
	}

	/** Header Restore: open the bottom section with its confirm armed. */
	async function askRestore() {
		onTab('profile');
		await tick();
		restoreArmed = true;
		await tick();
		const zone = root?.querySelector<HTMLElement>('.st-grp.danger');
		zone?.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
		zone?.querySelector<HTMLElement>('.st-confirm .st-btn.ghost')?.focus({ preventScroll: true });
	}
</script>

{#snippet tile()}
	<AgentAvatar {avatar} size={42} color={builtin ? null : form.avatar_color} neutral={builtin} />
{/snippet}

{#snippet topStatus()}
	<span class="savetick" class:ok={saveState === 'saved'} class:err={saveState === 'error' || draft} role="status" aria-live="polite">
		{#if draft}
			Name it to save
		{:else if saveState === 'saving'}
			<LoaderCircle class="spin" />Saving
		{:else if saveState === 'saved'}
			<Check />Saved
		{:else if saveState === 'error'}
			Not saved
		{/if}
	</span>
{/snippet}

{#snippet badges()}
	{#if builtin}<span class="chip"><Lock />Built-in</span>{/if}
	{#if builtin && modified}<span class="chip warn">Modified</span>{/if}
	{#if draft}<span class="chip">Draft</span>{/if}
{/snippet}

{#snippet subtitle()}
	<span>{[kind, folder, updated, chats].filter(Boolean).join(' · ')}</span>
{/snippet}

{#snippet primary()}
	<button type="button" class="st-btn" title="Duplicate" aria-label="Duplicate" disabled={draft} onclick={onDuplicate}>
		<Copy /><span class="lbl">Duplicate</span>
	</button>
	<button type="button" class="st-btn" title="Export .json" aria-label="Export .json" disabled={draft} onclick={onExport}>
		<Download /><span class="lbl">Export</span>
	</button>
	{#if builtin}
		{#if modified}
			<button type="button" class="st-btn" title="Restore defaults" aria-label="Restore defaults" onclick={askRestore}>
				<RotateCcw /><span class="lbl">Restore</span>
			</button>
		{/if}
	{/if}
{/snippet}

{#snippet banner()}
	{#if remoteChange}
		<InlineBanner>
			{remoteChange}
			{#snippet actions()}
				{#if editingProfile}<button type="button" class="st-btn sm" onclick={onReloadRemote}>Load theirs</button>{/if}
				<button type="button" class="st-btn ghost sm" onclick={onDismissRemote}>Dismiss</button>
			{/snippet}
		</InlineBanner>
	{/if}
{/snippet}

{#snippet dangerRows()}
	{#if builtin}
		<SettingRow label="Restore defaults" hint={modified ? 'Drops your edits.' : 'No edits to drop.'}>
			<InlineConfirm
				bind:armed={restoreArmed}
				label="Restore"
				question="Drop your edits?"
				disabled={!modified}
				size="sm"
				onConfirm={onRestore}
			/>
		</SettingRow>
		<SettingRow label="Delete agent" hint="Built-ins stay.">
			<button type="button" class="st-btn sm danger" disabled><Lock />Delete</button>
		</SettingRow>
	{:else}
		<SettingRow label="Delete agent" hint={draft ? 'Discards this draft.' : 'Removes it everywhere.'}>
			<InlineConfirm
				label="Delete"
				question={`Delete ${draft ? 'this draft' : title}?`}
				size="sm"
				onConfirm={onDelete}
			/>
		</SettingRow>
	{/if}
{/snippet}

<div class="agd" bind:this={root}>
	<DetailPane
		width="reading"
		name={title}
		{tile}
		{badges}
		{subtitle}
		{primary}
		{topStatus}
		{banner}
		{tabs}
		{tab}
		onTabChange={(t) => onTab(t as AgentTab)}
		{onBack}
		backLabel="Agents"
		dangerRows={tab === 'profile' ? dangerRows : undefined}
	>
		{#snippet children(current)}
			{#if current === 'profile'}
				<AgentProfileTab
					{form}
					{editingProfile}
					{avatar}
					{isAdmin}
					{colorSupported}
					{groups}
					{identityError}
					{onRetryIdentity}
					{onFormChange}
					{onAssignToGroup}
					{onRemoveFromGroup}
					{onCreateGroup}
					{onDeleteGroup}
				/>
			{:else if current === 'prompt'}
				<AgentInstructionsTab
					prompt={form.system_prompt_content}
					onPrompt={(v) => onFormChange('system_prompt_content', v)}
					{templates}
					{isNew}
					{onApplyTemplate}
				/>
			{:else if current === 'tools'}
				<ProfileToolConfig
					{toolSelectionMode}
					allowedTools={form.allowed_tools}
					disallowedTools={form.disallowed_tools}
					surface={toolSurface}
					loading={toolsLoading}
					refreshing={toolsRefreshing}
					{isAdmin}
					{onToolSelectionModeChange}
					{onToggleTool}
					{onToggleGroup}
					onRefresh={onRefreshTools}
				/>
			{:else if current === 'approvals' && isAdmin}
				<AgentApprovalsTab profileId={editingProfile?.id ?? null} />
			{:else if current === 'subagents'}
				<AgentSubagentsTab
					agents={others}
					mode={form.agent_invoke_mode}
					enabled={form.enabled_agents}
					hasDefault={!!subagentDefault}
					{avatarOf}
					{colorOf}
					onMode={pickInvokeMode}
					onToggle={onToggleAgent}
					onSetEnabled={(ids) => onFormChange('enabled_agents', ids)}
				/>
			{:else if current === 'models'}
				<AgentModelsTab
					providerModels={form.provider_models}
					onModelsChange={(next) => onFormChange('provider_models', next)}
					{subagentDefault}
					{defaultError}
					{onDefaultModel}
				/>
			{:else if current === 'advanced'}
				<AgentAdvancedTab {form} {onFormChange} />
			{/if}
		{/snippet}
	</DetailPane>
</div>

<style>
	.agd {
		container: agd / inline-size;
		min-height: 100%;
	}

	.savetick {
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		gap: 5px;
		min-height: 20px;
		font-size: 12px;
		color: var(--st-mfg);
		white-space: nowrap;
	}
	.savetick.ok {
		color: var(--st-primary);
	}
	.savetick.err {
		color: var(--st-destructive);
	}
	.savetick :global(svg.lucide) {
		width: 13px;
		height: 13px;
	}
	.savetick :global(.spin) {
		animation: agd-spin 0.9s linear infinite;
	}
	@keyframes agd-spin {
		to {
			transform: rotate(360deg);
		}
	}
	.chip {
		flex: none;
		display: inline-flex;
		align-items: center;
		gap: 3px;
		padding: 1px 7px;
		border-radius: 999px;
		font-size: 10.5px;
		font-weight: 600;
		background: var(--st-muted);
		color: var(--st-mfg);
		line-height: 1.5;
	}
	.chip :global(svg.lucide) {
		width: 11px;
		height: 11px;
	}
	.chip.warn {
		background: var(--st-warning-soft);
		color: var(--st-warning);
	}
	/* Narrow form rows wrap; the shared DetailPane owns the header. */
	@container agd (max-width: 560px) {
		.agd :global(.st-row:has(> .st-rt)) {
			display: flex;
			flex-wrap: wrap;
			align-items: center;
			gap: 8px 12px;
			padding: 10px 14px;
		}
		.agd :global(.st-row > .st-rt) {
			flex: 1 1 128px;
		}
		.agd :global(.st-row > .st-rt .st-rh) {
			white-space: normal;
		}
		.agd :global(.st-row > .st-rc) {
			flex: 0 1 auto;
			max-width: 100%;
			margin-left: auto;
			flex-wrap: wrap;
		}
		.agd :global(.st-row:has(> .st-rc .st-txt) > .st-rc) {
			flex: 1 1 100%;
			flex-wrap: nowrap;
		}
		.agd :global(.st-row .st-txt) {
			flex: 1 1 160px;
		}
		.agd :global(.st-rowx) {
			padding: 4px 14px 14px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.savetick :global(.spin) {
			animation: none;
		}
	}
</style>
