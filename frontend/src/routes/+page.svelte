<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { auth, username, claudeAuthenticated } from '$lib/stores/auth';
	import {
		chat,
		messages,
		isStreaming,
		chatError,
		profiles,
		selectedProfile,
		sessions,
		currentSessionId,
		projects,
		selectedProject,
		wsConnected
	} from '$lib/stores/chat';
	import { api, type FileUploadResponse } from '$lib/api/client';
	import { marked } from 'marked';

	let prompt = '';
	let messagesContainer: HTMLElement;
	let sidebarOpen = false;
	let shouldAutoScroll = true;
	let lastMessageCount = 0;
	let lastContentLength = 0;
	let showProfileModal = false;
	let showProjectModal = false;
	let showNewProfileForm = false;
	let showNewProjectForm = false;
	let editingProfile: any = null;
	let fileInput: HTMLInputElement;
	let isUploading = false;
	let uploadedFiles: FileUploadResponse[] = [];
	let textarea: HTMLTextAreaElement;

	// Profile form state
	let profileForm = {
		id: '',
		name: '',
		description: '',
		model: 'sonnet',
		permission_mode: 'default',
		max_turns: null as number | null,
		allowed_tools: '',
		disallowed_tools: '',
		include_partial_messages: true,
		continue_conversation: false,
		fork_session: false,
		system_prompt_type: 'preset',
		system_prompt_preset: 'claude_code',
		system_prompt_append: '',
		system_prompt_content: '',
		setting_sources: [] as string[],
		cwd: '',
		add_dirs: '',
		user: '',
		max_buffer_size: null as number | null
	};

	// Project form
	let newProjectId = '';
	let newProjectName = '';
	let newProjectDescription = '';

	onMount(() => {
		// Initialize WebSocket connection
		chat.init();

		// Load initial data
		Promise.all([
			chat.loadProfiles(),
			chat.loadSessions(),
			chat.loadProjects()
		]);
	});

	onDestroy(() => {
		chat.destroy();
	});

	// Check if user is near the bottom of scroll area
	function isNearBottom(threshold = 100): boolean {
		if (!messagesContainer) return true;
		const { scrollTop, scrollHeight, clientHeight } = messagesContainer;
		return scrollHeight - scrollTop - clientHeight < threshold;
	}

	// Auto-scroll when new messages arrive
	$: if (messagesContainer && $messages.length > 0) {
		const newMessageArrived = $messages.length > lastMessageCount;
		lastMessageCount = $messages.length;

		const totalContentLength = $messages.reduce((sum, m) => sum + (m.content?.length || 0), 0);
		const contentUpdated = totalContentLength > lastContentLength;
		lastContentLength = totalContentLength;

		if ((newMessageArrived || (contentUpdated && $isStreaming)) && shouldAutoScroll) {
			setTimeout(() => {
				if (isNearBottom(150)) {
					messagesContainer.scrollTop = messagesContainer.scrollHeight;
				}
			}, 10);
		}
	}

	function handleScroll() {
		if (!messagesContainer) return;
		shouldAutoScroll = isNearBottom(100);
	}

	async function handleSubmit() {
		if (!prompt.trim() || $isStreaming) return;
		const userPrompt = prompt;
		prompt = '';
		uploadedFiles = [];
		chat.sendMessage(userPrompt);
	}

	function handleKeyDown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			handleSubmit();
		}
	}

	// Auto-resize textarea
	function autoResize() {
		if (textarea) {
			textarea.style.height = 'auto';
			textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
		}
	}

	$: if (prompt !== undefined && textarea) {
		autoResize();
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

	function truncateTitle(title: string | null, maxLength: number = 35): string {
		if (!title) return 'New Chat';
		return title.length > maxLength ? title.substring(0, maxLength) + '...' : title;
	}

	async function selectSession(sessionId: string) {
		await chat.loadSession(sessionId);
		sidebarOpen = false;
	}

	async function deleteSession(e: Event, sessionId: string) {
		e.stopPropagation();
		if (confirm('Delete this session?')) {
			await chat.deleteSession(sessionId);
		}
	}

	function handleNewChat() {
		chat.startNewChat();
		sidebarOpen = false;
	}

	// Profile management functions
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

		const config: any = {
			model: profileForm.model,
			permission_mode: profileForm.permission_mode,
			include_partial_messages: profileForm.include_partial_messages,
			continue_conversation: profileForm.continue_conversation,
			fork_session: profileForm.fork_session
		};

		if (profileForm.max_turns) config.max_turns = profileForm.max_turns;
		if (profileForm.allowed_tools.trim()) {
			config.allowed_tools = profileForm.allowed_tools.split(',').map((t) => t.trim()).filter(Boolean);
		}
		if (profileForm.disallowed_tools.trim()) {
			config.disallowed_tools = profileForm.disallowed_tools.split(',').map((t) => t.trim()).filter(Boolean);
		}
		if (profileForm.setting_sources.length > 0) {
			config.setting_sources = profileForm.setting_sources;
		}
		if (profileForm.cwd.trim()) config.cwd = profileForm.cwd;
		if (profileForm.add_dirs.trim()) {
			config.add_dirs = profileForm.add_dirs.split(',').map((d) => d.trim()).filter(Boolean);
		}
		if (profileForm.user.trim()) config.user = profileForm.user;
		if (profileForm.max_buffer_size) config.max_buffer_size = profileForm.max_buffer_size;

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
		if (confirm('Delete this profile?')) {
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
		if (confirm('Delete this project?')) {
			await chat.deleteProject(projectId);
		}
	}

	function triggerFileUpload() {
		if (!$selectedProject) {
			alert('Please select a project first to upload files.');
			return;
		}
		fileInput?.click();
	}

	async function handleFileSelect(event: Event) {
		const input = event.target as HTMLInputElement;
		const files = input.files;
		if (!files || files.length === 0 || !$selectedProject) return;

		isUploading = true;
		try {
			for (const file of Array.from(files)) {
				const result = await api.uploadFile(`/projects/${$selectedProject}/upload`, file);
				uploadedFiles = [...uploadedFiles, result];
				const fileRef = `[File: ${result.path}]`;
				if (prompt.trim()) {
					prompt = prompt + '\n' + fileRef;
				} else {
					prompt = fileRef;
				}
			}
		} catch (error: any) {
			console.error('Upload failed:', error);
			alert(`Upload failed: ${error.detail || 'Unknown error'}`);
		} finally {
			isUploading = false;
			input.value = '';
		}
	}

	function removeUploadedFile(index: number) {
		const file = uploadedFiles[index];
		const fileRef = `[File: ${file.path}]`;
		prompt = prompt.replace(fileRef, '').replace(/\n\n+/g, '\n').trim();
		uploadedFiles = uploadedFiles.filter((_, i) => i !== index);
	}

	function toggleSettingSource(source: string) {
		if (profileForm.setting_sources.includes(source)) {
			profileForm.setting_sources = profileForm.setting_sources.filter((s) => s !== source);
		} else {
			profileForm.setting_sources = [...profileForm.setting_sources, source];
		}
	}
</script>

<svelte:head>
	<title>AI Hub</title>
	<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
</svelte:head>

<div class="h-screen flex overflow-hidden bg-[#0d0d0d]">
	<!-- Mobile Sidebar Overlay -->
	{#if sidebarOpen}
		<div
			class="fixed inset-0 bg-black/60 z-40 lg:hidden backdrop-blur-sm"
			on:click={() => (sidebarOpen = false)}
			on:keydown={(e) => e.key === 'Escape' && (sidebarOpen = false)}
			role="button"
			tabindex="0"
		></div>
	{/if}

	<!-- Sidebar -->
	<aside
		class="
		fixed lg:static inset-y-0 left-0 z-50
		w-72 bg-[#171717] border-r border-[#2a2a2a]
		transform transition-transform duration-200 ease-in-out
		{sidebarOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'}
		flex flex-col
	"
	>
		<!-- Header -->
		<div class="p-4 border-b border-[#2a2a2a]">
			<div class="flex items-center justify-between mb-4">
				<div class="flex items-center gap-2">
					<div class="w-8 h-8 rounded-lg bg-gradient-to-br from-violet-500 to-purple-600 flex items-center justify-center">
						<span class="text-white font-bold text-sm">AI</span>
					</div>
					<span class="font-semibold text-white">AI Hub</span>
				</div>
				<button class="lg:hidden text-gray-400 hover:text-white p-1" on:click={() => (sidebarOpen = false)}>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<button
				on:click={handleNewChat}
				class="w-full py-2.5 px-4 rounded-lg bg-[#2a2a2a] hover:bg-[#3a3a3a] text-white text-sm font-medium transition-colors flex items-center justify-center gap-2"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
				</svg>
				New Chat
			</button>
		</div>

		<!-- Selectors -->
		<div class="p-3 space-y-3 border-b border-[#2a2a2a]">
			<!-- Profile -->
			<div>
				<div class="flex items-center justify-between mb-1.5">
					<span class="text-[10px] uppercase tracking-wider text-gray-500 font-medium">Profile</span>
					<button class="text-[10px] text-violet-400 hover:text-violet-300" on:click={() => (showProfileModal = true)}>
						Manage
					</button>
				</div>
				<select
					value={$selectedProfile}
					on:change={(e) => chat.setProfile(e.currentTarget.value)}
					class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white focus:ring-1 focus:ring-violet-500"
				>
					{#each $profiles as profile}
						<option value={profile.id}>{profile.name}</option>
					{/each}
				</select>
			</div>

			<!-- Project -->
			<div>
				<div class="flex items-center justify-between mb-1.5">
					<span class="text-[10px] uppercase tracking-wider text-gray-500 font-medium">Project</span>
					<button class="text-[10px] text-violet-400 hover:text-violet-300" on:click={() => (showProjectModal = true)}>
						Manage
					</button>
				</div>
				<select
					value={$selectedProject}
					on:change={(e) => chat.setProject(e.currentTarget.value)}
					class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white focus:ring-1 focus:ring-violet-500"
				>
					<option value="">Default Workspace</option>
					{#each $projects as project}
						<option value={project.id}>{project.name}</option>
					{/each}
				</select>
			</div>
		</div>

		<!-- Sessions -->
		<div class="flex-1 overflow-y-auto p-3">
			<div class="flex items-center justify-between mb-2">
				<span class="text-[10px] uppercase tracking-wider text-gray-500 font-medium">History</span>
				<button on:click={() => chat.loadSessions()} class="text-gray-500 hover:text-white" title="Refresh">
					<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
						/>
					</svg>
				</button>
			</div>

			{#if $sessions.length === 0}
				<p class="text-sm text-gray-600 text-center py-8">No conversations yet</p>
			{:else}
				<div class="space-y-1">
					{#each $sessions as session}
						<button
							class="group w-full text-left p-2.5 rounded-lg transition-all
								{$currentSessionId === session.id
								? 'bg-[#2a2a2a] border-l-2 border-violet-500'
								: 'hover:bg-[#222] border-l-2 border-transparent'}"
							on:click={() => selectSession(session.id)}
						>
							<div class="flex items-center justify-between">
								<span class="text-sm text-gray-200 truncate flex-1">
									{truncateTitle(session.title)}
								</span>
								<button
									on:click={(e) => deleteSession(e, session.id)}
									class="opacity-0 group-hover:opacity-100 text-gray-500 hover:text-red-400 p-1 transition-opacity"
									title="Delete"
								>
									<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											stroke-width="2"
											d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
										/>
									</svg>
								</button>
							</div>
							<div class="flex items-center gap-2 mt-1 text-xs text-gray-500">
								<span>{formatDate(session.updated_at)}</span>
								{#if session.total_cost_usd > 0}
									<span class="text-gray-600">|</span>
									<span>{formatCost(session.total_cost_usd)}</span>
								{/if}
							</div>
						</button>
					{/each}
				</div>
			{/if}
		</div>

		<!-- User -->
		<div class="p-3 border-t border-[#2a2a2a]">
			<!-- Connection Status -->
			<div class="flex items-center gap-2 mb-2 text-xs">
				<div class="w-2 h-2 rounded-full {$wsConnected ? 'bg-green-500' : 'bg-red-500'}"></div>
				<span class="text-gray-500">{$wsConnected ? 'Connected' : 'Disconnected'}</span>
			</div>

			{#if !$claudeAuthenticated}
				<div class="text-xs text-amber-500 mb-2 flex items-center gap-1.5 bg-amber-500/10 px-2 py-1.5 rounded">
					<svg class="w-3.5 h-3.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
						<path
							fill-rule="evenodd"
							d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z"
							clip-rule="evenodd"
						/>
					</svg>
					<span>Claude not authenticated</span>
				</div>
			{/if}

			<div class="flex items-center justify-between">
				<span class="text-sm text-gray-400">{$username}</span>
				<div class="flex items-center gap-2">
					<a href="/settings" class="text-gray-500 hover:text-white p-1" title="Settings">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
							/>
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
						</svg>
					</a>
					<button on:click={handleLogout} class="text-gray-500 hover:text-white text-sm">Logout</button>
				</div>
			</div>
		</div>
	</aside>

	<!-- Main Content -->
	<main class="flex-1 flex flex-col min-w-0 bg-[#0d0d0d]">
		<!-- Mobile Header -->
		<header class="lg:hidden bg-[#171717] border-b border-[#2a2a2a] px-4 py-3 flex items-center justify-between">
			<button class="text-gray-400 hover:text-white" on:click={() => (sidebarOpen = true)}>
				<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
				</svg>
			</button>
			<span class="font-semibold text-white">AI Hub</span>
			<button on:click={handleNewChat} class="text-gray-400 hover:text-white">
				<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
				</svg>
			</button>
		</header>

		<!-- Messages Area -->
		<div bind:this={messagesContainer} on:scroll={handleScroll} class="flex-1 overflow-y-auto">
			{#if $messages.length === 0}
				<!-- Empty State -->
				<div class="h-full flex items-center justify-center">
					<div class="text-center max-w-md px-6">
						<div class="w-16 h-16 mx-auto mb-6 rounded-2xl bg-gradient-to-br from-violet-500/20 to-purple-600/20 flex items-center justify-center">
							<svg class="w-8 h-8 text-violet-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									stroke-width="1.5"
									d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
								/>
							</svg>
						</div>
						<h2 class="text-xl font-semibold text-white mb-2">Start a Conversation</h2>
						<p class="text-gray-500 mb-4">Ask Claude anything - code, questions, ideas, or just chat.</p>
						<div class="text-sm text-gray-600">
							<span class="text-gray-500">Profile:</span>
							<span class="text-gray-400 ml-1">{$profiles.find((p) => p.id === $selectedProfile)?.name || $selectedProfile}</span>
						</div>
					</div>
				</div>
			{:else}
				<!-- Messages -->
				<div class="max-w-3xl mx-auto px-4 py-6 space-y-6">
					{#each $messages as message, idx}
						<!-- User Message -->
						{#if message.role === 'user'}
							<div class="flex flex-col">
								<div class="text-xs text-gray-500 mb-1.5 font-medium">You</div>
								<div class="bg-[#1a1a1a] rounded-2xl px-4 py-3 text-gray-100">
									<p class="whitespace-pre-wrap">{message.content}</p>
								</div>
							</div>
						<!-- Assistant Text Message -->
						{:else if message.type === 'text' || !message.type}
							<div class="flex flex-col">
								<div class="text-xs text-violet-400 mb-1.5 font-medium flex items-center gap-2">
									Claude
									{#if message.streaming}
										<span class="flex gap-0.5">
											<span class="w-1.5 h-1.5 bg-violet-400 rounded-full animate-bounce" style="animation-delay: 0ms"></span>
											<span class="w-1.5 h-1.5 bg-violet-400 rounded-full animate-bounce" style="animation-delay: 150ms"></span>
											<span class="w-1.5 h-1.5 bg-violet-400 rounded-full animate-bounce" style="animation-delay: 300ms"></span>
										</span>
									{/if}
								</div>
								<div class="prose prose-invert prose-sm max-w-none text-gray-200">
									{#if message.content}
										{@html renderMarkdown(message.content)}
									{:else if message.streaming}
										<div class="flex gap-1 py-2">
											<span class="w-2 h-2 bg-gray-600 rounded-full animate-pulse"></span>
											<span class="w-2 h-2 bg-gray-600 rounded-full animate-pulse" style="animation-delay: 200ms"></span>
											<span class="w-2 h-2 bg-gray-600 rounded-full animate-pulse" style="animation-delay: 400ms"></span>
										</div>
									{/if}
								</div>

								<!-- Metadata -->
								{#if message.metadata && !message.streaming}
									<div class="mt-3 text-xs text-gray-600 flex items-center gap-3">
										{#if message.metadata.total_cost_usd}
											<span>{formatCost(message.metadata.total_cost_usd)}</span>
										{/if}
										{#if message.metadata.duration_ms}
											<span>{((message.metadata.duration_ms) / 1000).toFixed(1)}s</span>
										{/if}
									</div>
								{/if}
							</div>
						<!-- Tool Use -->
						{:else if message.type === 'tool_use'}
							<div class="ml-0">
								<div
									class="inline-flex items-center gap-2 bg-[#1e1e2e] border border-[#2a2a2a] rounded-lg px-3 py-2 text-sm"
								>
									{#if message.streaming}
										<svg class="w-4 h-4 text-blue-400 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												stroke-width="2"
												d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
											/>
										</svg>
									{:else}
										<svg class="w-4 h-4 text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
										</svg>
									{/if}
									<span class="text-gray-300 font-medium">{message.toolName}</span>
									{#if message.toolInput}
										<details class="inline">
											<summary class="text-gray-500 cursor-pointer hover:text-gray-400 text-xs">details</summary>
											<pre class="mt-2 text-xs bg-[#0d0d0d] p-2 rounded overflow-x-auto max-h-32">{JSON.stringify(message.toolInput, null, 2)}</pre>
										</details>
									{/if}
								</div>
							</div>
						<!-- Tool Result -->
						{:else if message.type === 'tool_result'}
							<div class="ml-0">
								<details class="bg-[#1a1a1a] border border-[#2a2a2a] rounded-lg overflow-hidden">
									<summary
										class="px-3 py-2 cursor-pointer hover:bg-[#222] flex items-center gap-2 text-sm text-gray-400"
									>
										<svg class="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												stroke-width="2"
												d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"
											/>
										</svg>
										<span>{message.toolName} result</span>
									</summary>
									<pre class="px-3 py-2 text-xs text-gray-400 overflow-x-auto max-h-48 bg-[#0d0d0d] border-t border-[#2a2a2a]">{message.content}</pre>
								</details>
							</div>
						{/if}
					{/each}

					<!-- Error -->
					{#if $chatError}
						<div class="bg-red-500/10 border border-red-500/30 text-red-400 px-4 py-3 rounded-lg flex items-center justify-between">
							<span class="text-sm">{$chatError}</span>
							<button on:click={() => chat.clearError()} class="text-red-400 hover:text-red-300">&times;</button>
						</div>
					{/if}
				</div>
			{/if}
		</div>

		<!-- Input Area -->
		<div class="border-t border-[#2a2a2a] bg-[#0d0d0d] p-4">
			<div class="max-w-3xl mx-auto">
				<!-- Uploaded Files -->
				{#if uploadedFiles.length > 0}
					<div class="mb-3 flex flex-wrap gap-2">
						{#each uploadedFiles as file, index}
							<div class="flex items-center gap-1.5 bg-[#1a1a1a] text-sm px-2.5 py-1 rounded-lg">
								<svg class="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path
										stroke-linecap="round"
										stroke-linejoin="round"
										stroke-width="2"
										d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
									/>
								</svg>
								<span class="text-gray-300 truncate max-w-[120px]" title={file.path}>{file.filename}</span>
								<button on:click={() => removeUploadedFile(index)} class="text-gray-500 hover:text-red-400">
									<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
									</svg>
								</button>
							</div>
						{/each}
					</div>
				{/if}

				<!-- Hidden file input -->
				<input type="file" bind:this={fileInput} on:change={handleFileSelect} class="hidden" multiple />

				<!-- Input Form -->
				<form on:submit|preventDefault={handleSubmit} class="flex items-end gap-3">
					<!-- File Button -->
					<button
						type="button"
						on:click={triggerFileUpload}
						class="p-2.5 text-gray-500 hover:text-white hover:bg-[#2a2a2a] rounded-lg transition-colors disabled:opacity-50"
						disabled={$isStreaming || !$claudeAuthenticated || isUploading}
						title={$selectedProject ? 'Upload file' : 'Select a project to upload files'}
					>
						{#if isUploading}
							<svg class="w-5 h-5 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
								<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
							</svg>
						{:else}
							<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
							</svg>
						{/if}
					</button>

					<!-- Textarea -->
					<div class="flex-1 relative">
						<textarea
							bind:this={textarea}
							bind:value={prompt}
							on:keydown={handleKeyDown}
							on:input={autoResize}
							placeholder="Message Claude..."
							class="w-full bg-[#1a1a1a] border border-[#2a2a2a] rounded-xl px-4 py-3 text-white placeholder-gray-500 resize-none focus:outline-none focus:ring-1 focus:ring-violet-500 focus:border-violet-500 min-h-[48px] max-h-[200px]"
							rows="1"
							disabled={$isStreaming || !$claudeAuthenticated}
						></textarea>
					</div>

					<!-- Send/Stop Button -->
					{#if $isStreaming}
						<button
							type="button"
							on:click={() => chat.stopGeneration()}
							class="p-2.5 bg-red-500/20 text-red-400 hover:bg-red-500/30 rounded-lg transition-colors"
						>
							<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
							</svg>
						</button>
					{:else}
						<button
							type="submit"
							class="p-2.5 bg-violet-600 hover:bg-violet-500 text-white rounded-lg transition-colors disabled:opacity-50 disabled:hover:bg-violet-600"
							disabled={!prompt.trim() || !$claudeAuthenticated}
						>
							<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
							</svg>
						</button>
					{/if}
				</form>

				<!-- Hints -->
				{#if !$claudeAuthenticated}
					<p class="mt-2 text-xs text-amber-500">
						Claude CLI not authenticated. Run <code class="bg-[#1a1a1a] px-1.5 py-0.5 rounded">docker exec -it ai-hub claude login</code>
					</p>
				{/if}
			</div>
		</div>
	</main>
</div>

<!-- Profile Modal -->
{#if showProfileModal}
	<div class="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4 backdrop-blur-sm">
		<div class="bg-[#171717] border border-[#2a2a2a] rounded-2xl w-full max-w-2xl max-h-[90vh] flex flex-col">
			<div class="p-4 border-b border-[#2a2a2a] flex items-center justify-between">
				<h2 class="text-lg font-semibold text-white">
					{showNewProfileForm ? (editingProfile ? 'Edit Profile' : 'New Profile') : 'Profiles'}
				</h2>
				<button
					class="text-gray-400 hover:text-white"
					on:click={() => {
						showProfileModal = false;
						showNewProfileForm = false;
						resetProfileForm();
					}}
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<div class="flex-1 overflow-y-auto p-4">
				{#if showNewProfileForm}
					<!-- Profile Form -->
					<div class="space-y-4">
						<div class="grid grid-cols-2 gap-4">
							<div>
								<label class="block text-xs text-gray-500 mb-1">ID</label>
								<input
									bind:value={profileForm.id}
									class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white"
									placeholder="my-profile"
									disabled={!!editingProfile}
								/>
							</div>
							<div>
								<label class="block text-xs text-gray-500 mb-1">Name</label>
								<input bind:value={profileForm.name} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white" placeholder="My Profile" />
							</div>
						</div>

						<div>
							<label class="block text-xs text-gray-500 mb-1">Description</label>
							<input bind:value={profileForm.description} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white" placeholder="Optional" />
						</div>

						<div class="grid grid-cols-3 gap-4">
							<div>
								<label class="block text-xs text-gray-500 mb-1">Model</label>
								<select bind:value={profileForm.model} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white">
									<option value="sonnet">Sonnet</option>
									<option value="opus">Opus</option>
									<option value="haiku">Haiku</option>
								</select>
							</div>
							<div>
								<label class="block text-xs text-gray-500 mb-1">Permission Mode</label>
								<select bind:value={profileForm.permission_mode} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white">
									<option value="default">Default</option>
									<option value="acceptEdits">Accept Edits</option>
									<option value="plan">Plan Only</option>
									<option value="bypassPermissions">Bypass</option>
								</select>
							</div>
							<div>
								<label class="block text-xs text-gray-500 mb-1">Max Turns</label>
								<input type="number" bind:value={profileForm.max_turns} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white" placeholder="Unlimited" />
							</div>
						</div>

						<div class="flex gap-3 pt-4">
							<button on:click={saveProfile} class="flex-1 py-2 bg-violet-600 hover:bg-violet-500 text-white rounded-lg text-sm font-medium">
								{editingProfile ? 'Save' : 'Create'}
							</button>
							<button
								on:click={() => {
									showNewProfileForm = false;
									resetProfileForm();
								}}
								class="flex-1 py-2 bg-[#2a2a2a] hover:bg-[#3a3a3a] text-white rounded-lg text-sm font-medium"
							>
								Cancel
							</button>
						</div>
					</div>
				{:else}
					<!-- Profile List -->
					<button on:click={openNewProfileForm} class="w-full py-2 mb-4 bg-[#2a2a2a] hover:bg-[#3a3a3a] text-white rounded-lg text-sm font-medium">
						+ New Profile
					</button>

					<div class="space-y-2">
						{#each $profiles as profile}
							<div class="p-3 bg-[#222] rounded-lg flex items-center justify-between">
								<div>
									<div class="flex items-center gap-2">
										<span class="text-sm text-white font-medium">{profile.name}</span>
										{#if profile.is_builtin}
											<span class="text-[10px] px-1.5 py-0.5 bg-violet-500/20 text-violet-400 rounded">Built-in</span>
										{/if}
									</div>
									{#if profile.description}
										<p class="text-xs text-gray-500 mt-0.5">{profile.description}</p>
									{/if}
								</div>
								{#if !profile.is_builtin}
									<div class="flex gap-1">
										<button on:click={() => editProfile(profile)} class="p-1.5 text-gray-500 hover:text-white">
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
											</svg>
										</button>
										<button on:click={() => deleteProfile(profile.id)} class="p-1.5 text-gray-500 hover:text-red-400">
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
											</svg>
										</button>
									</div>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			</div>
		</div>
	</div>
{/if}

<!-- Project Modal -->
{#if showProjectModal}
	<div class="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4 backdrop-blur-sm">
		<div class="bg-[#171717] border border-[#2a2a2a] rounded-2xl w-full max-w-lg max-h-[80vh] flex flex-col">
			<div class="p-4 border-b border-[#2a2a2a] flex items-center justify-between">
				<h2 class="text-lg font-semibold text-white">Projects</h2>
				<button
					class="text-gray-400 hover:text-white"
					on:click={() => {
						showProjectModal = false;
						showNewProjectForm = false;
					}}
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<div class="flex-1 overflow-y-auto p-4">
				{#if showNewProjectForm}
					<div class="space-y-4">
						<div>
							<label class="block text-xs text-gray-500 mb-1">ID</label>
							<input bind:value={newProjectId} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white" placeholder="my-project" />
						</div>
						<div>
							<label class="block text-xs text-gray-500 mb-1">Name</label>
							<input bind:value={newProjectName} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white" placeholder="My Project" />
						</div>
						<div>
							<label class="block text-xs text-gray-500 mb-1">Description</label>
							<textarea bind:value={newProjectDescription} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white resize-none" rows="2" placeholder="Optional"></textarea>
						</div>
						<p class="text-xs text-gray-500">
							Files: <code class="bg-[#0d0d0d] px-1 rounded">/workspace/{newProjectId || 'project-id'}/</code>
						</p>
						<div class="flex gap-3">
							<button on:click={createProject} class="flex-1 py-2 bg-violet-600 hover:bg-violet-500 text-white rounded-lg text-sm font-medium">Create</button>
							<button on:click={() => (showNewProjectForm = false)} class="flex-1 py-2 bg-[#2a2a2a] hover:bg-[#3a3a3a] text-white rounded-lg text-sm font-medium">Cancel</button>
						</div>
					</div>
				{:else}
					<button on:click={() => (showNewProjectForm = true)} class="w-full py-2 mb-4 bg-[#2a2a2a] hover:bg-[#3a3a3a] text-white rounded-lg text-sm font-medium">
						+ New Project
					</button>

					{#if $projects.length === 0}
						<p class="text-sm text-gray-500 text-center py-6">No projects yet</p>
					{:else}
						<div class="space-y-2">
							{#each $projects as project}
								<div class="p-3 bg-[#222] rounded-lg flex items-center justify-between">
									<div>
										<span class="text-sm text-white font-medium">{project.name}</span>
										{#if project.description}
											<p class="text-xs text-gray-500 mt-0.5">{project.description}</p>
										{/if}
										<p class="text-xs text-gray-600 mt-0.5">/workspace/{project.path}/</p>
									</div>
									<button on:click={() => deleteProject(project.id)} class="text-gray-500 hover:text-red-400">
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
										</svg>
									</button>
								</div>
							{/each}
						</div>
					{/if}
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.prose :global(pre) {
		@apply bg-[#0d0d0d] rounded-lg p-3 overflow-x-auto my-3;
	}

	.prose :global(code) {
		@apply bg-[#1a1a1a] px-1.5 py-0.5 rounded text-violet-300;
	}

	.prose :global(pre code) {
		@apply bg-transparent p-0;
	}

	.prose :global(p) {
		@apply mb-3 leading-relaxed;
	}

	.prose :global(ul),
	.prose :global(ol) {
		@apply mb-3 pl-5;
	}

	.prose :global(li) {
		@apply mb-1;
	}

	.prose :global(h1),
	.prose :global(h2),
	.prose :global(h3) {
		@apply font-semibold mt-4 mb-2;
	}

	.prose :global(a) {
		@apply text-violet-400 hover:text-violet-300;
	}

	.prose :global(blockquote) {
		@apply border-l-2 border-violet-500 pl-4 italic text-gray-400 my-3;
	}
</style>
