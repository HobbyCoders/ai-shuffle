<script lang="ts">
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	import { EFFORT_OPTIONS, modelEffortOptions, resolveModelEffort, reconcileModelEffort } from '$lib/constants/model-effort';
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * ApiUserManager - Manage API users for external integrations and connectors.
	 *
	 * Admin-only component for creating, editing, and deleting API users.
	 * API keys are generated on create and can be rotated. Shown once only.
	 */

	import { onMount } from 'svelte';
	import Banner from '$lib/components/Banner.svelte';
	import ModelPicker from '$lib/components/settings/ModelPicker.svelte';
	import { api } from '$lib/api/client';
	import type { Profile } from '$lib/api/client';
	import type { Session } from '$lib/api/sessions';
	import type {
		ApiUser,
		ApiUserCreateResponse,
		ApiUserRotateKeyResponse
	} from '$lib/api/apiUsers';
	import {
		getApiUsers,
		createApiUser as createApiUserApi,
		updateApiUser as updateApiUserApi,
		deleteApiUser as deleteApiUserApi,
		rotateApiUserKey
	} from '$lib/api/apiUsers';
	import { appData, apiUserSessions } from '$lib/stores/app-data.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { layout } from '$lib/stores/layout.svelte';

	interface Project {
		id: string;
		name: string;
		description?: string;
		path: string;
	}

	// Data
	let apiUsers: ApiUser[] = $state([]);
	let profiles: Profile[] = $state([]);
	let projects: Project[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	// UI
	let showCreateForm = $state(false);
	let editingUser: ApiUser | null = $state(null);
	let deletingId: string | null = $state(null);

	// Revealed API key (shown once after create or rotate)
	let revealedApiKey: string | null = $state(null);
	let revealedForUserId: string | null = $state(null);
	let copied = $state(false);

	// Expanded "Sessions" sections, keyed by API user id (collapsed by default).
	let expandedSessions: Set<string> = $state(new Set());
	// Loading feedback for the manual Sessions refresh affordance.
	let sessionsRefreshing = $state(false);

	// Form state
	let formName = $state('');
	let formDescription = $state('');
	let formProjectId = $state('');
	let formProfileIds: string[] = $state([]);
	let formIsActive = $state(true);
	// Preconfigured run selection: every session a connector starts for this
	// user binds these (re-bound each turn). NULL/'' falls back to the profile.
	let formModel = $state('');
	let formEffort = $state('');
	let saving = $state(false);
	let formError = $state('');

	// Same levels the chat composer offers.
	const effortLevels = $derived([
		{ value: '', label: 'Default', description: 'Use the agent default' },
		...(formModel ? modelEffortOptions(formModel, formEffort) : EFFORT_OPTIONS)
	]);

	async function loadData() {
		try {
			const [usersData, profilesData, projectsData] = await Promise.all([
				getApiUsers(),
				api.get<Profile[]>('/agents'),
				api.get<Project[]>('/projects')
			]);
			apiUsers = usersData;
			profiles = profilesData;
			projects = projectsData;
		} catch (e) {
			error = 'Failed to load data';
		}
	}

	async function handleCreate() {
		if (!formName.trim()) {
			formError = 'Name is required';
			return;
		}

		saving = true;
		formError = '';

		try {
			const result: ApiUserCreateResponse = await createApiUserApi({
				name: formName.trim(),
				description: formDescription.trim() || undefined,
				project_id: formProjectId || undefined,
				profile_ids: formProfileIds.length > 0 ? formProfileIds : undefined,
				model: formModel || null,
				effort: formEffort || null
			});

			revealedApiKey = result.api_key;
			revealedForUserId = result.id;
			copied = false;

			await loadData();
			resetForm();
		} catch (e: any) {
			formError = e.detail || 'Failed to create API user';
		} finally {
			saving = false;
		}
	}

	async function handleUpdate() {
		if (!editingUser) return;
		if (!formName.trim()) {
			formError = 'Name is required';
			return;
		}

		saving = true;
		formError = '';

		try {
			await updateApiUserApi(editingUser.id, {
				name: formName.trim(),
				description: formDescription.trim() || undefined,
				project_id: formProjectId || undefined,
				profile_ids: formProfileIds,
				is_active: formIsActive,
				model: formModel || null,
				effort: formEffort || null
			});
			await loadData();
			resetForm();
		} catch (e: any) {
			formError = e.detail || 'Failed to update API user';
		} finally {
			saving = false;
		}
	}

	async function handleDelete(id: string) {
		try {
			await deleteApiUserApi(id);
			deletingId = null;
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to delete API user';
		}
	}

	async function handleRotateKey(id: string) {
		try {
			const result: ApiUserRotateKeyResponse = await rotateApiUserKey(id);
			revealedApiKey = result.api_key;
			revealedForUserId = result.id;
			copied = false;
		} catch (e: any) {
			error = e.detail || 'Failed to rotate API key';
		}
	}

	function startEdit(user: ApiUser) {
		editingUser = user;
		formName = user.name;
		formDescription = user.description || '';
		formProjectId = user.project_id || '';
		formProfileIds = user.profile_ids ? [...user.profile_ids] : [];
		formIsActive = user.is_active;
		formModel = user.model || '';
		formEffort = user.effort || '';
		showCreateForm = true;
		remoteBaseline = remoteSnapshot();
		remoteChange = '';
	}
	function resetForm() {
		showCreateForm = false;
		editingUser = null;
		formName = '';
		formDescription = '';
		formProjectId = '';
		formProfileIds = [];
		formIsActive = true;
		formModel = '';
		formEffort = '';
		formError = '';
	}

	function toggleProfile(profileId: string) {
		if (formProfileIds.includes(profileId)) {
			formProfileIds = formProfileIds.filter(id => id !== profileId);
		} else {
			formProfileIds = [...formProfileIds, profileId];
		}
	}

	async function copyToClipboard(text: string) {
		try {
			await copyText(text);
			copied = true;
			setTimeout(() => { copied = false; }, 2000);
		} catch {
			copied = false;
			error = COPY_ERROR_MESSAGE;
		}
	}

	function formatDate(dateStr: string | null | undefined): string {
		if (!dateStr) return 'Never';
		return new Date(dateStr).toLocaleString();
	}

	// Parse a backend timestamp robustly. Session timestamps may be offset-aware
	// (…±HH:MM or trailing Z) or naive UTC (no zone). Offset-aware strings parse
	// as-is; naive ones get a 'Z' so they're read as UTC, not local time.
	function parseTimestamp(dateStr: string): Date {
		const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/.test(dateStr);
		return new Date(hasOffset ? dateStr : dateStr + 'Z');
	}

	// Relative "time ago" for session updated_at.
	function formatRelativeTime(dateStr: string | null | undefined): string {
		if (!dateStr) return '';
		const date = parseTimestamp(dateStr);
		if (isNaN(date.getTime())) return '';
		const diffMs = Date.now() - date.getTime();
		const mins = Math.floor(diffMs / 60000);
		if (mins < 1) return 'just now';
		if (mins < 60) return `${mins}m ago`;
		const hours = Math.floor(mins / 60);
		if (hours < 24) return `${hours}h ago`;
		const days = Math.floor(hours / 24);
		if (days < 7) return `${days}d ago`;
		if (days < 30) return `${Math.floor(days / 7)}w ago`;
		return date.toLocaleDateString();
	}

	function getProjectName(projectId: string | null): string | null {
		if (!projectId) return null;
		const project = projects.find(p => p.id === projectId);
		return project?.name || projectId;
	}

	function getProfileName(profileId: string | null | undefined): string | null {
		if (!profileId) return null;
		const profile = profiles.find(p => p.id === profileId);
		return profile?.name || profileId;
	}

	// Sessions belonging to a given API user, most recently updated first.
	function sessionsForUser(userId: string): Session[] {
		return $apiUserSessions
			.filter(s => s.api_user_id === userId)
			.slice()
			.sort((a, b) => parseTimestamp(b.updated_at).getTime() - parseTimestamp(a.updated_at).getTime());
	}

	function toggleSessions(userId: string) {
		const next = new Set(expandedSessions);
		if (next.has(userId)) {
			next.delete(userId);
		} else {
			next.add(userId);
		}
		expandedSessions = next;
	}

	async function refreshSessions() {
		if (sessionsRefreshing) return;
		sessionsRefreshing = true;
		try {
			await appData.loadApiUserSessions();
		} finally {
			sessionsRefreshing = false;
		}
	}

	// Open an API-user session in the main chat view via the same session-switch
	// mechanism the normal session list uses (handleOpenSession in +page.svelte):
	// prime the switcher with the session's project/worktree state, then navigate.
	function openSession(session: Session) {
		const worktreeInfo = session.worktree_id || session.worktree_branch || session.branch ? {
			worktreeId: session.worktree_id ?? null,
			worktreeBranch: session.worktree_branch ?? session.branch ?? '',
			worktreeDeleted: session.worktree_deleted ?? false,
			worktreeIntent: 'worktree' as const,
		} : null;
		activeSession.switchSession(session.id, session.project_id ?? null, worktreeInfo, session.title || null, session.project_v2_id ?? null);
		layout.openChat(session.id);
	}

	function sessionLabel(session: Session): string {
		return session.title?.trim() || session.id.slice(0, 8);
	}

	onMount(async () => {
		await loadData();
		loading = false;
	});
	let remoteChange = $state('');
	let remoteBaseline = '';
	function remoteSnapshot() { return JSON.stringify([formName, formDescription, formProjectId, formProfileIds, formIsActive, formModel, formEffort]); }
	watchResources(['api_users', 'settings', 'agents', 'projects'], async () => {
		const selected = editingUser;
		const before = remoteSnapshot();
		await loadData();
		if (!selected || editingUser !== selected) return;
		const fresh = apiUsers.find((item) => item.id === selected.id);
		if (saving || before !== remoteSnapshot() || remoteSnapshot() !== remoteBaseline) {
			remoteChange = fresh ? 'This item changed elsewhere. Your unsaved fields are kept here.' : 'This item was deleted elsewhere. Copy your unsaved fields before leaving.';
			return;
		}
		if (fresh) startEdit(fresh);
		else { editingUser = null; remoteChange = 'This item was deleted elsewhere.'; }
	});
</script>

{#if remoteChange}<p role="status" style="flex: none; height: auto;" class="px-4 py-2 text-sm text-muted-foreground">{remoteChange}</p>{/if}

<div class="space-y-4">
	{#if loading}
		<div class="text-center py-4 text-muted-foreground">Loading API users...</div>
	{:else if error}
		<Banner severity="error" message={error} dismissible onDismiss={() => (error = '')} />
	{:else}
		<!-- Header -->
		<div class="flex items-center justify-between">
			<div>
				<h3 class="text-sm font-medium text-foreground">API Users</h3>
			</div>
			{#if !showCreateForm}
				<button
					onclick={() => { showCreateForm = true; }}
					class="px-3 py-1.5 bg-primary hover:bg-primary/90 text-primary-foreground text-sm rounded-md transition-colors"
				>
					Add API User
				</button>
			{/if}
		</div>

		<!-- Revealed API Key Banner -->
		{#if revealedApiKey}
			<div class="rounded-lg p-4 border border-warning/40 bg-warning/10">
				<div class="flex items-start justify-between gap-3">
					<div class="flex-1 min-w-0">
						<p class="text-sm font-medium text-warning mb-1">
							API Key Generated
						</p>
						<p class="text-xs text-muted-foreground mb-2">
							This key will only be shown once. Copy it now and store it securely.
						</p>
						<code class="block px-3 py-2 bg-background border border-border rounded-md text-sm text-foreground font-mono break-all select-all">
							{revealedApiKey}
						</code>
					</div>
				</div>
				<div class="flex items-center gap-2 mt-3">
					<button
						onclick={() => copyToClipboard(revealedApiKey!)}
						class="px-3 py-1.5 bg-primary hover:bg-primary/90 text-primary-foreground text-xs rounded-md transition-colors"
					>
						{copied ? 'Copied!' : 'Copy Key'}
					</button>
					<button
						onclick={() => { revealedApiKey = null; revealedForUserId = null; }}
						class="px-3 py-1.5 bg-secondary hover:bg-secondary/80 text-foreground text-xs rounded-md transition-colors"
					>
						Dismiss
					</button>
				</div>
			</div>
		{/if}

		<!-- Create/Edit Form -->
		{#if showCreateForm}
			<div class="bg-muted/50 rounded-lg p-4 border border-border">
				<h4 class="text-sm font-medium text-foreground mb-3">
					{editingUser ? 'Edit API User' : 'New API User'}
				</h4>

				{#if formError}
					<div class="mb-3">
						<Banner severity="error" message={formError} />
					</div>
				{/if}

				<div class="space-y-5">
					<!-- Identity -->
				<div class="space-y-3">
					<div class="flex items-center gap-2 pb-1 border-b border-border">
						<span class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Identity</span>
					</div>

					<!-- Name -->
					<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
						<div>
							<label class="block text-xs text-muted-foreground mb-1" for="api-user-name">
								Name <span class="text-destructive">*</span>
							</label>
							<input
								id="api-user-name"
								type="text"
								bind:value={formName}
								placeholder="e.g., Discord Bot, Mobile App"
								class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary transition-colors"
							/>
						</div>

						<!-- Description -->
						<div>
							<label class="block text-xs text-muted-foreground mb-1" for="api-user-desc">Description</label>
							<input
								id="api-user-desc"
								type="text"
								bind:value={formDescription}
								placeholder="Optional description"
								class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary transition-colors"
							/>
						</div>
					</div>

					<!-- Project -->
					<div>
						<label class="block text-xs text-muted-foreground mb-1" for="api-user-project">Project</label>
						<select
							id="api-user-project"
							bind:value={formProjectId}
							class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground focus:outline-hidden focus:border-primary transition-colors"
						>
							<option value="">None</option>
							{#each projects as project}
								<option value={project.id}>{project.name}</option>
							{/each}
						</select>
						<p class="text-xs text-muted-foreground mt-1">Restrict this user to a specific project workspace</p>
					</div>
				</div>

				<!-- Run configuration -->
				<div class="space-y-3">
					<div class="flex items-center gap-2 pb-1 border-b border-border">
						<span class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Run configuration</span>
					</div>

					<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
						<!-- Model -->
						<div>
							<span class="block text-xs text-muted-foreground mb-1">Model</span>
							<ModelPicker
								id="api-user-model"
								value={formModel}
								allowEmpty
								emptyLabel="Agent default"
								onSelect={(value) => {
								formModel = value;
								if (value) formEffort = reconcileModelEffort(formEffort || null, resolveModelEffort(value)) ?? '';
							}}
							/>
							<p class="text-xs text-muted-foreground mt-1">Preconfigured for every connector session of this user</p>
						</div>

						<!-- Effort -->
						<div>
							<label class="block text-xs text-muted-foreground mb-1" for="api-user-effort">Effort</label>
							<select
								id="api-user-effort"
								bind:value={formEffort}
								class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground focus:outline-hidden focus:border-primary transition-colors"
							>
								{#each effortLevels as level}
									<option value={level.value}>{level.label} — {level.description}</option>
								{/each}
							</select>
						</div>
					</div>

					<div class="rounded-md border border-border bg-background/60 px-3 py-2">
						<p class="text-xs text-muted-foreground">
							<span class="font-medium text-foreground">Permissions are always Ask</span> — connector sessions
							can never inherit a bypass setting. Tool permission requests are sent to the connected
							platform (e.g. Discord) for the user to approve or deny.
						</p>
					</div>
				</div>

				<!-- Access -->
				<div class="space-y-3">
					<div class="flex items-center gap-2 pb-1 border-b border-border">
						<span class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Access</span>
					</div>

					<!-- Agents -->
					{#if profiles.length > 0}
						<div>
							<span class="block text-xs text-muted-foreground mb-2">Agents</span>
							<div class="space-y-1.5 max-h-40 overflow-y-auto">
								{#each profiles as profile}
									<label class="flex items-center gap-2 cursor-pointer">
										<input
											type="checkbox"
											checked={formProfileIds.includes(profile.id)}
											onchange={() => toggleProfile(profile.id)}
											class="w-4 h-4 rounded-sm border-border bg-input text-primary focus:ring-primary focus:ring-offset-background"
										/>
										<span class="text-sm text-foreground">{profile.name}</span>
										{#if profile.description}
											<span class="text-xs text-muted-foreground truncate">— {profile.description}</span>
										{/if}
									</label>
								{/each}
							</div>
							<p class="text-xs text-muted-foreground mt-1">Which agents this user can access</p>
						</div>
					{/if}

					<!-- Toggles -->
					<div class="flex flex-col gap-2">
						{#if editingUser}
							<label class="flex items-center gap-2 cursor-pointer">
								<input
									type="checkbox"
									bind:checked={formIsActive}
									class="w-4 h-4 rounded-sm border-border bg-input text-primary focus:ring-primary focus:ring-offset-background"
								/>
								<span class="text-sm text-foreground">Active</span>
							</label>
						{/if}
					</div>

					<!-- Actions -->
					<div class="flex items-center gap-2 pt-2">
						<button
							onclick={editingUser ? handleUpdate : handleCreate}
							disabled={saving}
							class="px-3 py-1.5 bg-primary hover:bg-primary/90 disabled:bg-primary/50 text-primary-foreground text-sm rounded-md transition-colors"
						>
							{saving ? 'Saving...' : (editingUser ? 'Update' : 'Create')}
						</button>
						<button
							onclick={resetForm}
							class="px-3 py-1.5 bg-secondary hover:bg-secondary/80 text-foreground text-sm rounded-md transition-colors"
						>
							Cancel
						</button>
					</div>
					</div>
				</div>
			</div>
		{/if}

		<!-- Users List -->
		{#if apiUsers.length === 0 && !showCreateForm}
			<div class="text-center py-8 text-muted-foreground text-sm">
				No API users yet. Create one to use with connectors or external integrations.
			</div>
		{:else}
			<div class="space-y-2">
				{#each apiUsers as user}
					{@const userSessions = sessionsForUser(user.id)}
					<div class="bg-card/30 rounded-lg p-3 border border-border">
						<div class="flex items-start justify-between gap-4">
							<div class="flex-1 min-w-0">
								<!-- Avatar + Name + Status -->
								<div class="flex items-center gap-2.5">
									<span class="w-8 h-8 rounded-full bg-primary/15 text-primary text-xs font-semibold flex items-center justify-center shrink-0 uppercase">
										{user.name.slice(0, 2)}
									</span>
									<div class="min-w-0">
										<div class="flex items-center gap-2">
											<span class={`w-2 h-2 rounded-full shrink-0 ${user.is_active ? 'bg-success' : 'bg-muted-foreground'}`}></span>
											<span class="text-sm font-medium text-foreground truncate">{user.name}</span>
											<span class="text-xs text-muted-foreground">({user.id})</span>
										</div>
										{#if user.description}
											<p class="text-xs text-muted-foreground mt-0.5 truncate">{user.description}</p>
										{/if}
									</div>
								</div>

								<!-- Badges -->
								<div class="flex flex-wrap items-center gap-1.5 mt-2">
									{#if user.model}
										<span class="px-1.5 py-0.5 bg-primary/20 text-primary text-xs rounded-sm font-medium" title="Preconfigured model">
											{user.model}
										</span>
									{/if}
									{#if user.effort}
										<span class="px-1.5 py-0.5 bg-primary/10 text-primary text-xs rounded-sm" title="Preconfigured effort">
											effort: {user.effort}
										</span>
									{/if}
									{#if getProjectName(user.project_id)}
										<span class="px-1.5 py-0.5 bg-primary/20 text-primary text-xs rounded-sm">
											{getProjectName(user.project_id)}
										</span>
									{/if}
									{#if user.profile_ids && user.profile_ids.length > 0}
										<span class="px-1.5 py-0.5 bg-accent/30 text-accent-foreground text-xs rounded-sm">
											{user.profile_ids.length} agent{user.profile_ids.length !== 1 ? 's' : ''}
										</span>
									{/if}
									<span class="px-1.5 py-0.5 bg-success/15 text-success text-xs rounded-sm" title="Permission requests funnel to the connector">
										ask
									</span>
									{#if !user.is_active}
										<span class="px-1.5 py-0.5 bg-destructive/20 text-destructive text-xs rounded-sm">
											disabled
										</span>
									{/if}
								</div>

								<!-- Meta -->
								<div class="flex items-center gap-4 mt-2 text-xs text-muted-foreground">
									<span>Created: {formatDate(user.created_at)}</span>
									<span>Last used: {formatDate(user.last_used_at)}</span>
								</div>
							</div>

							<!-- Actions -->
							<div class="flex items-center gap-1 shrink-0">
								<!-- Edit -->
								<button
									onclick={() => startEdit(user)}
									class="p-1.5 text-muted-foreground hover:text-foreground action-btn rounded-sm transition-colors"
									title="Edit"
									aria-label={`Edit key ${user.name}`}
								>
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
									</svg>
								</button>
								<!-- Rotate Key -->
								<button
									onclick={() => handleRotateKey(user.id)}
									class="p-1.5 text-muted-foreground hover:text-warning action-btn rounded-sm transition-colors"
									title="Rotate API key"
									aria-label={`Rotate API key for ${user.name}`}
								>
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
									</svg>
								</button>
								<!-- Delete -->
								{#if deletingId === user.id}
									<button
										onclick={() => handleDelete(user.id)}
										class="px-2 py-1 text-xs text-destructive-foreground bg-destructive rounded-sm transition-colors"
									>
										Confirm
									</button>
									<button
										onclick={() => { deletingId = null; }}
										class="px-2 py-1 text-xs text-foreground bg-secondary rounded-sm transition-colors"
									>
										Cancel
									</button>
								{:else}
									<button
										onclick={() => { deletingId = user.id; }}
										class="p-1.5 text-muted-foreground hover:text-destructive action-btn rounded-sm transition-colors"
										title="Delete"
										aria-label={`Delete key ${user.name}`}
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
										</svg>
									</button>
								{/if}
							</div>
						</div>

						<!-- Sessions (collapsed by default) -->
						<div class="mt-3 pt-3 border-t border-border">
							<div class="flex items-center gap-2">
								<button
									onclick={() => toggleSessions(user.id)}
									aria-expanded={expandedSessions.has(user.id)}
									class="sessions-toggle flex items-center gap-1.5 text-xs font-medium text-muted-foreground rounded-sm px-1 py-0.5 -ml-1 transition-colors"
								>
									<svg
										class={`w-3.5 h-3.5 shrink-0 transition-transform ${expandedSessions.has(user.id) ? 'rotate-90' : ''}`}
										fill="none" stroke="currentColor" viewBox="0 0 24 24"
									>
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
									</svg>
									<span>Sessions</span>
									<span class="px-1.5 py-0.5 bg-muted text-muted-foreground rounded-full text-[10px] leading-none">
										{userSessions.length}
									</span>
								</button>
								{#if expandedSessions.has(user.id)}
									<button
										onclick={refreshSessions}
										disabled={sessionsRefreshing}
										class="sessions-toggle p-1 text-muted-foreground rounded-sm transition-colors disabled:opacity-50"
										title="Refresh sessions"
									>
										<svg class={`w-3.5 h-3.5 ${sessionsRefreshing ? 'animate-spin' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
										</svg>
									</button>
								{/if}
							</div>

							{#if expandedSessions.has(user.id)}
								{#if userSessions.length === 0}
									<p class="text-xs text-muted-foreground mt-2 ml-1">
										No sessions yet for this API user.
									</p>
								{:else}
									<div class="space-y-1.5 mt-2">
										{#each userSessions as session (session.id)}
											<button
												onclick={() => openSession(session)}
												class="session-row w-full text-left rounded-[10px] border border-border bg-muted/40 px-3 py-2 transition-colors"
											>
												<div class="flex items-center gap-2 min-w-0">
													<span class="text-xs font-medium text-foreground truncate flex-1 min-w-0">
														{sessionLabel(session)}
													</span>
													<span class="text-[10px] text-muted-foreground shrink-0">
														{formatRelativeTime(session.updated_at)}
													</span>
												</div>
												<div class="flex flex-wrap items-center gap-1.5 mt-1.5">
													{#if session.status}
														<span class="px-1.5 py-0.5 bg-muted text-muted-foreground text-[10px] rounded-sm">
															{session.status}
														</span>
													{/if}
													{#if getProfileName(session.profile_id)}
														<span class="px-1.5 py-0.5 bg-accent/30 text-accent-foreground text-[10px] rounded-sm truncate max-w-[45%]">
															{getProfileName(session.profile_id)}
														</span>
													{/if}
													{#if getProjectName(session.project_id)}
														<span class="px-1.5 py-0.5 bg-primary/20 text-primary text-[10px] rounded-sm truncate max-w-[45%]">
															{getProjectName(session.project_id)}
														</span>
													{/if}
												</div>
											</button>
										{/each}
									</div>
								{/if}
							{/if}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	{/if}
</div>

<style>
	.action-btn:hover {
		background: var(--hover-overlay);
	}

	.action-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Session rows: hover feedback via border-color only — no opaque background
	   fill, which sticks on touch devices (:hover latches after tap). */
	.session-row:hover {
		border-color: var(--primary);
	}

	.session-row:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.sessions-toggle:hover {
		color: var(--foreground);
	}

	.sessions-toggle:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
</style>
