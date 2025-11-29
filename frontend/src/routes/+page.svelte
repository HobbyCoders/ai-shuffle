<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { auth, username, claudeAuthenticated, isAuthenticated } from '$lib/stores/auth';
	import {
		tabs,
		allTabs,
		activeTabId,
		activeTab,
		profiles,
		projects,
		sessions,
		defaultProfile,
		defaultProject,
		type ChatMessage,
		type ChatTab
	} from '$lib/stores/tabs';
	import { api, type FileUploadResponse } from '$lib/api/client';
	import { marked } from 'marked';

	// Per-tab state (we track input per tab)
	let tabInputs: Record<string, string> = {};
	let tabUploadedFiles: Record<string, FileUploadResponse[]> = {};

	let messagesContainers: Record<string, HTMLElement> = {};
	let sidebarOpen = false;
	let shouldAutoScroll: Record<string, boolean> = {};
	let lastMessageCounts: Record<string, number> = {};
	let lastContentLengths: Record<string, number> = {};
	let showProfileModal = false;
	let showProjectModal = false;
	let showNewProfileForm = false;
	let showNewProjectForm = false;
	let editingProfile: any = null;
	let fileInput: HTMLInputElement;
	let isUploading = false;
	let textareas: Record<string, HTMLTextAreaElement> = {};

	// Accordion states for profile form sections
	let expandedSections: Record<string, boolean> = {
		toolConfig: false,
		behavior: false,
		systemPrompt: false,
		settingSources: false,
		advanced: false
	};

	function toggleSection(section: string) {
		expandedSections[section] = !expandedSections[section];
		expandedSections = expandedSections; // Trigger reactivity
	}

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

	// Track auth state
	let wasAuthenticated = false;

	// Initialize tabs when authenticated
	$: if ($isAuthenticated && !wasAuthenticated) {
		wasAuthenticated = true;
		tabs.init();
		tabs.loadProfiles();
		tabs.loadSessions();
		tabs.loadProjects();
	} else if (!$isAuthenticated && wasAuthenticated) {
		wasAuthenticated = false;
	}

	onMount(() => {
		if ($isAuthenticated) {
			wasAuthenticated = true;
			tabs.init();
			Promise.all([
				tabs.loadProfiles(),
				tabs.loadSessions(),
				tabs.loadProjects()
			]);
		}
	});

	onDestroy(() => {
		tabs.destroy();
	});

	// Get prompt for current tab
	function getTabPrompt(tabId: string): string {
		return tabInputs[tabId] || '';
	}

	function setTabPrompt(tabId: string, value: string) {
		tabInputs[tabId] = value;
		tabInputs = tabInputs; // Trigger reactivity
	}

	// Auto-scroll for active tab
	$: if ($activeTab && messagesContainers[$activeTab.id]) {
		const tabId = $activeTab.id;
		const container = messagesContainers[tabId];
		const messages = $activeTab.messages;

		if (messages.length > 0) {
			const newMessageArrived = messages.length > (lastMessageCounts[tabId] || 0);
			lastMessageCounts[tabId] = messages.length;

			const totalContentLength = messages.reduce((sum, m) => sum + (m.content?.length || 0), 0);
			const contentUpdated = totalContentLength > (lastContentLengths[tabId] || 0);
			lastContentLengths[tabId] = totalContentLength;

			const autoScroll = shouldAutoScroll[tabId] !== false;
			if ((newMessageArrived || (contentUpdated && $activeTab.isStreaming)) && autoScroll) {
				setTimeout(() => {
					if (container && isNearBottom(container, 150)) {
						container.scrollTop = container.scrollHeight;
					}
				}, 10);
			}
		}
	}

	function isNearBottom(container: HTMLElement, threshold = 100): boolean {
		if (!container) return true;
		const { scrollTop, scrollHeight, clientHeight } = container;
		return scrollHeight - scrollTop - clientHeight < threshold;
	}

	function handleScroll(tabId: string) {
		const container = messagesContainers[tabId];
		if (!container) return;
		shouldAutoScroll[tabId] = isNearBottom(container, 100);
	}

	async function handleSubmit(tabId: string) {
		const prompt = getTabPrompt(tabId);
		if (!prompt.trim() || !$activeTab || $activeTab.isStreaming) return;

		setTabPrompt(tabId, '');
		tabUploadedFiles[tabId] = [];
		tabs.sendMessage(tabId, prompt);
	}

	function handleKeyDown(e: KeyboardEvent, tabId: string) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			handleSubmit(tabId);
		}
	}

	function autoResize(tabId: string) {
		const textarea = textareas[tabId];
		if (textarea) {
			textarea.style.height = 'auto';
			textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
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

	function truncateTitle(title: string | null, maxLength: number = 35): string {
		if (!title) return 'New Chat';
		return title.length > maxLength ? title.substring(0, maxLength) + '...' : title;
	}

	// Open session in new tab
	function openSessionInNewTab(sessionId: string) {
		tabs.createTab(sessionId);
		sidebarOpen = false;
	}

	// Open session in current tab
	async function openSessionInCurrentTab(sessionId: string) {
		if ($activeTabId) {
			await tabs.loadSessionInTab($activeTabId, sessionId);
		}
		sidebarOpen = false;
	}

	async function deleteSession(e: Event, sessionId: string) {
		e.stopPropagation();
		if (confirm('Delete this session?')) {
			await tabs.deleteSession(sessionId);
		}
	}

	function handleNewTab() {
		tabs.createTab();
	}

	function handleCloseTab(e: Event, tabId: string) {
		e.stopPropagation();
		tabs.closeTab(tabId);
	}

	function handleNewChatInTab() {
		if ($activeTabId) {
			tabs.startNewChatInTab($activeTabId);
		}
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
			await tabs.updateProfile(profileForm.id, {
				name: profileForm.name,
				description: profileForm.description || undefined,
				config
			});
		} else {
			await tabs.createProfile({
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
			await tabs.deleteProfile(profileId);
		}
	}

	async function createProject() {
		if (!newProjectId || !newProjectName) return;

		await tabs.createProject({
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
			await tabs.deleteProject(projectId);
		}
	}

	function triggerFileUpload() {
		if (!$activeTab?.project) {
			alert('Please select a project first to upload files.');
			return;
		}
		fileInput?.click();
	}

	async function handleFileSelect(event: Event) {
		const input = event.target as HTMLInputElement;
		const files = input.files;
		if (!files || files.length === 0 || !$activeTab?.project || !$activeTabId) return;

		isUploading = true;
		const tabId = $activeTabId;
		try {
			for (const file of Array.from(files)) {
				const result = await api.uploadFile(`/projects/${$activeTab.project}/upload`, file);
				if (!tabUploadedFiles[tabId]) tabUploadedFiles[tabId] = [];
				tabUploadedFiles[tabId] = [...tabUploadedFiles[tabId], result];

				const fileRef = `[File: ${result.path}]`;
				const currentPrompt = getTabPrompt(tabId);
				if (currentPrompt.trim()) {
					setTabPrompt(tabId, currentPrompt + '\n' + fileRef);
				} else {
					setTabPrompt(tabId, fileRef);
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

	function removeUploadedFile(tabId: string, index: number) {
		const files = tabUploadedFiles[tabId] || [];
		const file = files[index];
		if (!file) return;

		const fileRef = `[File: ${file.path}]`;
		let prompt = getTabPrompt(tabId);
		prompt = prompt.replace(fileRef, '').replace(/\n\n+/g, '\n').trim();
		setTabPrompt(tabId, prompt);
		tabUploadedFiles[tabId] = files.filter((_, i) => i !== index);
	}

	function toggleSettingSource(source: string) {
		if (profileForm.setting_sources.includes(source)) {
			profileForm.setting_sources = profileForm.setting_sources.filter((s) => s !== source);
		} else {
			profileForm.setting_sources = [...profileForm.setting_sources, source];
		}
	}

	function setTabProfile(tabId: string, profileId: string) {
		tabs.setTabProfile(tabId, profileId);
		tabs.setDefaultProfile(profileId);
	}

	function setTabProject(tabId: string, projectId: string) {
		tabs.setTabProject(tabId, projectId);
		tabs.setDefaultProject(projectId);
	}
</script>

<svelte:head>
	<title>AI Hub</title>
</svelte:head>

<div class="h-screen flex bg-[#0d0d0d] text-gray-100">
	<!-- Sidebar -->
	<aside
		class="fixed inset-y-0 left-0 z-40 w-72 bg-[#171717] border-r border-[#2a2a2a] transform transition-transform duration-200 lg:relative lg:translate-x-0 {sidebarOpen
			? 'translate-x-0'
			: '-translate-x-full'}"
	>
		<div class="h-full flex flex-col">
			<!-- Sidebar Header -->
			<div class="p-4 border-b border-[#2a2a2a] flex items-center justify-between">
				<div class="flex items-center gap-2">
					<div class="w-8 h-8 rounded-lg bg-gradient-to-br from-violet-500 to-purple-600 flex items-center justify-center">
						<svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
						</svg>
					</div>
					<span class="font-semibold text-white">AI Hub</span>
				</div>
				<button class="lg:hidden text-gray-400 hover:text-white p-1" on:click={() => (sidebarOpen = false)}>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<!-- New Chat Button -->
			<div class="p-3">
				<button
					on:click={handleNewTab}
					class="w-full flex items-center gap-2 px-4 py-2.5 bg-violet-600 hover:bg-violet-500 text-white rounded-lg transition-colors"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
					</svg>
					<span class="font-medium">New Tab</span>
				</button>
			</div>

			<!-- Session History -->
			<div class="flex-1 overflow-y-auto px-3 pb-3">
				<div class="text-xs text-gray-500 uppercase tracking-wider px-2 mb-2">History</div>
				<div class="space-y-1">
					{#each $sessions as session}
						<div
							class="group flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-[#252525] cursor-pointer transition-colors"
							on:click={() => openSessionInNewTab(session.id)}
							on:keypress={(e) => e.key === 'Enter' && openSessionInNewTab(session.id)}
							role="button"
							tabindex="0"
						>
							<div class="flex-1 min-w-0">
								<p class="text-sm text-gray-300 truncate">{truncateTitle(session.title)}</p>
								<p class="text-xs text-gray-600">{formatDate(session.updated_at)}</p>
							</div>
							<button
								on:click|stopPropagation={(e) => deleteSession(e, session.id)}
								class="opacity-0 group-hover:opacity-100 p-1 text-gray-500 hover:text-red-400 transition-opacity"
								title="Delete session"
							>
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
								</svg>
							</button>
						</div>
					{/each}
				</div>
			</div>

			<!-- Sidebar Footer -->
			<div class="p-3 border-t border-[#2a2a2a]">
				<div class="flex items-center justify-between">
					<div class="flex items-center gap-2">
						<div class="w-8 h-8 rounded-full bg-violet-600/20 flex items-center justify-center">
							<span class="text-sm text-violet-400">{$username?.[0]?.toUpperCase() || 'U'}</span>
						</div>
						<span class="text-sm text-gray-400">{$username}</span>
					</div>
					<div class="flex items-center gap-2">
						<a href="/settings" class="text-gray-500 hover:text-white p-1">
							<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
		</div>
	</aside>

	<!-- Main Content -->
	<main class="flex-1 flex flex-col min-w-0 bg-[#0d0d0d]">
		<!-- Tab Bar -->
		<div class="bg-[#171717] border-b border-[#2a2a2a] flex items-center">
			<!-- Mobile Menu Button -->
			<button class="lg:hidden p-3 text-gray-400 hover:text-white" on:click={() => (sidebarOpen = true)}>
				<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
				</svg>
			</button>

			<!-- Tabs -->
			<div class="flex-1 flex items-center overflow-x-auto scrollbar-hide" role="tablist">
				{#each $allTabs as tab}
					<div
						class="group flex items-center gap-2 px-3 sm:px-4 py-2.5 border-r border-[#2a2a2a] min-w-[100px] sm:min-w-[120px] max-w-[160px] sm:max-w-[200px] transition-colors cursor-pointer {tab.id === $activeTabId
							? 'bg-[#0d0d0d] text-white'
							: 'text-gray-400 hover:text-white hover:bg-[#1a1a1a]'}"
						on:click={() => tabs.setActiveTab(tab.id)}
						on:keypress={(e) => e.key === 'Enter' && tabs.setActiveTab(tab.id)}
						role="tab"
						tabindex="0"
						aria-selected={tab.id === $activeTabId}
					>
						<span class="flex-1 truncate text-xs sm:text-sm text-left">{tab.title}</span>
						{#if tab.isStreaming}
							<span class="w-2 h-2 bg-violet-500 rounded-full animate-pulse flex-shrink-0"></span>
						{:else if !tab.wsConnected}
							<span class="w-2 h-2 bg-yellow-500 rounded-full flex-shrink-0" title="Disconnected"></span>
						{/if}
						{#if $allTabs.length > 1}
							<button
								on:click|stopPropagation={(e) => handleCloseTab(e, tab.id)}
								class="opacity-0 group-hover:opacity-100 p-0.5 hover:bg-[#333] rounded transition-opacity flex-shrink-0"
								title="Close tab"
								aria-label="Close tab"
							>
								<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
								</svg>
							</button>
						{/if}
					</div>
				{/each}
			</div>

			<!-- Add Tab Button -->
			<button
				on:click={handleNewTab}
				class="p-3 text-gray-400 hover:text-white hover:bg-[#1a1a1a] transition-colors"
				title="New tab"
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
				</svg>
			</button>
		</div>

		<!-- Active Tab Content -->
		{#if $activeTab}
			{@const currentTab = $activeTab}
			{@const tabId = currentTab.id}

			<!-- Profile/Project Selector Bar -->
			<div class="bg-[#171717] border-b border-[#2a2a2a] px-2 sm:px-4 py-2 flex flex-wrap sm:flex-nowrap items-center gap-2 sm:gap-4">
				<!-- Profile Selector -->
				<div class="flex items-center gap-1 sm:gap-2">
					<span class="text-xs text-gray-500 hidden sm:inline">Profile:</span>
					<select
						value={currentTab.profile}
						on:change={(e) => setTabProfile(tabId, e.currentTarget.value)}
						class="bg-[#1a1a1a] text-xs sm:text-sm text-gray-300 border-0 rounded px-2 py-1 focus:ring-1 focus:ring-violet-500 max-w-[100px] sm:max-w-none"
						aria-label="Profile"
					>
						{#each $profiles as profile}
							<option value={profile.id}>{profile.name}</option>
						{/each}
					</select>
					<button on:click={() => (showProfileModal = true)} class="text-gray-500 hover:text-white p-1" aria-label="Profile settings">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
							/>
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
						</svg>
					</button>
				</div>

				<!-- Project Selector -->
				<div class="flex items-center gap-1 sm:gap-2">
					<span class="text-xs text-gray-500 hidden sm:inline">Project:</span>
					<select
						value={currentTab.project}
						on:change={(e) => setTabProject(tabId, e.currentTarget.value)}
						class="bg-[#1a1a1a] text-xs sm:text-sm text-gray-300 border-0 rounded px-2 py-1 focus:ring-1 focus:ring-violet-500 max-w-[100px] sm:max-w-none"
						aria-label="Project"
					>
						<option value="">Default</option>
						{#each $projects as project}
							<option value={project.id}>{project.name}</option>
						{/each}
					</select>
					<button on:click={() => (showProjectModal = true)} class="text-gray-500 hover:text-white p-1" aria-label="Project settings">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"
							/>
						</svg>
					</button>
				</div>

				<!-- Connection Status -->
				<div class="flex-1"></div>
				<div class="flex items-center gap-1 sm:gap-2 text-xs">
					{#if currentTab.wsConnected}
						<span class="flex items-center gap-1 text-green-400">
							<span class="w-2 h-2 bg-green-400 rounded-full"></span>
							<span class="hidden sm:inline">Connected</span>
						</span>
					{:else}
						<span class="flex items-center gap-1 text-yellow-400">
							<span class="w-2 h-2 bg-yellow-400 rounded-full animate-pulse"></span>
							<span class="hidden sm:inline">Connecting...</span>
						</span>
					{/if}
				</div>
			</div>

			<!-- Messages Area -->
			<div
				bind:this={messagesContainers[tabId]}
				on:scroll={() => handleScroll(tabId)}
				class="flex-1 overflow-y-auto"
			>
				{#if currentTab.messages.length === 0}
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
								<span class="text-gray-400 ml-1">{$profiles.find((p) => p.id === currentTab.profile)?.name || currentTab.profile}</span>
							</div>
						</div>
					</div>
				{:else}
					<!-- Messages -->
					<div class="max-w-3xl mx-auto px-4 py-6 space-y-6">
						{#each currentTab.messages as message}
							{#if message.role === 'user'}
								<div class="flex flex-col">
									<div class="text-xs text-gray-500 mb-1.5 font-medium">You</div>
									<div class="bg-[#1a1a1a] rounded-2xl px-4 py-3 text-gray-100">
										<p class="whitespace-pre-wrap">{message.content}</p>
									</div>
								</div>
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
									{#if message.metadata && !message.streaming}
										<div class="mt-3 text-xs text-gray-600 flex items-center gap-3">
											{#if message.metadata.total_cost_usd}
												<span>{formatCost(message.metadata.total_cost_usd as number)}</span>
											{/if}
											{#if message.metadata.duration_ms}
												<span>{((message.metadata.duration_ms as number) / 1000).toFixed(1)}s</span>
											{/if}
										</div>
									{/if}
								</div>
							{:else if message.type === 'tool_use'}
								<div class="ml-0">
									<div class="inline-flex items-center gap-2 bg-[#1e1e2e] border border-[#2a2a2a] rounded-lg px-3 py-2 text-sm">
										{#if message.streaming}
											<svg class="w-4 h-4 text-blue-400 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
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
							{:else if message.type === 'tool_result'}
								<div class="ml-0">
									<details class="bg-[#1a1a1a] border border-[#2a2a2a] rounded-lg overflow-hidden">
										<summary class="px-3 py-2 cursor-pointer hover:bg-[#222] flex items-center gap-2 text-sm text-gray-400">
											<svg class="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
											</svg>
											<span>{message.toolName} result</span>
										</summary>
										<pre class="px-3 py-2 text-xs text-gray-400 overflow-x-auto max-h-48 bg-[#0d0d0d] border-t border-[#2a2a2a]">{message.content}</pre>
									</details>
								</div>
							{/if}
						{/each}

						<!-- Error -->
						{#if currentTab.error}
							<div class="bg-red-500/10 border border-red-500/30 text-red-400 px-4 py-3 rounded-lg flex items-center justify-between">
								<span class="text-sm">{currentTab.error}</span>
								<button on:click={() => tabs.clearTabError(tabId)} class="text-red-400 hover:text-red-300">&times;</button>
							</div>
						{/if}
					</div>
				{/if}
			</div>

			<!-- Input Area -->
			<div class="border-t border-[#2a2a2a] bg-[#0d0d0d] p-4">
				<div class="max-w-3xl mx-auto">
					<!-- Uploaded Files -->
					{#if (tabUploadedFiles[tabId] || []).length > 0}
						<div class="mb-3 flex flex-wrap gap-2">
							{#each tabUploadedFiles[tabId] as file, index}
								<div class="flex items-center gap-1.5 bg-[#1a1a1a] text-sm px-2.5 py-1 rounded-lg">
									<svg class="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
									</svg>
									<span class="text-gray-300 truncate max-w-[120px]" title={file.path}>{file.filename}</span>
									<button on:click={() => removeUploadedFile(tabId, index)} class="text-gray-500 hover:text-red-400">
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
					<form on:submit|preventDefault={() => handleSubmit(tabId)} class="flex items-center gap-2">
						<!-- File Button -->
						<button
							type="button"
							on:click={triggerFileUpload}
							class="flex-shrink-0 w-10 h-10 flex items-center justify-center text-gray-500 hover:text-white hover:bg-[#2a2a2a] rounded-lg transition-colors disabled:opacity-50"
							disabled={currentTab.isStreaming || !$claudeAuthenticated || isUploading}
							title={currentTab.project ? 'Upload file' : 'Select a project to upload files'}
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
								bind:this={textareas[tabId]}
								value={getTabPrompt(tabId)}
								on:input={(e) => {
									setTabPrompt(tabId, e.currentTarget.value);
									autoResize(tabId);
								}}
								on:keydown={(e) => handleKeyDown(e, tabId)}
								placeholder="Message Claude..."
								class="w-full bg-[#1a1a1a] border border-[#2a2a2a] rounded-xl px-4 py-2.5 text-white placeholder-gray-500 resize-none focus:outline-none focus:ring-1 focus:ring-violet-500 focus:border-violet-500 min-h-[40px] max-h-[200px] leading-normal"
								rows="1"
								disabled={currentTab.isStreaming || !$claudeAuthenticated}
							></textarea>
						</div>

						<!-- Send/Stop Button -->
						{#if currentTab.isStreaming}
							<button
								type="button"
								on:click={() => tabs.stopGeneration(tabId)}
								class="flex-shrink-0 w-10 h-10 flex items-center justify-center bg-red-500/20 text-red-400 hover:bg-red-500/30 rounded-lg transition-colors"
							>
								<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
								</svg>
							</button>
						{:else}
							<button
								type="submit"
								class="flex-shrink-0 w-10 h-10 flex items-center justify-center bg-violet-600 hover:bg-violet-500 text-white rounded-lg transition-colors disabled:opacity-50 disabled:hover:bg-violet-600"
								disabled={!(tabInputs[tabId] || '').trim() || !$claudeAuthenticated}
							>
								<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
								</svg>
							</button>
						{/if}
					</form>
				</div>
			</div>
		{/if}
	</main>
</div>

<!-- Profile Modal -->
{#if showProfileModal}
	<div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" on:click={() => (showProfileModal = false)}>
		<div class="bg-[#1a1a1a] rounded-xl w-full max-w-lg max-h-[80vh] overflow-y-auto" on:click|stopPropagation>
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

			<div class="p-4">
				{#if showNewProfileForm}
					<div class="space-y-4">
						<div class="grid grid-cols-2 gap-4">
							<div>
								<label class="block text-xs text-gray-500 mb-1">ID</label>
								<input
									bind:value={profileForm.id}
									disabled={!!editingProfile}
									class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white disabled:opacity-50"
									placeholder="my-profile"
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
									<option value="plan">Plan</option>
									<option value="bypassPermissions">Bypass</option>
								</select>
							</div>
							<div>
								<label class="block text-xs text-gray-500 mb-1">Max Turns</label>
								<input type="number" bind:value={profileForm.max_turns} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white" placeholder="Unlimited" />
							</div>
						</div>

						<!-- Tool Configuration Accordion -->
						<div class="border border-[#2a2a2a] rounded-lg overflow-hidden">
							<button
								type="button"
								on:click={() => toggleSection('toolConfig')}
								class="w-full px-3 py-2 bg-[#252525] flex items-center justify-between text-sm text-gray-300 hover:bg-[#2a2a2a]"
							>
								<span>Tool Configuration</span>
								<svg class="w-4 h-4 transition-transform {expandedSections.toolConfig ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>
							{#if expandedSections.toolConfig}
								<div class="p-3 space-y-3 bg-[#1a1a1a]">
									<div>
										<label class="block text-xs text-gray-500 mb-1">Allowed Tools</label>
										<input bind:value={profileForm.allowed_tools} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white" placeholder="Read, Write, Bash (comma-separated)" />
										<p class="text-xs text-gray-600 mt-1">Empty = all tools allowed</p>
									</div>
									<div>
										<label class="block text-xs text-gray-500 mb-1">Disallowed Tools</label>
										<input bind:value={profileForm.disallowed_tools} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white" placeholder="Write, Edit (comma-separated)" />
									</div>
								</div>
							{/if}
						</div>

						<!-- Behavior Settings Accordion -->
						<div class="border border-[#2a2a2a] rounded-lg overflow-hidden">
							<button
								type="button"
								on:click={() => toggleSection('behavior')}
								class="w-full px-3 py-2 bg-[#252525] flex items-center justify-between text-sm text-gray-300 hover:bg-[#2a2a2a]"
							>
								<span>Behavior Settings</span>
								<svg class="w-4 h-4 transition-transform {expandedSections.behavior ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>
							{#if expandedSections.behavior}
								<div class="p-3 space-y-3 bg-[#1a1a1a]">
									<label class="flex items-center gap-2 cursor-pointer">
										<input type="checkbox" bind:checked={profileForm.include_partial_messages} class="w-4 h-4 rounded bg-[#2a2a2a] border-0 text-violet-600 focus:ring-violet-500" />
										<div>
											<span class="text-sm text-gray-300">Include Partial Messages</span>
											<p class="text-xs text-gray-600">Stream partial text as it's being generated</p>
										</div>
									</label>
									<label class="flex items-center gap-2 cursor-pointer">
										<input type="checkbox" bind:checked={profileForm.continue_conversation} class="w-4 h-4 rounded bg-[#2a2a2a] border-0 text-violet-600 focus:ring-violet-500" />
										<div>
											<span class="text-sm text-gray-300">Continue Conversation</span>
											<p class="text-xs text-gray-600">Automatically continue most recent conversation</p>
										</div>
									</label>
									<label class="flex items-center gap-2 cursor-pointer">
										<input type="checkbox" bind:checked={profileForm.fork_session} class="w-4 h-4 rounded bg-[#2a2a2a] border-0 text-violet-600 focus:ring-violet-500" />
										<div>
											<span class="text-sm text-gray-300">Fork Session</span>
											<p class="text-xs text-gray-600">Create new session ID when resuming</p>
										</div>
									</label>
								</div>
							{/if}
						</div>

						<!-- System Prompt Accordion -->
						<div class="border border-[#2a2a2a] rounded-lg overflow-hidden">
							<button
								type="button"
								on:click={() => toggleSection('systemPrompt')}
								class="w-full px-3 py-2 bg-[#252525] flex items-center justify-between text-sm text-gray-300 hover:bg-[#2a2a2a]"
							>
								<span>System Prompt</span>
								<svg class="w-4 h-4 transition-transform {expandedSections.systemPrompt ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>
							{#if expandedSections.systemPrompt}
								<div class="p-3 space-y-3 bg-[#1a1a1a]">
									<div>
										<label class="block text-xs text-gray-500 mb-1">Prompt Type</label>
										<select bind:value={profileForm.system_prompt_type} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white">
											<option value="preset">Use Claude Code Preset</option>
											<option value="custom">Custom Prompt</option>
										</select>
									</div>
									{#if profileForm.system_prompt_type === 'preset'}
										<div>
											<label class="block text-xs text-gray-500 mb-1">Preset</label>
											<select bind:value={profileForm.system_prompt_preset} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white">
												<option value="claude_code">Claude Code</option>
												<option value="default">Default</option>
											</select>
										</div>
										<div>
											<label class="block text-xs text-gray-500 mb-1">Append Instructions</label>
											<textarea bind:value={profileForm.system_prompt_append} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white resize-none" rows="3" placeholder="Additional instructions to append to the system prompt..."></textarea>
										</div>
									{:else}
										<div>
											<label class="block text-xs text-gray-500 mb-1">Custom System Prompt</label>
											<textarea bind:value={profileForm.system_prompt_content} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white resize-none" rows="4" placeholder="Enter your custom system prompt..."></textarea>
										</div>
									{/if}
								</div>
							{/if}
						</div>

						<!-- Settings Sources Accordion -->
						<div class="border border-[#2a2a2a] rounded-lg overflow-hidden">
							<button
								type="button"
								on:click={() => toggleSection('settingSources')}
								class="w-full px-3 py-2 bg-[#252525] flex items-center justify-between text-sm text-gray-300 hover:bg-[#2a2a2a]"
							>
								<span>Settings Sources</span>
								<svg class="w-4 h-4 transition-transform {expandedSections.settingSources ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>
							{#if expandedSections.settingSources}
								<div class="p-3 bg-[#1a1a1a]">
									<p class="text-xs text-gray-500 mb-2">Load settings from filesystem locations</p>
									<div class="flex flex-wrap gap-3">
										<label class="flex items-center gap-2 cursor-pointer">
											<input type="checkbox" checked={profileForm.setting_sources.includes('user')} on:change={() => toggleSettingSource('user')} class="w-4 h-4 rounded bg-[#2a2a2a] border-0 text-violet-600 focus:ring-violet-500" />
											<span class="text-sm text-gray-300">User (~/.claude)</span>
										</label>
										<label class="flex items-center gap-2 cursor-pointer">
											<input type="checkbox" checked={profileForm.setting_sources.includes('project')} on:change={() => toggleSettingSource('project')} class="w-4 h-4 rounded bg-[#2a2a2a] border-0 text-violet-600 focus:ring-violet-500" />
											<span class="text-sm text-gray-300">Project (.claude)</span>
										</label>
										<label class="flex items-center gap-2 cursor-pointer">
											<input type="checkbox" checked={profileForm.setting_sources.includes('local')} on:change={() => toggleSettingSource('local')} class="w-4 h-4 rounded bg-[#2a2a2a] border-0 text-violet-600 focus:ring-violet-500" />
											<span class="text-sm text-gray-300">Local</span>
										</label>
									</div>
								</div>
							{/if}
						</div>

						<!-- Advanced Settings Accordion -->
						<div class="border border-[#2a2a2a] rounded-lg overflow-hidden">
							<button
								type="button"
								on:click={() => toggleSection('advanced')}
								class="w-full px-3 py-2 bg-[#252525] flex items-center justify-between text-sm text-gray-300 hover:bg-[#2a2a2a]"
							>
								<span>Advanced Settings</span>
								<svg class="w-4 h-4 transition-transform {expandedSections.advanced ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>
							{#if expandedSections.advanced}
								<div class="p-3 space-y-3 bg-[#1a1a1a]">
									<div class="grid grid-cols-2 gap-3">
										<div>
											<label class="block text-xs text-gray-500 mb-1">Working Directory</label>
											<input bind:value={profileForm.cwd} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white" placeholder="/workspace/my-project" />
										</div>
										<div>
											<label class="block text-xs text-gray-500 mb-1">User Identifier</label>
											<input bind:value={profileForm.user} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white" placeholder="user@example.com" />
										</div>
									</div>
									<div>
										<label class="block text-xs text-gray-500 mb-1">Additional Directories</label>
										<input bind:value={profileForm.add_dirs} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white" placeholder="/extra/dir1, /extra/dir2 (comma-separated)" />
									</div>
									<div>
										<label class="block text-xs text-gray-500 mb-1">Max Buffer Size (bytes)</label>
										<input type="number" bind:value={profileForm.max_buffer_size} class="w-full bg-[#2a2a2a] border-0 rounded-lg px-3 py-2 text-sm text-white" placeholder="Default" />
									</div>
								</div>
							{/if}
						</div>

						<div class="flex gap-2 pt-4">
							<button
								on:click={() => {
									showNewProfileForm = false;
									resetProfileForm();
								}}
								class="flex-1 px-4 py-2 bg-[#2a2a2a] text-gray-300 rounded-lg hover:bg-[#333]"
							>
								Cancel
							</button>
							<button on:click={saveProfile} class="flex-1 px-4 py-2 bg-violet-600 text-white rounded-lg hover:bg-violet-500"> Save </button>
						</div>
					</div>
				{:else}
					<div class="space-y-2 mb-4">
						{#each $profiles as profile}
							<div class="flex items-center justify-between p-3 bg-[#252525] rounded-lg">
								<div>
									<p class="text-sm text-white font-medium">{profile.name}</p>
									<p class="text-xs text-gray-500">{profile.id}</p>
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
					<button on:click={openNewProfileForm} class="w-full py-2 border border-dashed border-[#2a2a2a] rounded-lg text-gray-500 hover:text-white hover:border-gray-500">
						+ New Profile
					</button>
				{/if}
			</div>
		</div>
	</div>
{/if}

<!-- Project Modal -->
{#if showProjectModal}
	<div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" on:click={() => (showProjectModal = false)}>
		<div class="bg-[#1a1a1a] rounded-xl w-full max-w-lg max-h-[80vh] overflow-y-auto" on:click|stopPropagation>
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

			<div class="p-4">
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
						<div class="flex gap-2">
							<button on:click={() => (showNewProjectForm = false)} class="flex-1 px-4 py-2 bg-[#2a2a2a] text-gray-300 rounded-lg hover:bg-[#333]"> Cancel </button>
							<button on:click={createProject} class="flex-1 px-4 py-2 bg-violet-600 text-white rounded-lg hover:bg-violet-500"> Create </button>
						</div>
					</div>
				{:else}
					<div class="space-y-2 mb-4">
						{#each $projects as project}
							<div class="flex items-center justify-between p-3 bg-[#252525] rounded-lg">
								<div>
									<p class="text-sm text-white font-medium">{project.name}</p>
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
					<button on:click={() => (showNewProjectForm = true)} class="w-full py-2 border border-dashed border-[#2a2a2a] rounded-lg text-gray-500 hover:text-white hover:border-gray-500">
						+ New Project
					</button>
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.scrollbar-hide::-webkit-scrollbar {
		display: none;
	}
	.scrollbar-hide {
		-ms-overflow-style: none;
		scrollbar-width: none;
	}
</style>
