<script lang="ts">
	/**
	 * One API user (or the draft from "Add API user"): Overview, Run,
	 * Access and Sessions. Existing users save each change as it is made;
	 * the draft keeps its fields until Create.
	 */
	import { Bot, Copy, Lock, RefreshCw } from 'lucide-svelte';
	import ModelPicker from '$lib/components/settings/ModelPicker.svelte';
	import { appData, apiUserSessions } from '$lib/stores/app-data.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { layout } from '$lib/stores/layout.svelte';
	import type { Session } from '$lib/api/sessions';
	import type { ApiUser } from '$lib/api/apiUsers';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import TextField from '../rows/TextField.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import Switch from '../rows/Switch.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import StatusDot from '../rows/StatusDot.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import {
		DRAFT_ID,
		effortChoices,
		relativeAgo,
		sessionLabel,
		sessionsFor,
		shortDate,
		type ApiUsersModel
	} from './api-users.svelte';

	interface Props {
		model: ApiUsersModel;
		/** A worker id, or DRAFT_ID for the draft. */
		id: string;
	}

	let { model, id }: Props = $props();

	const PERMS_HELP =
		'Connector sessions never inherit bypass. Tool approval requests go to the connected platform, such as Discord, for the user to approve or deny.';

	const isDraft = $derived(id === DRAFT_ID);
	const user = $derived(isDraft ? undefined : model.userById(id));
	const draft = $derived(isDraft ? model.draft : null);
	const sessions = $derived(user ? sessionsFor($apiUserSessions, user.id) : []);
	const projectOptions = $derived([
		{ value: '', label: 'None' },
		...model.projects.map((p) => ({ value: p.id, label: p.name }))
	]);
	const tabs = $derived(
		isDraft ? ['Overview', 'Run', 'Access'] : ['Overview', 'Run', 'Access', { id: 'Sessions', count: sessions.length || '' }]
	);
	const revealedKey = $derived(user && model.revealed?.userId === user.id ? model.revealed.key : null);

	/** Bumps after a refused field save so the field shows the stored value again. */
	let rev = $state(0);
	let refreshing = $state(false);

	async function save(u: ApiUser, patch: Parameters<ApiUsersModel['update']>[1]) {
		if (await model.update(u.id, patch)) flashSaved();
		else rev += 1;
	}

	async function create() {
		const created = await model.createDraft();
		if (!created) {
			if (model.draftError) settingsNav.setTab('integrations', DRAFT_ID, 'Overview');
			return;
		}
		settingsNav.setTab('integrations', created, 'Overview');
		settingsNav.selectItem(created, 'integrations');
	}

	function cancelDraft() {
		model.discardDraft();
		settingsNav.clearSelection('integrations');
	}

	async function remove(u: ApiUser) {
		if (await model.remove(u.id)) settingsNav.clearSelection('integrations');
	}

	async function refreshSessions() {
		if (refreshing) return;
		refreshing = true;
		try {
			await appData.loadApiUserSessions();
		} finally {
			refreshing = false;
		}
	}

	// Same switch the main session list uses: prime project/worktree, then open.
	function openSession(session: Session) {
		const worktreeInfo =
			session.worktree_id || session.worktree_branch || session.branch
				? {
						worktreeId: session.worktree_id ?? null,
						worktreeBranch: session.worktree_branch ?? session.branch ?? '',
						worktreeDeleted: session.worktree_deleted ?? false,
						worktreeIntent: 'worktree' as const
					}
				: null;
		activeSession.switchSession(
			session.id,
			session.project_id ?? null,
			worktreeInfo,
			session.title || null,
			session.project_v2_id ?? null
		);
		layout.openChat(session.id);
	}

	function sessionTone(status: string | null | undefined) {
		if (!status) return 'off' as const;
		return /running|active|busy|working/i.test(status) ? ('busy' as const) : ('ok' as const);
	}
</script>

