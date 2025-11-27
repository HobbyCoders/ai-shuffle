<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { auth, username, claudeAuthenticated } from '$lib/stores/auth';
	import { chat, messages, isStreaming, chatError, profiles, selectedProfile, sessions, currentSessionId, projects, selectedProject } from '$lib/stores/chat';
	import { marked } from 'marked';

	let prompt = '';
	let messagesContainer: HTMLElement;
	let sidebarOpen = false;
	let showProfileModal = false;
	let showProjectModal = false;
	let showNewProfileForm = false;
	let showNewProjectForm = false;

	// Profile form
	let newProfileId = '';
	let newProfileName = '';
	let newProfileDescription = '';
	let newProfileModel = 'claude-sonnet-4';
	let newProfilePermissionMode = 'default';

	// Project form
	let newProjectId = '';
	let newProjectName = '';
	let newProjectDescription = '';

	onMount(async () => {
		await Promise.all([
			chat.loadProfiles(),
			chat.loadSessions(),
			chat.loadProjects()
		]);
	});

	$: if ($messages.length && messagesContainer) {
		setTimeout(() => {
			messagesContainer.scrollTop = messagesContainer.scrollHeight;
		}, 10);
	}

	async function handleSubmit() {
		if (!prompt.trim() || $isStreaming) return;
		const userPrompt = prompt;
		prompt = '';
		await chat.sendMessage(userPrompt);
	}

	function handleKeyDown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			handleSubmit();
		}
	}

	async function handleLogout() {
		await auth.logout();
		goto('/login');
	}

	function formatCost(cost: number | undefined): string {
		if (cost === undefined) return '';
		return `$${cost.toFixed(4)}`;
	}

	function renderMarkdown(content: string): string {
		return marked(content, { breaks: true }) as string;
	}

	function formatDate(dateStr: string): string {
		const date = new Date(dateStr);
		const now = new Date();
		const diff = now.getTime() - date.getTime();
		const hours = Math.floor(diff / 3600000);
		const days = Math.floor(diff / 86400000);

		if (hours < 1) return 'Just now';
		if (hours < 24) return `${hours}h ago`;
		if (days === 1) return 'Yesterday';
		if (days < 7) return `${days}d ago`;
		return date.toLocaleDateString();
	}

	function truncateTitle(title: string | null, maxLength: number = 30): string {
		if (!title) return 'New Chat';
		return title.length > maxLength ? title.substring(0, maxLength) + '...' : title;
	}

	async function selectSession(sessionId: string) {
		await chat.loadSession(sessionId);
		sidebarOpen = false;
	}

	async function createProfile() {
		if (!newProfileId || !newProfileName) return;

		await chat.createProfile({
			id: newProfileId.toLowerCase().replace(/[^a-z0-9-]/g, '-'),
			name: newProfileName,
			description: newProfileDescription || undefined,
			config: {
				model: newProfileModel,
				permission_mode: newProfilePermissionMode
			}
		});

		// Reset form
		newProfileId = '';
		newProfileName = '';
		newProfileDescription = '';
		newProfileModel = 'claude-sonnet-4';
		newProfilePermissionMode = 'default';
		showNewProfileForm = false;
	}

	async function deleteProfile(profileId: string) {
		if (confirm('Are you sure you want to delete this profile?')) {
			await chat.deleteProfile(profileId);
		}
	}

	async function createProject() {
		if (!newProjectId || !newProjectName) return;

		await chat.createProject({
			id: newProjectId.toLowerCase().replace(/[^a-z0-9-]/g, '-'),
			name: newProjectName,
			description: newProjectDescription || undefined
		});

		// Reset form
		newProjectId = '';
		newProjectName = '';
		newProjectDescription = '';
		showNewProjectForm = false;
	}

	async function deleteProject(projectId: string) {
		if (confirm('Are you sure you want to delete this project?')) {
			await chat.deleteProject(projectId);
		}
	}

	function handleNewChat() {
		chat.startNewChat();
		sidebarOpen = false;
	}
</script>

<svelte:head>
	<title>AI Hub</title>
	<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
</svelte:head>

