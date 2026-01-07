<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { auth, username, isAdmin, apiUser } from '$lib/stores/auth';
	import { api } from '$lib/api/client';
	import type { ApiUser, ApiUserWithKey, Profile } from '$lib/api/client';
	import type { Project } from '$lib/stores/chat';

	let apiUsers: ApiUser[] = [];
	let profiles: Profile[] = [];
	let projects: Project[] = [];
	let loading = true;
	let error = '';

	// Form state
	let showCreateForm = false;
	let editingUser: ApiUser | null = null;
	let newlyCreatedKey: string | null = null;
	let regeneratedKey: string | null = null;

	let formData = {
		name: '',
		description: '',
		project_id: '',
		profile_id: ''
	};

	onMount(async () => {
		// Redirect non-admin users - they shouldn't see this page
		if (!$isAdmin) {
			goto('/');
			return;
		}
		await loadData();
	});

	async function loadData() {
		loading = true;
		error = '';
		try {
			const [usersRes, profilesRes, projectsRes] = await Promise.all([
				api.get<ApiUser[]>('/api-users'),
				api.get<Profile[]>('/profiles'),
				api.get<Project[]>('/projects')
			]);
			apiUsers = usersRes;
			profiles = profilesRes;
			projects = projectsRes;
		} catch (e: any) {
			error = e.detail || 'Failed to load data';
		}
		loading = false;
	}

	function resetForm() {
		formData = {
			name: '',
			description: '',
			project_id: '',
			profile_id: ''
		};
		editingUser = null;
		showCreateForm = false;
	}

	function openCreateForm() {
		resetForm();
		showCreateForm = true;
		newlyCreatedKey = null;
	}

	function openEditForm(user: ApiUser) {
		editingUser = user;
		formData = {
			name: user.name,
			description: user.description || '',
			project_id: user.project_id || '',
			profile_id: user.profile_id || ''
		};
		showCreateForm = true;
		newlyCreatedKey = null;
	}

	async function handleSubmit() {
		if (!formData.name.trim()) {
			error = 'Name is required';
			return;
		}

		error = '';
		try {
			if (editingUser) {
				await api.put(`/api-users/${editingUser.id}`, {
					name: formData.name,
					description: formData.description || null,
					project_id: formData.project_id || null,
					profile_id: formData.profile_id || null
				});
			} else {
				const result = await api.post<ApiUserWithKey>('/api-users', {
					name: formData.name,
					description: formData.description || null,
					project_id: formData.project_id || null,
					profile_id: formData.profile_id || null
				});
				newlyCreatedKey = result.api_key;
			}
			await loadData();
			if (!newlyCreatedKey) {
				resetForm();
			}
		} catch (e: any) {
			error = e.detail || 'Failed to save API user';
		}
	}

	async function toggleActive(user: ApiUser) {
		try {
			await api.put(`/api-users/${user.id}`, {
				is_active: !user.is_active
			});
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to update user';
		}
	}

	async function regenerateKey(userId: string) {
		if (!confirm('Are you sure you want to regenerate this API key? The old key will stop working immediately.')) {
			return;
		}

		try {
			const result = await api.post<ApiUserWithKey>(`/api-users/${userId}/regenerate-key`);
			regeneratedKey = result.api_key;
			// Show in a modal or alert
			alert(`New API Key (copy now - won't be shown again):\n\n${result.api_key}`);
			regeneratedKey = null;
		} catch (e: any) {
			error = e.detail || 'Failed to regenerate key';
		}
	}

	async function deleteUser(userId: string) {
		if (!confirm('Are you sure you want to delete this API user? This cannot be undone.')) {
			return;
		}

		try {
			await api.delete(`/api-users/${userId}`);
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to delete user';
		}
	}

	function copyToClipboard(text: string) {
		navigator.clipboard.writeText(text);
	}

	function formatDate(dateStr: string | null): string {
		if (!dateStr) return 'Never';
		const date = new Date(dateStr);
		return date.toLocaleString();
	}

	async function handleLogout() {
		await auth.logout();
		goto('/login');
	}
</script>

<svelte:head>
	<title>Settings - AI Hub</title>
</svelte:head>

