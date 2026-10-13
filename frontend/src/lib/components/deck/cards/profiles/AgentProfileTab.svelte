<script lang="ts">
	/**
	 * The Profile tab: Identity (avatar, name, description, folder) and the
	 * personal-assistant link. The danger zone comes after it (AgentDetail).
	 */
	import { tick } from 'svelte';
	import { ArrowUpRight, Check, FolderPlus, Lock, Trash2, X } from 'lucide-svelte';
	import type { Profile } from '$lib/api/client';
	import type { AvatarId } from '$lib/api/assistant';
	import RowGroup from '$lib/components/settings/rows/RowGroup.svelte';
	import SettingRow from '$lib/components/settings/rows/SettingRow.svelte';
	import TextField from '$lib/components/settings/rows/TextField.svelte';
	import SelectField from '$lib/components/settings/rows/SelectField.svelte';
	import AgentAvatar from '$lib/components/assistant/AgentAvatar.svelte';
	import AvatarPicker from './AvatarPicker.svelte';
	import type { AgentForm, GroupState } from './agentForm';

	interface Props {
		form: AgentForm;
		/** null while the agent is an unsaved draft. */
		editingProfile: Profile | null;
		avatar: AvatarId;
		isAdmin: boolean;
		colorSupported: boolean;
		isAssistant: boolean;
		groups: GroupState;
		identityError?: string;
		onRetryIdentity?: () => void;
		onFormChange: (field: keyof AgentForm, value: unknown) => void;
		onAssignToGroup: (profileId: string, groupName: string) => void;
		onRemoveFromGroup: (profileId: string) => void;
		onCreateGroup: (category: string, name: string) => void;
		onDeleteGroup: (category: string, name: string) => void;
		onOpenAssistant: (profileId: string) => void;
	}

	let {
		form,
		editingProfile,
		avatar,
		isAdmin,
		colorSupported,
		isAssistant,
		groups,
		identityError = '',
		onRetryIdentity,
		onFormChange,
		onAssignToGroup,
		onRemoveFromGroup,
		onCreateGroup,
		onDeleteGroup,
		onOpenAssistant
	}: Props = $props();

	const builtin = $derived(!!editingProfile?.is_builtin);
	const nameMissing = $derived(!form.name.trim());
	let picking = $state(false);

	// ── Folder ───────────────────────────────────────────────────────────
	let creatingFolder = $state(false);
	let newFolder = $state('');
	let newFolderInput: HTMLInputElement | undefined = $state();
	const folders = $derived((groups?.profiles?.groups ?? []).map((g) => g.name));
	const currentFolder = $derived.by(() => {
		const f = editingProfile ? groups?.profiles?.assignments?.[editingProfile.id] : '';
		return f && folders.includes(f) ? f : '';
	});

	async function startFolder() {
		creatingFolder = true;
		newFolder = '';
		await tick();
		newFolderInput?.focus();
	}

	function confirmFolder() {
		const name = newFolder.trim();
		if (name && editingProfile) {
			if (!folders.includes(name)) onCreateGroup('profiles', name);
			onAssignToGroup(editingProfile.id, name);
		}
		creatingFolder = false;
		newFolder = '';
	}

	function deleteFolder() {
		if (!editingProfile || !currentFolder) return;
		onRemoveFromGroup(editingProfile.id);
		onDeleteGroup('profiles', currentFolder);
	}
</script>