<div class="min-h-screen flex flex-col lg:flex-row">
	<!-- Mobile Sidebar Overlay -->
	{#if sidebarOpen}
		<div
			class="fixed inset-0 bg-black/50 z-40 lg:hidden"
			on:click={() => sidebarOpen = false}
			on:keydown={(e) => e.key === 'Escape' && (sidebarOpen = false)}
			role="button"
			tabindex="0"
		></div>
	{/if}

	<!-- Sidebar -->
	<aside class="
		fixed lg:static inset-y-0 left-0 z-50
		w-72 lg:w-80 bg-[var(--color-bg)] border-r border-[var(--color-border)]
		transform transition-transform duration-200 ease-in-out
		{sidebarOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'}
		flex flex-col h-screen
	">
		<!-- Sidebar Header -->
		<div class="p-4 border-b border-[var(--color-border)]">
			<div class="flex items-center justify-between mb-4">
				<h1 class="text-lg font-bold text-white">AI Hub</h1>
				<button
					class="lg:hidden text-gray-400 hover:text-white p-1"
					on:click={() => sidebarOpen = false}
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<button
				on:click={handleNewChat}
				class="btn btn-primary w-full flex items-center justify-center gap-2"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
				</svg>
				New Chat
			</button>
		</div>

		<!-- Project Selector -->
		<div class="p-3 border-b border-[var(--color-border)]">
			<div class="flex items-center justify-between mb-2">
				<span class="text-xs text-gray-500 uppercase tracking-wider">Project</span>
				<button
					class="text-xs text-[var(--color-primary)] hover:text-[var(--color-primary-hover)]"
					on:click={() => showProjectModal = true}
				>
					Manage
				</button>
			</div>
			<select
				value={$selectedProject}
				on:change={(e) => chat.setProject(e.currentTarget.value)}
				class="input !py-1.5 text-sm"
			>
				<option value="">Default Workspace</option>
				{#each $projects as project}
					<option value={project.id}>{project.name}</option>
				{/each}
			</select>
		</div>

		<!-- Profile Selector -->
		<div class="p-3 border-b border-[var(--color-border)]">
			<div class="flex items-center justify-between mb-2">
				<span class="text-xs text-gray-500 uppercase tracking-wider">Profile</span>
				<button
					class="text-xs text-[var(--color-primary)] hover:text-[var(--color-primary-hover)]"
					on:click={() => showProfileModal = true}
				>
					Manage
				</button>
			</div>
			<select
				value={$selectedProfile}
				on:change={(e) => chat.setProfile(e.currentTarget.value)}
				class="input !py-1.5 text-sm"
			>
				{#each $profiles as profile}
					<option value={profile.id}>{profile.name}</option>
				{/each}
			</select>
		</div>

		<!-- Sessions List -->
		<div class="flex-1 overflow-y-auto p-3">
			<div class="flex items-center justify-between mb-2">
				<span class="text-xs text-gray-500 uppercase tracking-wider">Recent Sessions</span>
				<button
					on:click={() => chat.loadSessions()}
					class="text-xs text-gray-500 hover:text-white"
					title="Refresh sessions"
				>
					<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
					</svg>
				</button>
			</div>

			{#if $sessions.length === 0}
				<p class="text-sm text-gray-500 text-center py-4">No sessions yet</p>
			{:else}
				<div class="space-y-1">
					{#each $sessions as session}
						<button
							class="w-full text-left p-2 rounded-lg transition-colors {$currentSessionId === session.id ? 'bg-[var(--color-surface-hover)]' : 'hover:bg-[var(--color-surface)]'}"
							on:click={() => selectSession(session.id)}
						>
							<div class="text-sm text-white truncate">
								{truncateTitle(session.title)}
							</div>
							<div class="flex items-center gap-2 mt-1 text-xs text-gray-500">
								<span>{formatDate(session.updated_at)}</span>
								{#if session.total_cost_usd > 0}
									<span>• {formatCost(session.total_cost_usd)}</span>
								{/if}
							</div>
						</button>
					{/each}
				</div>
			{/if}
		</div>

		<!-- User Section -->
		<div class="p-3 border-t border-[var(--color-border)]">
			{#if !$claudeAuthenticated}
				<div class="text-xs text-yellow-500 mb-2 flex items-center gap-1">
					<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 20 20">
						<path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
					</svg>
					Claude not authenticated
				</div>
			{/if}
			<div class="flex items-center justify-between">
				<span class="text-sm text-gray-400">{$username}</span>
				<button on:click={handleLogout} class="text-sm text-gray-400 hover:text-white">
					Logout
				</button>
			</div>
		</div>
	</aside>

	<!-- Main Content -->
	<main class="flex-1 flex flex-col min-h-screen lg:min-h-0">
		<!-- Mobile Header -->
		<header class="lg:hidden bg-[var(--color-surface)] border-b border-[var(--color-border)] px-4 py-3 flex items-center justify-between">
			<button
				class="text-gray-400 hover:text-white p-1"
				on:click={() => sidebarOpen = true}
			>
				<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
				</svg>
			</button>
			<h1 class="text-lg font-bold text-white">AI Hub</h1>
			<button
				on:click={handleNewChat}
				class="text-gray-400 hover:text-white p-1"
			>
				<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
				</svg>
			</button>
		</header>

		<!-- Chat area -->
		<div class="flex-1 flex flex-col">
			<!-- Messages -->
			<div
				bind:this={messagesContainer}
				class="flex-1 overflow-y-auto p-4 space-y-4"
			>
				{#if $messages.length === 0}
					<div class="h-full flex items-center justify-center">
						<div class="text-center max-w-md px-4">
							<div class="text-4xl mb-4">💬</div>
							<p class="text-gray-400 mb-2">Start a conversation with Claude</p>
							<p class="text-gray-500 text-sm">
								Using profile: <span class="text-gray-300">{$profiles.find(p => p.id === $selectedProfile)?.name || $selectedProfile}</span>
							</p>
							{#if $selectedProject}
								<p class="text-gray-500 text-sm mt-1">
									Project: <span class="text-gray-300">{$projects.find(p => p.id === $selectedProject)?.name || $selectedProject}</span>
								</p>
							{/if}
						</div>
					</div>
				{:else}
					{#each $messages as message}
						<div class="flex gap-3 {message.role === 'user' ? 'justify-end' : ''}">
							<div class="max-w-[90%] sm:max-w-[80%] {message.role === 'user' ? 'order-2' : ''}">
								<!-- Role label -->
								<div class="text-xs text-gray-500 mb-1 {message.role === 'user' ? 'text-right' : ''}">
									{message.role === 'user' ? 'You' : 'Claude'}
								</div>

								<!-- Message content -->
								<div class="card p-3 sm:p-4 {message.role === 'user' ? 'bg-primary-900/30 border-primary-800' : ''}">
									{#if message.role === 'assistant'}
										<div class="prose prose-invert prose-sm max-w-none">
											{@html renderMarkdown(message.content)}
										</div>

										{#if message.streaming && !message.content}
											<div class="flex gap-1">
												<span class="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style="animation-delay: 0ms"></span>
												<span class="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style="animation-delay: 150ms"></span>
												<span class="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style="animation-delay: 300ms"></span>
											</div>
										{/if}

										<!-- Tool uses -->
										{#if message.toolUses && message.toolUses.length > 0}
											<div class="mt-3 space-y-2">
												{#each message.toolUses as tool}
													<details class="bg-[var(--color-bg)] rounded-lg overflow-hidden">
														<summary class="px-3 py-2 cursor-pointer text-sm text-gray-300 hover:bg-[var(--color-surface-hover)]">
															Tool: {tool.name}
														</summary>
														<div class="px-3 py-2 border-t border-[var(--color-border)]">
															<div class="text-xs text-gray-500 mb-1">Input:</div>
															<pre class="text-xs overflow-x-auto">{JSON.stringify(tool.input, null, 2)}</pre>
															{#if tool.output}
																<div class="text-xs text-gray-500 mt-2 mb-1">Output:</div>
																<pre class="text-xs overflow-x-auto max-h-40">{tool.output}</pre>
															{/if}
														</div>
													</details>
												{/each}
											</div>
										{/if}

										<!-- Metadata -->
										{#if message.metadata && !message.streaming}
											<div class="mt-3 pt-2 border-t border-[var(--color-border)] text-xs text-gray-500 flex flex-wrap gap-2 sm:gap-4">
												{#if message.metadata.total_cost_usd}
													<span>Cost: {formatCost(message.metadata.total_cost_usd as number)}</span>
												{/if}
												{#if message.metadata.duration_ms}
													<span>Time: {((message.metadata.duration_ms as number) / 1000).toFixed(1)}s</span>
												{/if}
											</div>
										{/if}
									{:else}
										<p class="whitespace-pre-wrap break-words">{message.content}</p>
									{/if}
								</div>
							</div>
						</div>
					{/each}
				{/if}

				{#if $chatError}
					<div class="bg-red-900/50 border border-red-500 text-red-300 px-4 py-3 rounded-lg">
						{$chatError}
						<button on:click={() => chat.clearError()} class="ml-2 text-red-400 hover:text-red-300">
							&times;
						</button>
					</div>
				{/if}
			</div>

			<!-- Input area -->
			<div class="p-3 sm:p-4 border-t border-[var(--color-border)] bg-[var(--color-bg)]">
				<form on:submit|preventDefault={handleSubmit} class="flex gap-2">
					<textarea
						bind:value={prompt}
						on:keydown={handleKeyDown}
						placeholder="Type a message..."
						class="input flex-1 resize-none min-h-[44px] max-h-32"
						rows="1"
						disabled={$isStreaming || !$claudeAuthenticated}
					></textarea>
					{#if $isStreaming}
						<button
							type="button"
							class="btn btn-danger shrink-0"
							on:click={() => chat.stopGeneration()}
						>
							Stop
						</button>
					{:else}
						<button
							type="submit"
							class="btn btn-primary shrink-0"
							disabled={!prompt.trim() || !$claudeAuthenticated}
						>
							Send
						</button>
					{/if}
				</form>

				{#if !$claudeAuthenticated}
					<p class="mt-2 text-xs sm:text-sm text-yellow-500">
						Claude CLI not authenticated. Run <code class="bg-[var(--color-surface)] px-1 rounded text-xs">docker exec -it ai-hub claude login</code>
					</p>
				{/if}
			</div>
		</div>
	</main>
</div>

<!-- Profile Management Modal -->
{#if showProfileModal}
	<div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
		<div class="card w-full max-w-lg max-h-[80vh] flex flex-col">
			<div class="p-4 border-b border-[var(--color-border)] flex items-center justify-between">
				<h2 class="text-lg font-bold text-white">Manage Profiles</h2>
				<button
					class="text-gray-400 hover:text-white"
					on:click={() => { showProfileModal = false; showNewProfileForm = false; }}
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<div class="flex-1 overflow-y-auto p-4">
				{#if showNewProfileForm}
					<div class="space-y-4">
						<h3 class="text-sm font-medium text-white">Create New Profile</h3>
						<div>
							<label class="block text-sm text-gray-400 mb-1">ID (lowercase, no spaces)</label>
							<input bind:value={newProfileId} class="input" placeholder="my-profile" />
						</div>
						<div>
							<label class="block text-sm text-gray-400 mb-1">Name</label>
							<input bind:value={newProfileName} class="input" placeholder="My Profile" />
						</div>
						<div>
							<label class="block text-sm text-gray-400 mb-1">Description</label>
							<textarea bind:value={newProfileDescription} class="input" rows="2" placeholder="Optional description"></textarea>
						</div>
						<div>
							<label class="block text-sm text-gray-400 mb-1">Model</label>
							<select bind:value={newProfileModel} class="input">
								<option value="claude-sonnet-4">Claude Sonnet 4</option>
								<option value="claude-opus-4">Claude Opus 4</option>
								<option value="claude-haiku-3-5">Claude Haiku 3.5</option>
							</select>
						</div>
						<div>
							<label class="block text-sm text-gray-400 mb-1">Permission Mode</label>
							<select bind:value={newProfilePermissionMode} class="input">
								<option value="default">Default</option>
								<option value="bypassPermissions">Bypass Permissions</option>
								<option value="plan">Plan Only</option>
							</select>
						</div>
						<div class="flex gap-2">
							<button on:click={createProfile} class="btn btn-primary flex-1">Create</button>
							<button on:click={() => showNewProfileForm = false} class="btn btn-secondary flex-1">Cancel</button>
						</div>
					</div>
				{:else}
					<button
						on:click={() => showNewProfileForm = true}
						class="btn btn-secondary w-full mb-4 flex items-center justify-center gap-2"
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
						</svg>
						Create New Profile
					</button>

					<div class="space-y-2">
						{#each $profiles as profile}
							<div class="p-3 bg-[var(--color-surface)] rounded-lg flex items-center justify-between">
								<div class="flex-1 min-w-0">
									<div class="flex items-center gap-2">
										<span class="text-sm font-medium text-white">{profile.name}</span>
										{#if profile.is_builtin}
											<span class="text-xs px-1.5 py-0.5 bg-[var(--color-primary)]/20 text-[var(--color-primary)] rounded">Built-in</span>
										{/if}
									</div>
									{#if profile.description}
										<p class="text-xs text-gray-500 truncate">{profile.description}</p>
									{/if}
								</div>
								{#if !profile.is_builtin}
									<button
										on:click={() => deleteProfile(profile.id)}
										class="text-gray-500 hover:text-red-500 ml-2"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
										</svg>
									</button>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			</div>
		</div>
	</div>
{/if}

<!-- Project Management Modal -->
{#if showProjectModal}
	<div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
		<div class="card w-full max-w-lg max-h-[80vh] flex flex-col">
			<div class="p-4 border-b border-[var(--color-border)] flex items-center justify-between">
				<h2 class="text-lg font-bold text-white">Manage Projects</h2>
				<button
					class="text-gray-400 hover:text-white"
					on:click={() => { showProjectModal = false; showNewProjectForm = false; }}
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<div class="flex-1 overflow-y-auto p-4">
				{#if showNewProjectForm}
					<div class="space-y-4">
						<h3 class="text-sm font-medium text-white">Create New Project</h3>
						<div>
							<label class="block text-sm text-gray-400 mb-1">ID (lowercase, no spaces)</label>
							<input bind:value={newProjectId} class="input" placeholder="my-project" />
						</div>
						<div>
							<label class="block text-sm text-gray-400 mb-1">Name</label>
							<input bind:value={newProjectName} class="input" placeholder="My Project" />
						</div>
						<div>
							<label class="block text-sm text-gray-400 mb-1">Description</label>
							<textarea bind:value={newProjectDescription} class="input" rows="2" placeholder="Optional description"></textarea>
						</div>
						<p class="text-xs text-gray-500">
							Project files will be stored in: <code class="bg-[var(--color-surface)] px-1 rounded">/workspace/{newProjectId || 'project-id'}/</code>
						</p>
						<div class="flex gap-2">
							<button on:click={createProject} class="btn btn-primary flex-1">Create</button>
							<button on:click={() => showNewProjectForm = false} class="btn btn-secondary flex-1">Cancel</button>
						</div>
					</div>
				{:else}
					<button
						on:click={() => showNewProjectForm = true}
						class="btn btn-secondary w-full mb-4 flex items-center justify-center gap-2"
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
						</svg>
						Create New Project
					</button>

					<div class="space-y-2">
						{#if $projects.length === 0}
							<p class="text-sm text-gray-500 text-center py-4">No projects yet. Create one to organize your work.</p>
						{:else}
							{#each $projects as project}
								<div class="p-3 bg-[var(--color-surface)] rounded-lg flex items-center justify-between">
									<div class="flex-1 min-w-0">
										<span class="text-sm font-medium text-white">{project.name}</span>
										{#if project.description}
											<p class="text-xs text-gray-500 truncate">{project.description}</p>
										{/if}
										<p class="text-xs text-gray-600">/workspace/{project.path}/</p>
									</div>
									<button
										on:click={() => deleteProject(project.id)}
										class="text-gray-500 hover:text-red-500 ml-2"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
										</svg>
									</button>
								</div>
							{/each}
						{/if}
					</div>
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.prose :global(pre) {
		@apply bg-[var(--color-bg)] rounded-lg p-3 overflow-x-auto;
	}

	.prose :global(code) {
		@apply bg-[var(--color-bg)] px-1 rounded;
	}

	.prose :global(p) {
		@apply mb-2;
	}

	.prose :global(ul), .prose :global(ol) {
		@apply mb-2 pl-4;
	}
</style>