{#if !$isAdmin}
	<!-- Show access denied for non-admin users -->
	<div class="min-h-screen bg-[var(--color-bg)] flex items-center justify-center">
		<div class="card p-8 text-center max-w-md">
			<div class="text-4xl mb-4">🔒</div>
			<h1 class="text-xl font-bold text-white mb-2">Admin Access Required</h1>
			<p class="text-gray-400 mb-4">
				This page is only available to administrators.
			</p>
			<a href="/" class="btn btn-primary">Return to Chat</a>
		</div>
	</div>
{:else}
	<div class="min-h-screen bg-[var(--color-bg)]">
		<!-- Header -->
		<header class="bg-[var(--color-surface)] border-b border-[var(--color-border)] px-4 py-3">
			<div class="max-w-6xl mx-auto flex items-center justify-between">
				<div class="flex items-center gap-4">
					<a href="/" class="text-gray-400 hover:text-white">
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18" />
						</svg>
					</a>
					<h1 class="text-lg font-bold text-white">Settings</h1>
				</div>
				<div class="flex items-center gap-4">
					<span class="text-sm text-gray-400">{$username}</span>
					<button on:click={handleLogout} class="text-sm text-gray-400 hover:text-white">
						Logout
					</button>
				</div>
			</div>
		</header>

		<main class="max-w-6xl mx-auto px-4 py-6">
			<!-- API Users Section -->
			<section class="mb-8">
				<div class="flex items-center justify-between mb-4">
					<div>
						<h2 class="text-xl font-bold text-white">API Users</h2>
						<p class="text-sm text-gray-500 mt-1">
							Create API users for programmatic access. Each user gets an API key and can be restricted to specific projects and profiles.
						</p>
					</div>
					<button on:click={openCreateForm} class="btn btn-primary flex items-center gap-2">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
						</svg>
						Create API User
					</button>
				</div>

				{#if error}
					<div class="bg-red-900/50 border border-red-500 text-red-300 px-4 py-3 rounded-lg mb-4">
						{error}
						<button on:click={() => error = ''} class="ml-2 text-red-400 hover:text-red-300">&times;</button>
					</div>
				{/if}

				{#if loading}
					<div class="text-center py-8">
						<div class="animate-spin w-8 h-8 border-2 border-[var(--color-primary)] border-t-transparent rounded-full mx-auto"></div>
					</div>
				{:else if apiUsers.length === 0}
					<div class="card p-8 text-center">
						<p class="text-gray-400 mb-4">No API users yet</p>
						<p class="text-sm text-gray-500">Create an API user to allow external applications to access the AI Hub API.</p>
					</div>
				{:else}
					<div class="space-y-3">
						{#each apiUsers as user}
							<div class="card p-4">
								<div class="flex items-start justify-between">
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2 mb-1">
											<span class="font-medium text-white">{user.name}</span>
											<span class="text-xs px-2 py-0.5 rounded {user.is_active ? 'bg-green-900/50 text-green-400' : 'bg-red-900/50 text-red-400'}">
												{user.is_active ? 'Active' : 'Inactive'}
											</span>
										</div>
										{#if user.description}
											<p class="text-sm text-gray-500 mb-2">{user.description}</p>
										{/if}
										<div class="flex flex-wrap gap-4 text-xs text-gray-500">
											<span>
												Profile: <span class="text-gray-300">
													{profiles.find(p => p.id === user.profile_id)?.name || 'Any'}
												</span>
											</span>
											<span>
												Project: <span class="text-gray-300">
													{projects.find(p => p.id === user.project_id)?.name || 'Default'}
												</span>
											</span>
											<span>Created: {formatDate(user.created_at)}</span>
											<span>Last used: {formatDate(user.last_used_at)}</span>
										</div>
									</div>
									<div class="flex items-center gap-2 ml-4">
										<button
											on:click={() => openEditForm(user)}
											class="p-2 text-gray-500 hover:text-white rounded"
											title="Edit"
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
											</svg>
										</button>
										<button
											on:click={() => regenerateKey(user.id)}
											class="p-2 text-gray-500 hover:text-yellow-500 rounded"
											title="Regenerate Key"
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
											</svg>
										</button>
										<button
											on:click={() => toggleActive(user)}
											class="p-2 text-gray-500 hover:text-blue-500 rounded"
											title={user.is_active ? 'Deactivate' : 'Activate'}
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												{#if user.is_active}
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
												{:else}
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
												{/if}
											</svg>
										</button>
										<button
											on:click={() => deleteUser(user.id)}
											class="p-2 text-gray-500 hover:text-red-500 rounded"
											title="Delete"
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
											</svg>
										</button>
									</div>
								</div>
							</div>
						{/each}
					</div>
				{/if}
			</section>

			<!-- API Usage Instructions -->
			<section class="card p-6">
				<h3 class="text-lg font-bold text-white mb-3">API Usage</h3>
				<p class="text-sm text-gray-400 mb-4">
					Use the API key in the Authorization header to make requests:
				</p>
				<pre class="bg-[var(--color-bg)] p-3 rounded-lg text-sm overflow-x-auto mb-4"><code>curl -X POST http://localhost:8080/api/v1/conversation/stream \
  -H "Authorization: Bearer aih_your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{{"prompt": "Hello, Claude!"}}'</code></pre>
				<p class="text-xs text-gray-500">
					When using an API key, the configured project and profile for that user will be used automatically.
				</p>
			</section>
		</main>
	</div>

	<!-- Create/Edit Modal -->
	{#if showCreateForm}
		<div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
			<div class="card w-full max-w-lg">
				<div class="p-4 border-b border-[var(--color-border)] flex items-center justify-between">
					<h2 class="text-lg font-bold text-white">
						{editingUser ? 'Edit API User' : 'Create API User'}
					</h2>
					<button
						class="text-gray-400 hover:text-white"
						on:click={resetForm}
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>

				<div class="p-4 space-y-4">
					{#if newlyCreatedKey}
						<!-- Show newly created key -->
						<div class="bg-green-900/30 border border-green-500 p-4 rounded-lg">
							<p class="text-green-400 font-medium mb-2">API User created successfully!</p>
							<p class="text-sm text-gray-300 mb-3">Copy this API key now - it won't be shown again:</p>
							<div class="flex items-center gap-2">
								<code class="flex-1 bg-[var(--color-bg)] px-3 py-2 rounded text-sm text-white break-all">
									{newlyCreatedKey}
								</code>
								<button
									on:click={() => copyToClipboard(newlyCreatedKey || '')}
									class="btn btn-secondary shrink-0"
								>
									Copy
								</button>
							</div>
						</div>
						<button on:click={resetForm} class="btn btn-primary w-full">Done</button>
					{:else}
						<div>
							<label class="block text-sm text-gray-400 mb-1">Name *</label>
							<input
								bind:value={formData.name}
								class="input"
								placeholder="My Application"
							/>
						</div>

						<div>
							<label class="block text-sm text-gray-400 mb-1">Description</label>
							<textarea
								bind:value={formData.description}
								class="input"
								rows="2"
								placeholder="Optional description"
							></textarea>
						</div>

						<div>
							<label class="block text-sm text-gray-400 mb-1">Project</label>
							<select bind:value={formData.project_id} class="input">
								<option value="">Default Workspace</option>
								{#each projects as project}
									<option value={project.id}>{project.name}</option>
								{/each}
							</select>
							<p class="text-xs text-gray-600 mt-1">Restrict this user to a specific project workspace</p>
						</div>

						<div>
							<label class="block text-sm text-gray-400 mb-1">Profile</label>
							<select bind:value={formData.profile_id} class="input">
								<option value="">Any Profile</option>
								{#each profiles as profile}
									<option value={profile.id}>{profile.name}</option>
								{/each}
							</select>
							<p class="text-xs text-gray-600 mt-1">Restrict this user to use a specific agent profile</p>
						</div>

						<div class="flex gap-2 pt-2">
							<button on:click={handleSubmit} class="btn btn-primary flex-1">
								{editingUser ? 'Save Changes' : 'Create User'}
							</button>
							<button on:click={resetForm} class="btn btn-secondary flex-1">Cancel</button>
						</div>
					{/if}
				</div>
			</div>
		</div>
	{/if}
{/if}
