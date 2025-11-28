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
	let editingProfile: any = null;

	// Profile form state
	let profileForm = {
		id: '',
		name: '',
		description: '',
		// Core settings
		model: 'sonnet',
		permission_mode: 'default',
		max_turns: null as number | null,
		// Tool configuration
		allowed_tools: '',
		disallowed_tools: '',
		// Streaming
		include_partial_messages: true,
		// Session behavior
		continue_conversation: false,
		fork_session: false,
		// System prompt
		system_prompt_type: 'preset',
		system_prompt_preset: 'claude_code',
		system_prompt_append: '',
		system_prompt_content: '',
		// Settings sources
		setting_sources: [] as string[],
		// Advanced
		cwd: '',
		add_dirs: '',
		user: '',
		max_buffer_size: null as number | null
	};

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

	function resetProfileForm() {
		profileForm = {
			id: '',
			name: '',
			description: '',
			model: 'sonnet',
			permission_mode: 'default',
			max_turns: null,
			allowed_tools: '',
			disallowed_tools: '',
			include_partial_messages: true,
			continue_conversation: false,
			fork_session: false,
			system_prompt_type: 'preset',
			system_prompt_preset: 'claude_code',
			system_prompt_append: '',
			system_prompt_content: '',
			setting_sources: [],
			cwd: '',
			add_dirs: '',
			user: '',
			max_buffer_size: null
		};
		editingProfile = null;
	}

	function openNewProfileForm() {
		resetProfileForm();
		showNewProfileForm = true;
	}

	function editProfile(profile: any) {
		editingProfile = profile;
		const config = profile.config || {};
		const sp = config.system_prompt || {};

		profileForm = {
			id: profile.id,
			name: profile.name,
			description: profile.description || '',
			model: config.model || 'sonnet',
			permission_mode: config.permission_mode || 'default',
			max_turns: config.max_turns || null,
			allowed_tools: (config.allowed_tools || []).join(', '),
			disallowed_tools: (config.disallowed_tools || []).join(', '),
			include_partial_messages: config.include_partial_messages !== false,
			continue_conversation: config.continue_conversation || false,
			fork_session: config.fork_session || false,
			system_prompt_type: sp.type || 'preset',
			system_prompt_preset: sp.preset || 'claude_code',
			system_prompt_append: sp.append || '',
			system_prompt_content: sp.content || '',
			setting_sources: config.setting_sources || [],
			cwd: config.cwd || '',
			add_dirs: (config.add_dirs || []).join(', '),
			user: config.user || '',
			max_buffer_size: config.max_buffer_size || null
		};
		showNewProfileForm = true;
	}

	async function saveProfile() {
		if (!profileForm.id || !profileForm.name) return;

		// Build config object
		const config: any = {
			model: profileForm.model,
			permission_mode: profileForm.permission_mode,
			include_partial_messages: profileForm.include_partial_messages,
			continue_conversation: profileForm.continue_conversation,
			fork_session: profileForm.fork_session
		};

		// Optional fields
		if (profileForm.max_turns) config.max_turns = profileForm.max_turns;
		if (profileForm.allowed_tools.trim()) {
			config.allowed_tools = profileForm.allowed_tools.split(',').map(t => t.trim()).filter(Boolean);
		}
		if (profileForm.disallowed_tools.trim()) {
			config.disallowed_tools = profileForm.disallowed_tools.split(',').map(t => t.trim()).filter(Boolean);
		}
		if (profileForm.setting_sources.length > 0) {
			config.setting_sources = profileForm.setting_sources;
		}
		if (profileForm.cwd.trim()) config.cwd = profileForm.cwd;
		if (profileForm.add_dirs.trim()) {
			config.add_dirs = profileForm.add_dirs.split(',').map(d => d.trim()).filter(Boolean);
		}
		if (profileForm.user.trim()) config.user = profileForm.user;
		if (profileForm.max_buffer_size) config.max_buffer_size = profileForm.max_buffer_size;

		// System prompt
		if (profileForm.system_prompt_type === 'preset') {
			config.system_prompt = {
				type: 'preset',
				preset: profileForm.system_prompt_preset
			};
			if (profileForm.system_prompt_append.trim()) {
				config.system_prompt.append = profileForm.system_prompt_append;
			}
		} else if (profileForm.system_prompt_content.trim()) {
			config.system_prompt = {
				type: 'custom',
				content: profileForm.system_prompt_content
			};
		}

		if (editingProfile) {
			await chat.updateProfile(profileForm.id, {
				name: profileForm.name,
				description: profileForm.description || undefined,
				config
			});
		} else {
			await chat.createProfile({
				id: profileForm.id.toLowerCase().replace(/[^a-z0-9-]/g, '-'),
				name: profileForm.name,
				description: profileForm.description || undefined,
				config
			});
		}

		resetProfileForm();
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

	function toggleSettingSource(source: string) {
		if (profileForm.setting_sources.includes(source)) {
			profileForm.setting_sources = profileForm.setting_sources.filter(s => s !== source);
		} else {
			profileForm.setting_sources = [...profileForm.setting_sources, source];
		}
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
				class="flex-1 overflow-y-auto"
			>
				<div class="max-w-4xl mx-auto px-4 py-4 space-y-6">
					{#if $messages.length === 0}
						<div class="h-full flex items-center justify-center min-h-[60vh]">
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
							<div class="flex flex-col {message.role === 'user' ? 'items-end' : 'items-start'}">
								<!-- Role label -->
								<div class="text-xs text-gray-500 mb-1 px-1">
									{message.role === 'user' ? 'You' : 'Claude'}
								</div>

								<!-- Message content -->
								<div class="w-full max-w-[85%] sm:max-w-[75%] card p-3 sm:p-4 {message.role === 'user' ? 'bg-[var(--color-primary)]/10 border-[var(--color-primary)]/30' : ''}">
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
			</div>

			<!-- Input area -->
			<div class="border-t border-[var(--color-border)] bg-[var(--color-bg)]">
				<div class="max-w-4xl mx-auto px-4 py-3">
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
		</div>
	</main>
</div>

<!-- Profile Management Modal -->
{#if showProfileModal}
	<div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
		<div class="card w-full max-w-2xl max-h-[90vh] flex flex-col">
			<div class="p-4 border-b border-[var(--color-border)] flex items-center justify-between">
				<h2 class="text-lg font-bold text-white">
					{showNewProfileForm ? (editingProfile ? 'Edit Profile' : 'Create Profile') : 'Manage Profiles'}
				</h2>
				<button
					class="text-gray-400 hover:text-white"
					on:click={() => { showProfileModal = false; showNewProfileForm = false; resetProfileForm(); }}
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<div class="flex-1 overflow-y-auto p-4">
				{#if showNewProfileForm}
					<div class="space-y-4">
						<!-- Basic Info -->
						<div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
							<div>
								<label class="block text-sm text-gray-400 mb-1">ID {editingProfile ? '(read-only)' : ''}</label>
								<input
									bind:value={profileForm.id}
									class="input"
									placeholder="my-profile"
									disabled={!!editingProfile}
								/>
							</div>
							<div>
								<label class="block text-sm text-gray-400 mb-1">Name *</label>
								<input bind:value={profileForm.name} class="input" placeholder="My Profile" />
							</div>
						</div>

						<div>
							<label class="block text-sm text-gray-400 mb-1">Description</label>
							<input bind:value={profileForm.description} class="input" placeholder="Optional description" />
						</div>

						<!-- Core Settings -->
						<div class="border-t border-[var(--color-border)] pt-4">
							<h3 class="text-sm font-medium text-white mb-3">Core Settings</h3>
							<div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
								<div>
									<label class="block text-sm text-gray-400 mb-1">Model</label>
									<select bind:value={profileForm.model} class="input">
										<option value="sonnet">Sonnet</option>
										<option value="opus">Opus</option>
										<option value="haiku">Haiku</option>
									</select>
								</div>
								<div>
									<label class="block text-sm text-gray-400 mb-1">Permission Mode</label>
									<select bind:value={profileForm.permission_mode} class="input">
										<option value="default">Default</option>
										<option value="acceptEdits">Accept Edits</option>
										<option value="plan">Plan Only</option>
										<option value="bypassPermissions">Bypass Permissions</option>
									</select>
								</div>
								<div>
									<label class="block text-sm text-gray-400 mb-1">Max Turns</label>
									<input
										type="number"
										bind:value={profileForm.max_turns}
										class="input"
										placeholder="Unlimited"
										min="1"
									/>
								</div>
							</div>
						</div>

						<!-- Tool Configuration -->
						<div class="border-t border-[var(--color-border)] pt-4">
							<h3 class="text-sm font-medium text-white mb-3">Tool Configuration</h3>
							<div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
								<div>
									<label class="block text-sm text-gray-400 mb-1">Allowed Tools</label>
									<input
										bind:value={profileForm.allowed_tools}
										class="input"
										placeholder="Read, Write, Bash (comma-separated)"
									/>
									<p class="text-xs text-gray-600 mt-1">Empty = all tools allowed</p>
								</div>
								<div>
									<label class="block text-sm text-gray-400 mb-1">Disallowed Tools</label>
									<input
										bind:value={profileForm.disallowed_tools}
										class="input"
										placeholder="Write, Edit (comma-separated)"
									/>
								</div>
							</div>
						</div>

						<!-- Streaming & Session Behavior -->
						<div class="border-t border-[var(--color-border)] pt-4">
							<h3 class="text-sm font-medium text-white mb-3">Behavior Settings</h3>
							<div class="space-y-3">
								<label class="flex items-center gap-3">
									<input type="checkbox" bind:checked={profileForm.include_partial_messages} class="w-4 h-4" />
									<div>
										<span class="text-sm text-white">Include Partial Messages</span>
										<p class="text-xs text-gray-500">Stream partial text as it's being generated</p>
									</div>
								</label>
								<label class="flex items-center gap-3">
									<input type="checkbox" bind:checked={profileForm.continue_conversation} class="w-4 h-4" />
									<div>
										<span class="text-sm text-white">Continue Conversation</span>
										<p class="text-xs text-gray-500">Automatically continue most recent conversation</p>
									</div>
								</label>
								<label class="flex items-center gap-3">
									<input type="checkbox" bind:checked={profileForm.fork_session} class="w-4 h-4" />
									<div>
										<span class="text-sm text-white">Fork Session</span>
										<p class="text-xs text-gray-500">Create new session ID when resuming</p>
									</div>
								</label>
							</div>
						</div>

						<!-- System Prompt -->
						<div class="border-t border-[var(--color-border)] pt-4">
							<h3 class="text-sm font-medium text-white mb-3">System Prompt</h3>
							<div class="space-y-3">
								<div>
									<label class="block text-sm text-gray-400 mb-1">Prompt Type</label>
									<select bind:value={profileForm.system_prompt_type} class="input">
										<option value="preset">Use Claude Code Preset</option>
										<option value="custom">Custom Prompt</option>
									</select>
								</div>

								{#if profileForm.system_prompt_type === 'preset'}
									<div>
										<label class="block text-sm text-gray-400 mb-1">Append Instructions</label>
										<textarea
											bind:value={profileForm.system_prompt_append}
											class="input"
											rows="3"
											placeholder="Additional instructions to append to the Claude Code preset..."
										></textarea>
									</div>
								{:else}
									<div>
										<label class="block text-sm text-gray-400 mb-1">Custom System Prompt</label>
										<textarea
											bind:value={profileForm.system_prompt_content}
											class="input"
											rows="4"
											placeholder="Enter your custom system prompt..."
										></textarea>
									</div>
								{/if}
							</div>
						</div>

						<!-- Settings Sources -->
						<div class="border-t border-[var(--color-border)] pt-4">
							<h3 class="text-sm font-medium text-white mb-3">Settings Sources</h3>
							<p class="text-xs text-gray-500 mb-2">Load settings from filesystem locations</p>
							<div class="flex flex-wrap gap-3">
								<label class="flex items-center gap-2">
									<input
										type="checkbox"
										checked={profileForm.setting_sources.includes('user')}
										on:change={() => toggleSettingSource('user')}
										class="w-4 h-4"
									/>
									<span class="text-sm text-gray-300">User (~/.claude)</span>
								</label>
								<label class="flex items-center gap-2">
									<input
										type="checkbox"
										checked={profileForm.setting_sources.includes('project')}
										on:change={() => toggleSettingSource('project')}
										class="w-4 h-4"
									/>
									<span class="text-sm text-gray-300">Project (.claude)</span>
								</label>
								<label class="flex items-center gap-2">
									<input
										type="checkbox"
										checked={profileForm.setting_sources.includes('local')}
										on:change={() => toggleSettingSource('local')}
										class="w-4 h-4"
									/>
									<span class="text-sm text-gray-300">Local</span>
								</label>
							</div>
						</div>

						<!-- Advanced Settings -->
						<details class="border-t border-[var(--color-border)] pt-4">
							<summary class="text-sm font-medium text-white cursor-pointer">Advanced Settings</summary>
							<div class="mt-3 space-y-4">
								<div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
									<div>
										<label class="block text-sm text-gray-400 mb-1">Working Directory</label>
										<input
											bind:value={profileForm.cwd}
											class="input"
											placeholder="/workspace/my-project"
										/>
									</div>
									<div>
										<label class="block text-sm text-gray-400 mb-1">User Identifier</label>
										<input
											bind:value={profileForm.user}
											class="input"
											placeholder="user@example.com"
										/>
									</div>
								</div>
								<div>
									<label class="block text-sm text-gray-400 mb-1">Additional Directories</label>
									<input
										bind:value={profileForm.add_dirs}
										class="input"
										placeholder="/extra/dir1, /extra/dir2 (comma-separated)"
									/>
								</div>
								<div>
									<label class="block text-sm text-gray-400 mb-1">Max Buffer Size (bytes)</label>
									<input
										type="number"
										bind:value={profileForm.max_buffer_size}
										class="input"
										placeholder="Default"
										min="1024"
									/>
								</div>
							</div>
						</details>

						<!-- Actions -->
						<div class="flex gap-2 pt-4 border-t border-[var(--color-border)]">
							<button on:click={saveProfile} class="btn btn-primary flex-1">
								{editingProfile ? 'Save Changes' : 'Create Profile'}
							</button>
							<button on:click={() => { showNewProfileForm = false; resetProfileForm(); }} class="btn btn-secondary flex-1">
								Cancel
							</button>
						</div>
					</div>
				{:else}
					<button
						on:click={openNewProfileForm}
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
									<p class="text-xs text-gray-600 mt-1">
										{profile.config?.model || 'sonnet'} • {profile.config?.permission_mode || 'default'}
									</p>
								</div>
								<div class="flex items-center gap-1 ml-2">
									{#if !profile.is_builtin}
										<button
											on:click={() => editProfile(profile)}
											class="p-1.5 text-gray-500 hover:text-white rounded"
											title="Edit profile"
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
											</svg>
										</button>
										<button
											on:click={() => deleteProfile(profile.id)}
											class="p-1.5 text-gray-500 hover:text-red-500 rounded"
											title="Delete profile"
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
											</svg>
										</button>
									{/if}
								</div>
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