{#snippet errors()}
	{#if model.error}
		<InlineBanner tone="warn">
			<span role="alert">{model.error}</span>
			{#snippet actions()}
				<button type="button" class="st-btn ghost sm" onclick={() => (model.error = '')}>Dismiss</button>
			{/snippet}
		</InlineBanner>
	{/if}
{/snippet}

{#if draft}
	<DetailPane
		name={draft.name.trim() || 'New API user'}
		icon={Bot}
		status="busy"
		statusText="Not created yet"
		badge="API user"
		item={DRAFT_ID}
		category="integrations"
		{tabs}
	>
		{#snippet banner()}
			{#if model.draftError}
				<InlineBanner tone="warn"><span role="alert">{model.draftError}</span></InlineBanner>
			{/if}
		{/snippet}
		{#snippet children(tab)}
			{#if tab === 'Overview'}
				<RowGroup title="Identity">
					<SettingRow label="Name">
						<TextField bind:value={draft.name} placeholder="Discord bot" maxlength={100} />
					</SettingRow>
					<SettingRow label="Description">
						<TextField bind:value={draft.description} placeholder="Optional" />
					</SettingRow>
					<SettingRow label="Project" hint="Limits to one workspace">
						<SelectField bind:value={draft.projectId} options={projectOptions} />
					</SettingRow>
				</RowGroup>
			{:else if tab === 'Run'}
				{@render runRows(draft.model, draft.effort, (m) => model.setDraftModel(m), (e) => (draft.effort = e))}
			{:else if tab === 'Access'}
				{@render agentRows(draft.profileIds, (pid) => model.toggleDraftProfile(pid))}
			{/if}
			<div class="acts">
				<button type="button" class="st-btn ghost" onclick={cancelDraft}>Cancel</button>
				<button type="button" class="st-btn primary" disabled={model.creating} onclick={create}>
					{model.creating ? 'Creating…' : 'Create API user'}
				</button>
			</div>
		{/snippet}
	</DetailPane>
{:else if user}
	<DetailPane
		name={user.name}
		icon={Bot}
		status={user.is_active ? 'ok' : 'off'}
		statusText={user.is_active ? 'Active' : 'Disabled'}
		badge="API user"
		subtitle={user.description || undefined}
		item={user.id}
		category="integrations"
		facts={[
			{ label: 'Created', value: shortDate(user.created_at) },
			{ label: 'Last used', value: user.last_used_at ? relativeAgo(user.last_used_at) || shortDate(user.last_used_at) : 'Never' },
			{ label: 'Sessions', value: sessions.length },
			{ label: 'Permissions', value: 'Ask' }
		]}
		{tabs}
		danger={[
			{
				label: 'Rotate API key',
				hint: 'Old key stops working',
				button: 'Rotate key',
				question: 'Replace the key now?',
				onConfirm: async () => {
					await model.rotate(user.id);
				}
			},
			{
				label: 'Delete this user',
				hint: 'Its key stops working',
				button: 'Delete',
				question: `Delete ${user.name}?`,
				onConfirm: () => remove(user)
			}
		]}
	>
		{#snippet banner()}
			{#if revealedKey}
				<InlineBanner tone="key">
					Shown once: <code class="key">{revealedKey}</code>
					{#snippet actions()}
						<button type="button" class="st-btn sm" onclick={() => model.copyKey()}>
							<Copy />{model.copied ? 'Copied' : 'Copy key'}
						</button>
						<button type="button" class="st-btn ghost sm" onclick={() => model.dismissKey()}>Dismiss</button>
					{/snippet}
				</InlineBanner>
			{/if}
			{@render errors()}
		{/snippet}
		{#snippet children(tab)}
			{#if tab === 'Overview'}
				{#key rev}
					<RowGroup title="Identity">
						<SettingRow label="Name">
							<TextField value={user.name} maxlength={100} onchange={(v) => save(user, { name: v })} />
						</SettingRow>
						<SettingRow label="Description">
							<TextField
								value={user.description ?? ''}
								placeholder="Optional"
								onchange={(v) => save(user, { description: v })}
							/>
						</SettingRow>
						<SettingRow label="Project" hint="Limits to one workspace">
							<SelectField
								value={user.project_id ?? ''}
								options={projectOptions}
								onchange={(v) => save(user, { project_id: v })}
							/>
						</SettingRow>
						<SettingRow label="Active">
							<Switch checked={user.is_active} onchange={(v) => save(user, { is_active: v })} />
						</SettingRow>
					</RowGroup>
				{/key}
			{:else if tab === 'Run'}
				{@render runRows(
					user.model ?? '',
					user.effort ?? '',
					async (m) => {
						if (await model.setModel(user, m)) flashSaved();
					},
					(e) => save(user, { effort: e })
				)}
			{:else if tab === 'Access'}
				{@render agentRows(user.profile_ids ?? [], async (pid) => {
					if (await model.toggleProfile(user, pid)) flashSaved();
				})}
			{:else if tab === 'Sessions'}
				<RowGroup title="Sessions" count={sessions.length}>
					{#snippet actions()}
						<button
							type="button"
							class="st-btn icon ghost sm"
							class:spin={refreshing}
							aria-label="Refresh sessions"
							disabled={refreshing}
							onclick={refreshSessions}
						>
							<RefreshCw />
						</button>
					{/snippet}
					{#if sessions.length === 0}
						<SettingRow label="No sessions yet" />
					{:else}
						{#each sessions as session (session.id)}
							{@const agent = model.profileName(session.profile_id)}
							{@const project = model.projectName(session.project_id)}
							<button type="button" class="st-row ses" onclick={() => openSession(session)}>
								<span class="ses-main">
									<span class="ses-name">{sessionLabel(session)}</span>
									<span class="ses-meta">{[agent, project].filter(Boolean).join(', ')}</span>
								</span>
								<span class="ses-side">
									{#if session.status}
										<span class="ses-status"><StatusDot tone={sessionTone(session.status)} />{session.status}</span>
									{/if}
									<span class="ses-when">{relativeAgo(session.updated_at)}</span>
								</span>
							</button>
						{/each}
					{/if}
				</RowGroup>
			{/if}
		{/snippet}
	</DetailPane>
{/if}

{#snippet runRows(current: string, effort: string, onModel: (m: string) => void, onEffort: (e: string) => void)}
	<RowGroup title="Runs with">
		<SettingRow label="Model">
			<span class="picker">
				<ModelPicker id="api-user-model" value={current} allowEmpty emptyLabel="Agent default" onSelect={onModel} />
			</span>
		</SettingRow>
		<SettingRow label="Effort">
			<SelectField value={effort} options={effortChoices(current, effort)} onchange={onEffort} />
		</SettingRow>
		<SettingRow label="Permissions" help={PERMS_HELP}>
			<ValueText tone="fg"><Lock />Ask, always</ValueText>
		</SettingRow>
	</RowGroup>
{/snippet}

{#snippet agentRows(selected: string[], onToggle: (id: string) => void)}
	<RowGroup title="Agents" count="{selected.length} of {model.profiles.length}">
		{#if model.profiles.length === 0}
			<SettingRow label="No agents yet" />
		{:else}
			{#each model.profiles as profile (profile.id)}
				<label class="st-row agent">
					<input
						type="checkbox"
						class="chk"
						checked={selected.includes(profile.id)}
						onchange={() => onToggle(profile.id)}
					/>
					<span class="agent-name">{profile.name}</span>
					{#if profile.description}<span class="agent-desc">{profile.description}</span>{/if}
				</label>
			{/each}
		{/if}
	</RowGroup>
{/snippet}

<style>
	.acts {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 18px;
	}
	.key {
		font-family: var(--st-mono);
		font-size: 12px;
		overflow-wrap: anywhere;
		user-select: all;
	}
	.picker {
		display: inline-flex;
		min-width: 0;
		max-width: 300px;
	}
	.st-row.ses,
	.st-row.agent {
		display: grid;
		align-items: center;
		gap: 12px;
		width: 100%;
		min-height: 52px;
		padding: 8px 16px;
		border: 0;
		background: none;
		text-align: left;
		color: inherit;
		font: inherit;
	}
	.st-row.ses {
		grid-template-columns: minmax(0, 1fr) auto;
	}
	.st-row.ses:hover {
		background: var(--st-hover);
	}
	.st-row.ses:focus-visible {
		outline-offset: -2px;
	}
	.ses-main {
		display: grid;
		min-width: 0;
	}
	.ses-name {
		font-size: 14px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.ses-meta {
		font-size: 12.5px;
		color: var(--st-mfg);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.ses-side {
		display: grid;
		justify-items: end;
		gap: 2px;
	}
	.ses-status {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 13px;
		text-transform: capitalize;
	}
	.ses-when {
		font-size: 11.5px;
		color: var(--st-subtle);
		font-variant-numeric: tabular-nums;
	}
	.st-row.agent {
		grid-template-columns: auto auto minmax(0, 1fr);
		cursor: pointer;
	}
	.agent-name {
		font-size: 14px;
	}
	.agent-desc {
		font-size: 12.5px;
		color: var(--st-mfg);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.chk {
		width: 16px;
		height: 16px;
		margin: 0;
		accent-color: var(--st-primary);
	}
	.spin :global(svg.lucide) {
		animation: st-api-spin 0.9s linear infinite;
	}
	@keyframes st-api-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spin :global(svg.lucide) {
			animation: none;
		}
	}
</style>