{#snippet picker()}
	{#if editingProfile}
		<AvatarPicker
			agentId={editingProfile.id}
			{builtin}
			color={form.avatar_color}
			{colorSupported}
			loadError={identityError}
			onRetry={onRetryIdentity}
			onColor={(c) => onFormChange('avatar_color', c)}
		/>
	{/if}
{/snippet}
{#snippet description()}
	<textarea
		class="st-area"
		rows={2}
		aria-label="Description"
		placeholder="Cuts releases and drafts the changelog."
		value={form.description}
		oninput={(e) => onFormChange('description', (e.target as HTMLTextAreaElement).value)}
	></textarea>
{/snippet}

<RowGroup title="Identity">
	{#if editingProfile && isAdmin}
		<SettingRow
			label="Avatar"
			hint={builtin ? 'Locked for built-ins.' : colorSupported ? 'Shape and colour.' : 'Pick a shape.'}
			expanded={picking && !builtin ? picker : undefined}
		>
			<AgentAvatar {avatar} size={30} color={builtin ? null : form.avatar_color} neutral={builtin} />
			{#if builtin}
				<button type="button" class="st-btn sm" disabled><Lock />Locked</button>
			{:else}
				<button type="button" class="st-btn sm" aria-expanded={picking} onclick={() => (picking = !picking)}>
					{picking ? 'Done' : 'Change'}
				</button>
			{/if}
		</SettingRow>
	{/if}

	<SettingRow label="Name" hint={nameMissing ? 'Required to save.' : 'Shown in pickers and chats.'}>
		<TextField
			value={form.name}
			width={260}
			placeholder="My agent"
			oninput={(v) => onFormChange('name', v)}
		/>
	</SettingRow>

	<SettingRow label="Description" hint="Helps callers pick this agent." expanded={description} />

	{#if editingProfile}
		{@const profileId = editingProfile.id}
		<SettingRow label="Folder" hint="Groups agents in the list.">
			{#if creatingFolder}
				<input
					bind:this={newFolderInput}
					class="st-txt-local"
					placeholder="New folder name"
					aria-label="New folder name"
					bind:value={newFolder}
					onkeydown={(e) => {
						if (e.key === 'Enter') {
							e.preventDefault();
							confirmFolder();
						}
						if (e.key === 'Escape') {
							e.preventDefault();
							e.stopPropagation();
							creatingFolder = false;
						}
					}}
				/>
				<button type="button" class="st-btn icon sm" aria-label="Create folder" title="Create folder" onclick={confirmFolder}><Check /></button>
				<button type="button" class="st-btn ghost icon sm" aria-label="Cancel new folder" title="Cancel" onclick={() => (creatingFolder = false)}><X /></button>
			{:else}
				<SelectField
					value={currentFolder}
					width={200}
					label="Folder"
					options={[{ value: '', label: 'No folder' }, ...folders.map((f) => ({ value: f, label: f }))]}
					onchange={(v) => (v ? onAssignToGroup(profileId, v) : onRemoveFromGroup(profileId))}
				/>
				<button type="button" class="st-btn icon sm" aria-label="New folder" title="New folder" onclick={startFolder}><FolderPlus /></button>
				{#if currentFolder}
					<button
						type="button"
						class="st-btn ghost icon sm"
						aria-label={`Delete folder ${currentFolder}`}
						title="Delete folder"
						onclick={deleteFolder}><Trash2 /></button
					>
				{/if}
			{/if}
		</SettingRow>
	{/if}
</RowGroup>

{#if editingProfile && isAdmin}
	{@const profileId = editingProfile.id}
	<RowGroup title="Assistant">
		<SettingRow
			label={isAssistant ? 'Your personal assistant' : 'Personal assistant'}
			hint={isAssistant ? 'Home chat and research.' : 'Give it a lasting home chat.'}
		>
			<button
				type="button"
				class="st-btn sm"
				aria-label={isAssistant ? 'Assistant settings' : 'Set up as assistant'}
				onclick={() => onOpenAssistant(profileId)}
			>
				{isAssistant ? 'Assistant settings' : 'Set up'}<ArrowUpRight />
			</button>
		</SettingRow>
	</RowGroup>
{/if}

<style>
	.st-area {
		display: block;
		width: 100%;
		padding: 8px 10px;
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		border-radius: var(--st-radius-md);
		font-size: 13px;
		line-height: 1.5;
		color: var(--st-fg);
		resize: vertical;
		outline: none;
	}
	.st-area::placeholder,
	.st-txt-local::placeholder {
		color: var(--st-subtle);
	}
	.st-area:focus,
	.st-txt-local:focus {
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	.st-txt-local {
		height: 32px;
		width: 180px;
		max-width: 100%;
		padding: 0 10px;
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		border-radius: var(--st-radius-md);
		font-size: 13px;
		color: var(--st-fg);
		outline: none;
		min-width: 0;
	}
</style>
