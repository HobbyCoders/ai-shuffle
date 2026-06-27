<script lang="ts">
	/**
	 * ApiUserManager - Manage API users for external integrations and connectors.
	 *
	 * Admin-only component for creating, editing, and deleting API users.
	 * API keys are generated on create and can be rotated. Shown once only.
	 */

	import { onMount } from 'svelte';
	import Banner from '$lib/components/Banner.svelte';
	import { api } from '$lib/api/client';
	import type { Profile } from '$lib/api/client';
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

	// Form state
	let formName = $state('');
	let formDescription = $state('');
	let formProjectId = $state('');
	let formProfileIds: string[] = $state([]);
	let formWebLoginAllowed = $state(false);
	let formIsActive = $state(true);
	let saving = $state(false);
	let formError = $state('');

	async function loadData() {
		try {
			const [usersData, profilesData, projectsData] = await Promise.all([
				getApiUsers(),
				api.get<Profile[]>('/profiles'),
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
				web_login_allowed: formWebLoginAllowed
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
				web_login_allowed: formWebLoginAllowed
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
		formWebLoginAllowed = user.web_login_allowed;
		formIsActive = user.is_active;
		showCreateForm = true;
	}

	function resetForm() {
		showCreateForm = false;
		editingUser = null;
		formName = '';
		formDescription = '';
		formProjectId = '';
		formProfileIds = [];
		formWebLoginAllowed = false;
		formIsActive = true;
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
			await navigator.clipboard.writeText(text);
			copied = true;
			setTimeout(() => { copied = false; }, 2000);
		} catch {
			// Fallback
			const input = document.createElement('input');
			input.value = text;
			document.body.appendChild(input);
			input.select();
			document.execCommand('copy');
			document.body.removeChild(input);
			copied = true;
			setTimeout(() => { copied = false; }, 2000);
		}
	}

	function formatDate(dateStr: string | null | undefined): string {
		if (!dateStr) return 'Never';
		return new Date(dateStr).toLocaleString();
	}

	function getProjectName(projectId: string | null): string | null {
		if (!projectId) return null;
		const project = projects.find(p => p.id === projectId);
		return project?.name || projectId;
	}

	onMount(async () => {
		await loadData();
		loading = false;
	});
</script>

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
				<p class="text-xs text-muted-foreground mt-0.5">
					Manage API users for connectors and external integrations
				</p>
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

				<div class="space-y-3">
					<!-- Name -->
					<div>
						<label class="block text-xs text-muted-foreground mb-1" for="api-user-name">
							Name <span class="text-destructive">*</span>
						</label>
						<input
							id="api-user-name"
							type="text"
							bind:value={formName}
							placeholder="e.g., Discord Bot, Mobile App"
							class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary transition-colors"
						/>
					</div>

					<!-- Description -->
					<div>
						<label class="block text-xs text-muted-foreground mb-1" for="api-user-desc">Description</label>
						<input
							id="api-user-desc"
							type="text"
							bind:value={formDescription}
							placeholder="Optional description for this API user"
							class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary transition-colors"
						/>
					</div>

					<!-- Project -->
					<div>
						<label class="block text-xs text-muted-foreground mb-1" for="api-user-project">Project</label>
						<select
							id="api-user-project"
							bind:value={formProjectId}
							class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground focus:outline-none focus:border-primary transition-colors"
						>
							<option value="">None</option>
							{#each projects as project}
								<option value={project.id}>{project.name}</option>
							{/each}
						</select>
						<p class="text-xs text-muted-foreground mt-1">Restrict this user to a specific project workspace</p>
					</div>

					<!-- Profiles -->
					{#if profiles.length > 0}
						<div>
							<span class="block text-xs text-muted-foreground mb-2">Profiles</span>
							<div class="space-y-1.5 max-h-40 overflow-y-auto">
								{#each profiles as profile}
									<label class="flex items-center gap-2 cursor-pointer">
										<input
											type="checkbox"
											checked={formProfileIds.includes(profile.id)}
											onchange={() => toggleProfile(profile.id)}
											class="w-4 h-4 rounded border-border bg-input text-primary focus:ring-primary focus:ring-offset-background"
										/>
										<span class="text-sm text-foreground">{profile.name}</span>
										{#if profile.description}
											<span class="text-xs text-muted-foreground truncate">— {profile.description}</span>
										{/if}
									</label>
								{/each}
							</div>
							<p class="text-xs text-muted-foreground mt-1">Which agent profiles this user can access</p>
						</div>
					{/if}

					<!-- Toggles -->
					<div class="flex flex-col gap-2">
						<label class="flex items-center gap-2 cursor-pointer">
							<input
								type="checkbox"
								bind:checked={formWebLoginAllowed}
								class="w-4 h-4 rounded border-border bg-input text-primary focus:ring-primary focus:ring-offset-background"
							/>
							<span class="text-sm text-foreground">Allow web login</span>
						</label>
						{#if editingUser}
							<label class="flex items-center gap-2 cursor-pointer">
								<input
									type="checkbox"
									bind:checked={formIsActive}
									class="w-4 h-4 rounded border-border bg-input text-primary focus:ring-primary focus:ring-offset-background"
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
		{/if}

		<!-- Users List -->
		{#if apiUsers.length === 0 && !showCreateForm}
			<div class="text-center py-8 text-muted-foreground text-sm">
				No API users yet. Create one to use with connectors or external integrations.
			</div>
		{:else}
			<div class="space-y-2">
				{#each apiUsers as user}
					<div class="bg-card/30 rounded-lg p-3 border border-border">
						<div class="flex items-start justify-between gap-4">
							<div class="flex-1 min-w-0">
								<!-- Name + Status -->
								<div class="flex items-center gap-2">
									<span class={`w-2 h-2 rounded-full flex-shrink-0 ${user.is_active ? 'bg-success' : 'bg-muted-foreground'}`}></span>
									<span class="text-sm font-medium text-foreground truncate">{user.name}</span>
									<span class="text-xs text-muted-foreground">({user.id})</span>
								</div>

								<!-- Description -->
								{#if user.description}
									<p class="text-xs text-muted-foreground mt-1 ml-4 truncate">{user.description}</p>
								{/if}

								<!-- Badges -->
								<div class="flex flex-wrap items-center gap-1.5 mt-2 ml-4">
									{#if getProjectName(user.project_id)}
										<span class="px-1.5 py-0.5 bg-primary/20 text-primary text-xs rounded">
											{getProjectName(user.project_id)}
										</span>
									{/if}
									{#if user.profile_ids && user.profile_ids.length > 0}
										<span class="px-1.5 py-0.5 bg-accent/30 text-accent-foreground text-xs rounded">
											{user.profile_ids.length} profile{user.profile_ids.length !== 1 ? 's' : ''}
										</span>
									{/if}
									{#if user.web_login_allowed}
										<span class="px-1.5 py-0.5 bg-muted text-muted-foreground text-xs rounded">
											web login
										</span>
									{/if}
									{#if !user.is_active}
										<span class="px-1.5 py-0.5 bg-destructive/20 text-destructive text-xs rounded">
											disabled
										</span>
									{/if}
								</div>

								<!-- Meta -->
								<div class="flex items-center gap-4 mt-2 ml-4 text-xs text-muted-foreground">
									<span>Created: {formatDate(user.created_at)}</span>
									<span>Last used: {formatDate(user.last_used_at)}</span>
								</div>
							</div>

							<!-- Actions -->
							<div class="flex items-center gap-1 flex-shrink-0">
								<!-- Edit -->
								<button
									onclick={() => startEdit(user)}
									class="p-1.5 text-muted-foreground hover:text-foreground action-btn rounded transition-colors"
									title="Edit"
								>
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
									</svg>
								</button>
								<!-- Rotate Key -->
								<button
									onclick={() => handleRotateKey(user.id)}
									class="p-1.5 text-muted-foreground hover:text-warning action-btn rounded transition-colors"
									title="Rotate API key"
								>
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
									</svg>
								</button>
								<!-- Delete -->
								{#if deletingId === user.id}
									<button
										onclick={() => handleDelete(user.id)}
										class="px-2 py-1 text-xs text-destructive-foreground bg-destructive rounded transition-colors"
									>
										Confirm
									</button>
									<button
										onclick={() => { deletingId = null; }}
										class="px-2 py-1 text-xs text-foreground bg-secondary rounded transition-colors"
									>
										Cancel
									</button>
								{:else}
									<button
										onclick={() => { deletingId = user.id; }}
										class="p-1.5 text-muted-foreground hover:text-destructive action-btn rounded transition-colors"
										title="Delete"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
										</svg>
									</button>
								{/if}
							</div>
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
</style>
