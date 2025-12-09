<script lang="ts">
	import { onMount, onDestroy, tick } from 'svelte';
	import { goto } from '$app/navigation';
	import { auth, username, claudeAuthenticated, isAuthenticated, isAdmin, apiUser } from '$lib/stores/auth';
	import {
		tabs,
		allTabs,
		activeTabId,
		activeTab,
		profiles,
		projects,
		sessions,
		adminSessions,
		apiUsers,
		adminSessionsFilter,
		selectedSessionIds,
		selectedAdminSessionIds,
		selectionMode,
		adminSelectionMode,
		sessionsLoading,
		type ChatMessage,
		type ChatTab,
		type ApiUser
	} from '$lib/stores/tabs';
	import { api, type FileUploadResponse, searchSessions, type SessionSearchResult } from '$lib/api/client';
	import { marked } from 'marked';
	import TerminalModal from '$lib/components/TerminalModal.svelte';
	import RewindModal from '$lib/components/RewindModal.svelte';
	import SystemMessage from '$lib/components/SystemMessage.svelte';
	import CommandAutocomplete from '$lib/components/CommandAutocomplete.svelte';
	import FileAutocomplete, { type FileItem } from '$lib/components/FileAutocomplete.svelte';
	import SpotlightSearch from '$lib/components/SpotlightSearch.svelte';
	import SubagentMessage from '$lib/components/SubagentMessage.svelte';
	import QuickActions from '$lib/components/QuickActions.svelte';
	import SubagentManager from '$lib/components/SubagentManager.svelte';
	import SessionCard from '$lib/components/SessionCard.svelte';
	import ConnectionStatus from '$lib/components/ConnectionStatus.svelte';
	import PermissionQueue from '$lib/components/PermissionQueue.svelte';
	import UserQuestion from '$lib/components/UserQuestion.svelte';
	import TodoList from '$lib/components/TodoList.svelte';
	import { groups, organizeByGroups } from '$lib/stores/groups';
	import { executeCommand, isSlashCommand, syncAfterRewind, listCommands, type Command } from '$lib/api/commands';
	import { groupSessionsByDate, type DateGroup } from '$lib/utils/dateGroups';

	// Configure marked for better code highlighting
	marked.setOptions({
		breaks: true,
		gfm: true
	});

	// Subagent interface for enabled agents management
	interface Subagent {
		id: string;
		name: string;
		description: string;
		model?: string;
	}

	// Tool interfaces for tool configuration
	interface ToolInfo {
		name: string;
		category: string;
		description: string;
		mcp_server?: string;
	}

	interface ToolCategory {
		id: string;
		name: string;
		description: string;
		tools: ToolInfo[];
	}

	interface ToolsResponse {
		categories: ToolCategory[];
		all_tools: ToolInfo[];
	}

	// Subagents list for profile config
	let allSubagents: Subagent[] = [];

	// Available tools for profile config
	let availableTools: ToolsResponse = { categories: [], all_tools: [] };
	let toolsExpanded: Record<string, boolean> = {}; // Track expanded categories

	// Load subagents
	async function loadSubagents() {
		try {
			allSubagents = await api.get<Subagent[]>('/subagents');
		} catch (e) {
			console.error('Failed to load subagents:', e);
			allSubagents = [];
		}
	}

	// Load available tools
	async function loadTools() {
		try {
			availableTools = await api.get<ToolsResponse>('/tools');
		} catch (e) {
			console.error('Failed to load tools:', e);
			availableTools = { categories: [], all_tools: [] };
		}
	}

	// Per-tab state (we track input per tab)
	let tabInputs: Record<string, string> = {};
	let tabUploadedFiles: Record<string, FileUploadResponse[]> = {};

	let sidebarOpen = false;
	let sidebarTab: 'my-chats' | 'admin' = 'my-chats';

	// Icon Rail state
	type SidebarSection = 'none' | 'sessions' | 'projects' | 'profiles' | 'subagents';
	let activeSidebarSection: SidebarSection = 'none';
	let sidebarPinned = false;

	// Session sidebar state
	let sessionSearchQuery = '';
	let sessionSearchExpanded = false;
	let collapsedGroups: Set<string> = new Set(['yesterday', 'week', 'month', 'older']); // Only 'today' expanded by default

	// Search state
	let searchResults: SessionSearchResult[] = [];
	let isSearching = false;
	let searchDebounceTimer: ReturnType<typeof setTimeout> | null = null;

	// Debounced search function
	function debouncedSearch(query: string) {
		// Clear existing timer
		if (searchDebounceTimer) {
			clearTimeout(searchDebounceTimer);
		}

		// If query is empty, clear results immediately
		if (!query.trim()) {
			searchResults = [];
			isSearching = false;
			return;
		}

		isSearching = true;

		// Debounce the API call by 300ms
		searchDebounceTimer = setTimeout(async () => {
			try {
				const adminOnly = sidebarTab === 'my-chats';
				searchResults = await searchSessions(query, { adminOnly, limit: 20 });
			} catch (error) {
				console.error('Search failed:', error);
				searchResults = [];
			} finally {
				isSearching = false;
			}
		}, 300);
	}

	// Trigger search when query changes
	$: debouncedSearch(sessionSearchQuery);

	// Computed: filtered and grouped sessions (when not searching)
	$: filteredSessions = $sessions;
	$: groupedSessions = groupSessionsByDate(filteredSessions);

	// Admin sessions grouped (when not searching)
	$: filteredAdminSessions = $adminSessions;
	$: groupedAdminSessions = groupSessionsByDate(filteredAdminSessions);

	function toggleGroupCollapse(key: string) {
		if (collapsedGroups.has(key)) {
			collapsedGroups.delete(key);
		} else {
			collapsedGroups.add(key);
		}
		collapsedGroups = collapsedGroups; // Trigger reactivity
	}

	function clearSearch() {
		sessionSearchQuery = '';
		sessionSearchExpanded = false;
		searchResults = [];
		isSearching = false;
		if (searchDebounceTimer) {
			clearTimeout(searchDebounceTimer);
			searchDebounceTimer = null;
		}
	}

	function toggleSidebarSection(section: SidebarSection) {
		if (activeSidebarSection === section) {
			if (!sidebarPinned) {
				activeSidebarSection = 'none';
			}
		} else {
			activeSidebarSection = section;
		}
	}

	function closeSidebar() {
		if (!sidebarPinned) {
			activeSidebarSection = 'none';
		}
	}

	function toggleSidebarPin() {
		sidebarPinned = !sidebarPinned;
	}

	// Auto-scroll: Simple logic
	// - Always scroll to bottom UNLESS user scrolls up to read
	// - Re-enable auto-scroll when user scrolls back to bottom
	// Using a Svelte action for reliable DOM access

	function autoScroll(node: HTMLElement, tabId: string) {
		let userScrolledUp = false;

		function isNearBottom(): boolean {
			const threshold = 100;
			return node.scrollHeight - node.scrollTop - node.clientHeight < threshold;
		}

		function scrollToBottom() {
			node.scrollTop = node.scrollHeight;
		}

		function handleScroll() {
			userScrolledUp = !isNearBottom();
		}

		// Create a MutationObserver to watch for content changes
		const observer = new MutationObserver(() => {
			if (!userScrolledUp) {
				scrollToBottom();
			}
		});

		observer.observe(node, {
			childList: true,
			subtree: true,
			characterData: true
		});

		node.addEventListener('scroll', handleScroll);

		// Initial scroll to bottom
		scrollToBottom();

		return {
			update(newTabId: string) {
				// When tab changes, reset and scroll
				userScrolledUp = false;
				scrollToBottom();
			},
			destroy() {
				observer.disconnect();
				node.removeEventListener('scroll', handleScroll);
			}
		};
	}

	let showProfileModal = false;
	let showProjectModal = false;
	let showNewProfileForm = false;
	let showNewProjectForm = false;
	let editingProfile: any = null;
	let fileInput: HTMLInputElement;
	let isUploading = false;
	let textareas: Record<string, HTMLTextAreaElement> = {};

	// Voice recording state
	let isRecording = false;
	let isTranscribing = false;
	let mediaRecorder: MediaRecorder | null = null;
	let audioChunks: Blob[] = [];
	let recordingError = '';

	// Terminal modal state for /resume and other interactive commands
	let showTerminalModal = false;
	let terminalCommand = '/resume';
	let terminalSessionId = '';

	// Rewind modal state (V2 - direct JSONL manipulation)
	let showRewindModal = false;
	let rewindSessionId = '';

	// Spotlight search state (Cmd+K)
	let showSpotlight = false;

	// Subagent manager state
	let showSubagentManager = false;

	// Mobile tools menu state
	let showToolsMenu = false;

	// Override popup menu state
	let showModelPopup = false;
	let showModePopup = false;

	// Command autocomplete state
	let showCommandAutocomplete: Record<string, boolean> = {};
	let commandAutocompleteRefs: Record<string, CommandAutocomplete> = {};

	// File autocomplete state (@ mentions)
	let showFileAutocomplete: Record<string, boolean> = {};
	let fileAutocompleteRefs: Record<string, FileAutocomplete> = {};

	// Accordion states for profile form sections
	let expandedSections: Record<string, boolean> = {
		toolConfig: false,
		behavior: false,
		enabledAgents: false,
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
		allowed_tools: [] as string[],
		disallowed_tools: [] as string[],
		enabled_agents: [] as string[],
		include_partial_messages: true,
		continue_conversation: false,
		fork_session: false,
		system_prompt_type: 'preset',
		system_prompt_preset: 'claude_code',
		system_prompt_append: '',
		system_prompt_content: '',
		system_prompt_inject_env: false,
		// AI tools - individual toggles for each AI tool
		ai_tools_image_generation: false,
		setting_sources: [] as string[],
		cwd: '',
		add_dirs: '',
		user: '',
		max_buffer_size: null as number | null
	};

	// Tool selection mode: 'all' = all tools allowed, 'allow' = whitelist mode, 'disallow' = blacklist mode
	let toolSelectionMode: 'all' | 'allow' | 'disallow' = 'all';

	// Toggle agent selection
	function toggleAgent(agentId: string) {
		if (profileForm.enabled_agents.includes(agentId)) {
			profileForm.enabled_agents = profileForm.enabled_agents.filter(id => id !== agentId);
		} else {
			profileForm.enabled_agents = [...profileForm.enabled_agents, agentId];
		}
	}

	// Toggle tool selection
	function toggleTool(toolName: string) {
		if (toolSelectionMode === 'allow') {
			if (profileForm.allowed_tools.includes(toolName)) {
				profileForm.allowed_tools = profileForm.allowed_tools.filter(t => t !== toolName);
			} else {
				profileForm.allowed_tools = [...profileForm.allowed_tools, toolName];
			}
		} else if (toolSelectionMode === 'disallow') {
			if (profileForm.disallowed_tools.includes(toolName)) {
				profileForm.disallowed_tools = profileForm.disallowed_tools.filter(t => t !== toolName);
			} else {
				profileForm.disallowed_tools = [...profileForm.disallowed_tools, toolName];
			}
		}
	}

	// Check if a tool is selected based on current mode
	function isToolSelected(toolName: string): boolean {
		if (toolSelectionMode === 'all') return true;
		if (toolSelectionMode === 'allow') return profileForm.allowed_tools.includes(toolName);
		if (toolSelectionMode === 'disallow') return !profileForm.disallowed_tools.includes(toolName);
		return true;
	}

	// Toggle all tools in a category
	function toggleCategory(category: ToolCategory) {
		const categoryToolNames = category.tools.map(t => t.name);

		if (toolSelectionMode === 'allow') {
			const allSelected = categoryToolNames.every(name => profileForm.allowed_tools.includes(name));
			if (allSelected) {
				// Deselect all in category
				profileForm.allowed_tools = profileForm.allowed_tools.filter(t => !categoryToolNames.includes(t));
			} else {
				// Select all in category
				const newTools = categoryToolNames.filter(name => !profileForm.allowed_tools.includes(name));
				profileForm.allowed_tools = [...profileForm.allowed_tools, ...newTools];
			}
		} else if (toolSelectionMode === 'disallow') {
			const allDisabled = categoryToolNames.every(name => profileForm.disallowed_tools.includes(name));
			if (allDisabled) {
				// Enable all in category (remove from disallowed)
				profileForm.disallowed_tools = profileForm.disallowed_tools.filter(t => !categoryToolNames.includes(t));
			} else {
				// Disable all in category (add to disallowed)
				const newTools = categoryToolNames.filter(name => !profileForm.disallowed_tools.includes(name));
				profileForm.disallowed_tools = [...profileForm.disallowed_tools, ...newTools];
			}
		}
	}

	// Check if all tools in a category are selected
	function isCategoryFullySelected(category: ToolCategory): boolean {
		const categoryToolNames = category.tools.map(t => t.name);
		if (toolSelectionMode === 'all') return true;
		if (toolSelectionMode === 'allow') {
			return categoryToolNames.length > 0 && categoryToolNames.every(name => profileForm.allowed_tools.includes(name));
		}
		if (toolSelectionMode === 'disallow') {
			return categoryToolNames.every(name => profileForm.disallowed_tools.includes(name));
		}
		return true;
	}

	// Check if some (but not all) tools in a category are selected
	function isCategoryPartiallySelected(category: ToolCategory): boolean {
		const categoryToolNames = category.tools.map(t => t.name);
		if (toolSelectionMode === 'all') return false;
		if (toolSelectionMode === 'allow') {
			const selectedCount = categoryToolNames.filter(name => profileForm.allowed_tools.includes(name)).length;
			return selectedCount > 0 && selectedCount < categoryToolNames.length;
		}
		if (toolSelectionMode === 'disallow') {
			const disabledCount = categoryToolNames.filter(name => profileForm.disallowed_tools.includes(name)).length;
			return disabledCount > 0 && disabledCount < categoryToolNames.length;
		}
		return false;
	}

	// Check if no tools in a category are selected
	function isCategoryNoneSelected(category: ToolCategory): boolean {
		const categoryToolNames = category.tools.map(t => t.name);
		if (toolSelectionMode === 'all') return false;
		if (toolSelectionMode === 'allow') {
			return categoryToolNames.every(name => !profileForm.allowed_tools.includes(name));
		}
		if (toolSelectionMode === 'disallow') {
			return categoryToolNames.every(name => !profileForm.disallowed_tools.includes(name));
		}
		return false;
	}

	// Toggle tool category expansion
	function toggleToolCategoryExpansion(categoryId: string) {
		toolsExpanded[categoryId] = !toolsExpanded[categoryId];
		toolsExpanded = toolsExpanded; // Trigger reactivity
	}

	function getModelDisplay(model?: string): string {
		switch (model) {
			case 'haiku': return 'Haiku';
			case 'sonnet': return 'Sonnet';
			case 'opus': return 'Opus';
			default: return 'Inherit';
		}
	}

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
		// Load admin-specific data if user is admin
		if ($isAdmin) {
			tabs.loadApiUsers();
			tabs.loadAdminSessions();
		}
	} else if (!$isAuthenticated && wasAuthenticated) {
		wasAuthenticated = false;
	}

	onMount(() => {
		if ($isAuthenticated) {
			wasAuthenticated = true;
			tabs.init();
			const promises = [
				tabs.loadProfiles(),
				tabs.loadSessions(),
				tabs.loadProjects(),
				loadSubagents(),
				loadTools()
			];
			// Load admin-specific data if user is admin
			if ($isAdmin) {
				promises.push(tabs.loadApiUsers());
				promises.push(tabs.loadAdminSessions());
			}
			Promise.all(promises);
		}

		// Handle page restored from bfcache (back/forward navigation)
		// This ensures sessions list is fresh after browser back/forward
		const handlePageShow = (event: PageTransitionEvent) => {
			if (event.persisted && $isAuthenticated) {
				console.log('[Page] Restored from bfcache, refreshing sessions');
				tabs.loadSessions();
				if ($isAdmin) {
					tabs.loadAdminSessions();
				}
			}
		};

		window.addEventListener('pageshow', handlePageShow);

		// Spotlight search keyboard shortcut (Cmd+K / Ctrl+K)
		const handleSpotlightKeydown = (event: KeyboardEvent) => {
			if ((event.metaKey || event.ctrlKey) && event.key === 'k') {
				event.preventDefault();
				showSpotlight = !showSpotlight;
			}
		};

		window.addEventListener('keydown', handleSpotlightKeydown);

		return () => {
			window.removeEventListener('pageshow', handlePageShow);
			window.removeEventListener('keydown', handleSpotlightKeydown);
		};
	});

	onDestroy(() => {
		tabs.destroy();
	});

	// Initialize tabInputs for all tabs to ensure proper binding
	$: {
		for (const tab of $allTabs) {
			if (tabInputs[tab.id] === undefined) {
				tabInputs[tab.id] = '';
			}
		}
	}

	// Auto-set profile and project for API users based on their API key restrictions
	$: if ($apiUser && $activeTabId && $activeTab) {
		// If API user has a locked profile and current tab doesn't have it set
		if ($apiUser.profile_id && $activeTab.profile !== $apiUser.profile_id) {
			tabs.setTabProfile($activeTabId, $apiUser.profile_id);
		}
		// If API user has a locked project and current tab doesn't have it set
		if ($apiUser.project_id && $activeTab.project !== $apiUser.project_id) {
			tabs.setTabProject($activeTabId, $apiUser.project_id);
		}
	}

	// Handle permission response from PermissionQueue component
	function handlePermissionRespond(tabId: string, event: CustomEvent<{
		request_id: string;
		decision: 'allow' | 'deny';
		remember?: 'none' | 'session' | 'profile';
		pattern?: string;
	}>) {
		const { request_id, decision, remember, pattern } = event.detail;
		tabs.sendPermissionResponse(tabId, request_id, decision, remember, pattern);
	}

	// Handle user question response from UserQuestion component
	function handleUserQuestionRespond(tabId: string, event: CustomEvent<{
		request_id: string;
		tool_use_id: string;
		answers: Record<string, string | string[]>;
	}>) {
		const { request_id, tool_use_id, answers } = event.detail;
		tabs.sendUserQuestionResponse(tabId, request_id, tool_use_id, answers);
	}

	async function handleSubmit(tabId: string) {
		const prompt = tabInputs[tabId] || '';
		// Allow sending while streaming (streaming input) - tabs.sendMessage will queue the message
		if (!prompt.trim() || !$activeTab || isUploading) return;

		// API users use their API key restrictions - skip profile/project validation if they have restrictions
		const isApiUserWithRestrictions = $apiUser && ($apiUser.profile_id || $apiUser.project_id);

		// Require profile and project to be selected before sending messages (unless API user with restrictions)
		if (!$activeTab.profile && !($apiUser?.profile_id)) {
			tabs.setTabError(tabId, 'Please select a profile before starting a chat');
			return;
		}
		if (!$activeTab.project && !($apiUser?.project_id)) {
			tabs.setTabError(tabId, 'Please select a project before starting a chat');
			return;
		}

		// Append file references to the end of the message
		const files = tabUploadedFiles[tabId] || [];
		let finalPrompt = prompt;
		if (files.length > 0) {
			const fileRefs = files.map(f => `@${f.path}`).join(' ');
			finalPrompt = prompt.trim() + '\n\n' + fileRefs;
		}

		tabInputs[tabId] = '';
		tabInputs = tabInputs; // Trigger Svelte reactivity
		tabUploadedFiles[tabId] = [];
		tabs.sendMessage(tabId, finalPrompt);
	}

	function handleKeyDown(e: KeyboardEvent, tabId: string) {
		// Let the command autocomplete handle the event first if visible
		const commandAutocomplete = commandAutocompleteRefs[tabId];
		if (commandAutocomplete && showCommandAutocomplete[tabId]) {
			const handled = commandAutocomplete.handleKeyDown(e);
			if (handled) {
				return; // Command autocomplete handled the event
			}
		}

		// Let file autocomplete handle the event if visible
		const fileAutocomplete = fileAutocompleteRefs[tabId];
		if (fileAutocomplete && showFileAutocomplete[tabId]) {
			const handled = fileAutocomplete.handleKeyDown(e);
			if (handled) {
				return; // File autocomplete handled the event
			}
		}

		// Normal textarea behavior - on desktop, Enter sends; on mobile, let Enter create newlines
		// Mobile detection: touch device with narrow screen
		const isMobile = window.matchMedia('(max-width: 640px)').matches && ('ontouchstart' in window);

		if (e.key === 'Enter' && !e.shiftKey && !isMobile) {
			e.preventDefault();
			handleSubmit(tabId);
		}
	}

	// Check if input contains an active @ mention (at start or after whitespace)
	// Returns true if there's an @ that could be a file reference (supports paths with spaces)
	function hasActiveAtMention(input: string): boolean {
		// Search backwards to find the last @ that's at start or preceded by whitespace
		for (let i = input.length - 1; i >= 0; i--) {
			if (input[i] === '@') {
				if (i === 0 || /\s/.test(input[i - 1])) {
					return true;
				}
			}
		}
		return false;
	}

	// Handle input changes for command and file autocomplete
	function handleInputChange(tabId: string) {
		const input = tabInputs[tabId] || '';

		// Show command autocomplete when input starts with /
		showCommandAutocomplete[tabId] = input.startsWith('/') && input.length > 0;
		showCommandAutocomplete = showCommandAutocomplete;

		// Show file autocomplete when there's an active @ mention
		showFileAutocomplete[tabId] = hasActiveAtMention(input) && !!$activeTab?.project;
		showFileAutocomplete = showFileAutocomplete;
	}

	// Handle command selection from autocomplete
	async function handleCommandSelect(tabId: string, command: Command) {
		showCommandAutocomplete[tabId] = false;
		showCommandAutocomplete = showCommandAutocomplete;

		if (command.type === 'interactive') {
			// Open terminal modal for interactive commands
			openTerminalModal(tabId, `/${command.name}`);
		} else {
			// For custom commands, fill in the command
			tabInputs[tabId] = `/${command.name} `;
			tabInputs = tabInputs;
			// Focus the textarea
			setTimeout(() => {
				const textarea = textareas[tabId];
				if (textarea) {
					textarea.focus();
					textarea.setSelectionRange(textarea.value.length, textarea.value.length);
				}
			}, 0);
		}
	}

	// Find the last @ position that's at start or preceded by whitespace
	function findLastAtIndex(input: string): number {
		for (let i = input.length - 1; i >= 0; i--) {
			if (input[i] === '@') {
				if (i === 0 || /\s/.test(input[i - 1])) {
					return i;
				}
			}
		}
		return -1;
	}

	// Handle file selection from @ autocomplete
	function handleFileSelect(tabId: string, file: FileItem) {
		const input = tabInputs[tabId] || '';

		// Find the last @ mention to replace
		const atStartIndex = findLastAtIndex(input);
		if (atStartIndex === -1) {
			showFileAutocomplete[tabId] = false;
			showFileAutocomplete = showFileAutocomplete;
			return;
		}

		// For directories, replace with path and keep autocomplete open
		if (file.type === 'directory') {
			const newInput = input.substring(0, atStartIndex) + '@' + file.path;
			tabInputs[tabId] = newInput;
			tabInputs = tabInputs;
			// Keep autocomplete open for directory navigation
			setTimeout(() => {
				const textarea = textareas[tabId];
				if (textarea) {
					textarea.focus();
					textarea.setSelectionRange(textarea.value.length, textarea.value.length);
				}
			}, 0);
			return;
		}

		// For files, add to the uploaded files list as a chip (no text in input)
		const fileRef: FileUploadResponse = {
			filename: file.name,
			path: file.path,
			full_path: file.path,
			size: file.size || 0
		};

		// Add to tracked files
		if (!tabUploadedFiles[tabId]) tabUploadedFiles[tabId] = [];
		tabUploadedFiles[tabId] = [...tabUploadedFiles[tabId], fileRef];

		// Remove the @query from input (file reference shown as chip only)
		const newInput = input.substring(0, atStartIndex).trimEnd();
		tabInputs[tabId] = newInput;
		tabInputs = tabInputs;

		// Close autocomplete and focus
		showFileAutocomplete[tabId] = false;
		showFileAutocomplete = showFileAutocomplete;

		setTimeout(() => {
			const textarea = textareas[tabId];
			if (textarea) {
				textarea.focus();
				textarea.setSelectionRange(textarea.value.length, textarea.value.length);
			}
		}, 0);
	}

	// Open the terminal modal for interactive commands (like /resume)
	function openTerminalModal(tabId: string, command: string = '/resume') {
		const tab = $allTabs.find(t => t.id === tabId);
		if (!tab?.sessionId) {
			alert('Please start a conversation first before using this command.');
			return;
		}
		terminalSessionId = tab.sessionId;
		terminalCommand = command;
		showTerminalModal = true;
	}

	// Open the rewind modal (V2 - direct JSONL manipulation)
	function openRewindModal(tabId: string) {
		const tab = $allTabs.find(t => t.id === tabId);
		if (!tab?.sessionId) {
			alert('Please start a conversation first before using rewind.');
			return;
		}
		rewindSessionId = tab.sessionId;
		showRewindModal = true;
	}

	// Handle rewind completion from RewindModal V2
	async function handleRewindCompleteV2(success: boolean, messagesRemoved: number, messageContent?: string) {
		console.log('Rewind V2 complete:', { success, messagesRemoved, messageContent });

		if (success && messagesRemoved > 0 && $activeTabId && rewindSessionId) {
			// Reload the session to reflect changes
			await tabs.loadSessionInTab($activeTabId, rewindSessionId);

			// Populate the input field with the rewound message so user can edit and resend
			if (messageContent) {
				tabInputs[$activeTabId] = messageContent;
				tabInputs = tabInputs; // Trigger Svelte reactivity

				// Focus the textarea after a short delay
				setTimeout(() => {
					const textarea = textareas[$activeTabId];
					if (textarea) {
						textarea.focus();
						// Move cursor to end
						textarea.selectionStart = textarea.selectionEnd = textarea.value.length;
					}
				}, 100);
			}
		}

		rewindSessionId = '';
	}

	// Close the rewind modal
	function closeRewindModal() {
		showRewindModal = false;
		rewindSessionId = '';
	}

	// Handle rewind completion from TerminalModal (legacy - for /resume)
	async function handleRewindComplete(checkpointMessage: string | null, selectedOption: number | null) {
		console.log('Rewind complete (legacy):', { checkpointMessage, selectedOption });

		// Sync our chat if conversation was restored (options 1 or 2)
		if (terminalSessionId && checkpointMessage && (selectedOption === 1 || selectedOption === 2)) {
			try {
				const result = await syncAfterRewind(terminalSessionId, checkpointMessage, selectedOption);
				console.log('Chat sync result:', result);

				// Reload the session to reflect changes
				if ($activeTabId && result.success && result.deleted_count > 0) {
					await tabs.loadSessionInTab($activeTabId, terminalSessionId);
				}
			} catch (error) {
				console.error('Failed to sync chat after rewind:', error);
			}
		}
	}

	// Close the terminal modal
	function closeTerminalModal() {
		showTerminalModal = false;
		terminalSessionId = '';
	}

	// Spotlight search handlers
	function handleSpotlightSelectSession(session: { id: string }) {
		if ($activeTabId) {
			tabs.loadSessionInTab($activeTabId, session.id);
		}
		showSpotlight = false;
	}

	function handleSpotlightSelectCommand(command: Command) {
		if (!$activeTabId) return;

		if (command.type === 'interactive') {
			openTerminalModal($activeTabId, `/${command.name}`);
		} else {
			// Fill in the command in the input
			tabInputs[$activeTabId] = `/${command.name} `;
			tabInputs = tabInputs;
			// Focus the textarea
			setTimeout(() => {
				const textarea = textareas[$activeTabId!];
				if (textarea) {
					textarea.focus();
					textarea.setSelectionRange(textarea.value.length, textarea.value.length);
				}
			}, 0);
		}
		showSpotlight = false;
	}

	function handleSpotlightNewChat() {
		tabs.addTab();
		showSpotlight = false;
	}

	function handleSpotlightOpenSettings() {
		showProfileModal = true;
		showSpotlight = false;
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
		// Process base64 images with download capability
		// Match data URLs in image tags or standalone base64 image data
		const base64ImagePattern = /!\[([^\]]*)\]\((data:image\/[^;]+;base64,[^)]+)\)/g;

		// Replace base64 image markdown with custom HTML that includes download button
		let processedContent = content.replace(base64ImagePattern, (match, alt, dataUrl) => {
			const downloadId = `img-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
			return `<div class="generated-image-container my-4">
				<img src="${dataUrl}" alt="${alt || 'Generated image'}" class="max-w-full rounded-lg shadow-md border border-border" />
				<div class="flex gap-2 mt-2">
					<button onclick="(function(){
						const link = document.createElement('a');
						link.href = '${dataUrl}';
						link.download = '${alt || 'generated-image'}.png';
						link.click();
					})()" class="text-xs px-3 py-1 bg-primary text-primary-foreground rounded hover:opacity-90 flex items-center gap-1">
						<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
						</svg>
						Download
					</button>
				</div>
			</div>`;
		});

		// Also detect JSON tool results with image_base64 field
		const jsonImagePattern = /"image_base64"\s*:\s*"([A-Za-z0-9+/=]+)"/g;
		processedContent = processedContent.replace(jsonImagePattern, (match, base64Data) => {
			// Only replace if it looks like substantial base64 data (not a short string)
			if (base64Data.length > 100) {
				return `<div class="generated-image-container my-4">
					<img src="data:image/png;base64,${base64Data}" alt="Generated image" class="max-w-full rounded-lg shadow-md border border-border" />
					<div class="flex gap-2 mt-2">
						<button onclick="(function(){
							const link = document.createElement('a');
							link.href = 'data:image/png;base64,${base64Data}';
							link.download = 'generated-image.png';
							link.click();
						})()" class="text-xs px-3 py-1 bg-primary text-primary-foreground rounded hover:opacity-90 flex items-center gap-1">
							<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
							</svg>
							Download
						</button>
					</div>
				</div>`;
			}
			return match;
		});

		// Render generated-images markdown as clean image display (buttons are in message header)
		const generatedImagePattern = /!\[([^\]]*)\]\((\/api\/generated-images\/[^)]+)\)/g;
		processedContent = processedContent.replace(generatedImagePattern, (match, alt, imageUrl) => {
			return `<div class="generated-image-container my-4">
				<img src="${imageUrl}" alt="${alt || 'Generated image'}" class="max-w-full max-h-[500px] rounded-lg shadow-lg border border-border" />
			</div>`;
		});

		return marked(processedContent, { breaks: true }) as string;
	}

	// Render tool result content, detecting and displaying images
	function renderToolResult(content: string): string {
		try {
			// Try to parse as JSON to detect image data
			const data = JSON.parse(content);

			// Handle URL-based image results (new format - saves to disk)
			if (data.success && data.image_url) {
				const imageUrl = data.image_url;
				const filename = data.filename || 'generated-image.png';
				return `<div class="generated-image-result">
					<div class="mb-2 text-xs text-green-500">✓ Image generated successfully</div>
					<img src="${imageUrl}" alt="Generated image" class="max-w-full max-h-96 rounded-lg shadow-md border border-border" />
					<div class="flex gap-2 mt-3">
						<a href="${imageUrl}" download="${filename}" class="text-xs px-3 py-1.5 bg-primary text-primary-foreground rounded-md hover:opacity-90 flex items-center gap-1.5 font-medium no-underline">
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
							</svg>
							Download Image
						</a>
						<button onclick="(function(){
							navigator.clipboard.writeText(window.location.origin + '${imageUrl}');
							this.textContent = 'Copied!';
							setTimeout(() => this.textContent = 'Copy URL', 2000);
						}).call(this)" class="text-xs px-3 py-1.5 bg-muted text-foreground rounded-md hover:bg-accent flex items-center gap-1.5">
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
							</svg>
							Copy URL
						</button>
					</div>
				</div>`;
			}

			// Handle base64 image results (legacy format)
			if (data.success && data.image_base64 && data.mime_type) {
				// This is an image generation result!
				const mimeType = data.mime_type || 'image/png';
				const dataUrl = `data:${mimeType};base64,${data.image_base64}`;
				return `<div class="generated-image-result">
					<div class="mb-2 text-xs text-green-500">✓ Image generated successfully</div>
					<img src="${dataUrl}" alt="Generated image" class="max-w-full max-h-96 rounded-lg shadow-md border border-border" />
					<div class="flex gap-2 mt-3">
						<button onclick="(function(){
							const link = document.createElement('a');
							link.href = '${dataUrl}';
							link.download = 'generated-image.png';
							link.click();
						})()" class="text-xs px-3 py-1.5 bg-primary text-primary-foreground rounded-md hover:opacity-90 flex items-center gap-1.5 font-medium">
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
							</svg>
							Download Image
						</button>
						<button onclick="(function(){
							navigator.clipboard.writeText('${dataUrl}');
							this.textContent = 'Copied!';
							setTimeout(() => this.textContent = 'Copy Data URL', 2000);
						}).call(this)" class="text-xs px-3 py-1.5 bg-muted text-foreground rounded-md hover:bg-accent flex items-center gap-1.5">
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
							</svg>
							Copy Data URL
						</button>
					</div>
				</div>`;
			}
		} catch {
			// Not JSON, continue with default rendering
		}
		// Return empty to use default <pre> rendering
		return '';
	}

	// Check if tool result contains an image
	function toolResultHasImage(content: string): boolean {
		try {
			const data = JSON.parse(content);
			// Check for both URL-based (new) and base64 (legacy) formats
			return data.success && (data.image_url || (data.image_base64 && data.mime_type));
		} catch {
			return false;
		}
	}

	// Render standalone image display (for main chat, outside tool groups)
	function renderStandaloneImage(content: string): string {
		try {
			const data = JSON.parse(content);

			// Handle URL-based image results (new format)
			if (data.success && data.image_url) {
				const imageUrl = data.image_url;
				const filename = data.filename || 'generated-image.png';
				return `<div class="generated-image-standalone mt-3">
					<img src="${imageUrl}" alt="Generated image" class="max-w-full max-h-[500px] rounded-lg shadow-lg border border-border" />
					<div class="flex items-center gap-2 mt-2">
						<a href="${imageUrl}" download="${filename}" class="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-muted-foreground hover:bg-muted/50 hover:text-foreground transition-colors no-underline" title="Download image">
							<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
							</svg>
							<span>Download</span>
						</a>
						<button onclick="(function(btn){
							navigator.clipboard.writeText(window.location.origin + '${imageUrl}');
							var iconSpan = btn.querySelector('.copy-icon');
							var checkSpan = btn.querySelector('.check-icon');
							var textSpan = btn.querySelector('.btn-text');
							iconSpan.style.display = 'none';
							checkSpan.style.display = 'block';
							textSpan.textContent = 'Copied';
							textSpan.classList.add('text-green-500');
							setTimeout(function() {
								iconSpan.style.display = 'block';
								checkSpan.style.display = 'none';
								textSpan.textContent = 'Copy URL';
								textSpan.classList.remove('text-green-500');
							}, 2000);
						})(this)" class="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-muted-foreground hover:bg-muted/50 hover:text-foreground transition-colors" title="Copy image URL">
							<svg class="w-3.5 h-3.5 copy-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
							</svg>
							<svg class="w-3.5 h-3.5 check-icon text-green-500" style="display:none" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
							</svg>
							<span class="btn-text">Copy URL</span>
						</button>
					</div>
				</div>`;
			}

			// Handle base64 image results (legacy format)
			if (data.success && data.image_base64 && data.mime_type) {
				const mimeType = data.mime_type || 'image/png';
				const dataUrl = `data:${mimeType};base64,${data.image_base64}`;
				return `<div class="generated-image-standalone mt-3">
					<img src="${dataUrl}" alt="Generated image" class="max-w-full max-h-[500px] rounded-lg shadow-lg border border-border" />
					<div class="flex items-center gap-2 mt-2">
						<button onclick="(function(){
							const link = document.createElement('a');
							link.href = '${dataUrl}';
							link.download = 'generated-image.png';
							link.click();
						})()" class="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-muted-foreground hover:bg-muted/50 hover:text-foreground transition-colors" title="Download image">
							<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
							</svg>
							<span>Download</span>
						</button>
						<button onclick="(function(btn){
							navigator.clipboard.writeText('${dataUrl}');
							var iconSpan = btn.querySelector('.copy-icon');
							var checkSpan = btn.querySelector('.check-icon');
							var textSpan = btn.querySelector('.btn-text');
							iconSpan.style.display = 'none';
							checkSpan.style.display = 'block';
							textSpan.textContent = 'Copied';
							textSpan.classList.add('text-green-500');
							setTimeout(function() {
								iconSpan.style.display = 'block';
								checkSpan.style.display = 'none';
								textSpan.textContent = 'Copy URL';
								textSpan.classList.remove('text-green-500');
							}, 2000);
						})(this)" class="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-muted-foreground hover:bg-muted/50 hover:text-foreground transition-colors" title="Copy image URL">
							<svg class="w-3.5 h-3.5 copy-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
							</svg>
							<svg class="w-3.5 h-3.5 check-icon text-green-500" style="display:none" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
							</svg>
							<span class="btn-text">Copy URL</span>
						</button>
					</div>
				</div>`;
			}
		} catch {
			// Not valid JSON
		}
		return '';
	}

	// Copy text to clipboard with feedback
	let copiedMessageId: string | null = null;
	let copiedUrlMessageId: string | null = null;

	async function copyToClipboard(text: string, messageId: string) {
		try {
			await navigator.clipboard.writeText(text);
			copiedMessageId = messageId;
			setTimeout(() => {
				copiedMessageId = null;
			}, 2000);
		} catch (e) {
			console.error('Failed to copy to clipboard:', e);
		}
	}

	async function copyUrlToClipboard(url: string, messageId: string) {
		try {
			const fullUrl = window.location.origin + url;
			await navigator.clipboard.writeText(fullUrl);
			copiedUrlMessageId = messageId;
			setTimeout(() => {
				copiedUrlMessageId = null;
			}, 2000);
		} catch (e) {
			console.error('Failed to copy URL to clipboard:', e);
		}
	}

	// Extract generated image URL from message content
	function extractGeneratedImageUrl(content: string): string | null {
		const match = content.match(/!\[[^\]]*\]\((\/api\/generated-images\/[^)]+)\)/);
		return match ? match[1] : null;
	}

	// Get filename from generated image URL
	function getFilenameFromUrl(url: string): string {
		const pathMatch = url.match(/path=([^&]+)/);
		if (pathMatch) {
			const path = decodeURIComponent(pathMatch[1]);
			return path.split('/').pop() || 'generated-image.png';
		}
		return url.split('/').pop() || 'generated-image.png';
	}

	function formatTime(date?: Date): string {
		const d = date || new Date();
		return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
	}

	function formatDate(dateStr: string): string {
		const date = new Date(dateStr);
		const now = new Date();
		const diff = now.getTime() - date.getTime();
		const minutes = Math.floor(diff / 60000);
		const hours = Math.floor(diff / 3600000);
		const days = Math.floor(diff / 86400000);

		if (minutes < 1) return 'Just now';
		if (minutes < 5) return '5m ago';
		if (minutes < 10) return '10m ago';
		if (minutes < 30) return '30m ago';
		if (minutes < 60) return '50m ago';
		if (hours < 24) return `${hours}h ago`;
		if (days === 1) return 'Yesterday';
		if (days < 7) return `${days}d ago`;
		return date.toLocaleDateString();
	}

	function formatSessionCost(cost: number | undefined | null): string {
		if (cost === undefined || cost === null || cost === 0) return '';
		return `$${cost.toFixed(4)}`;
	}

	function formatTokenCount(count: number): string {
		if (count >= 1000000) {
			return `${(count / 1000000).toFixed(1)}M`;
		}
		if (count >= 1000) {
			return `${(count / 1000).toFixed(1)}k`;
		}
		return count.toString();
	}

	function truncateTitle(title: string | null, maxLength: number = 35): string {
		if (!title) return 'New Chat';
		return title.length > maxLength ? title.substring(0, maxLength) + '...' : title;
	}

	// Open session - creates new tab or switches to existing tab with that session
	function openSession(sessionId: string) {
		// Prevent clicking while sessions are loading (race condition with bfcache)
		if ($sessionsLoading) {
			console.log('[Page] Ignoring session click while loading');
			return;
		}
		tabs.openSession(sessionId);
		sidebarOpen = false;
		// Auto-scroll is handled by the autoScroll action on the messages container
	}

	async function deleteSession(e: Event | null, sessionId: string) {
		e?.stopPropagation();
		if (confirm('Delete this session?')) {
			await tabs.deleteSession(sessionId);
		}
	}

	async function handleBatchDelete(isAdmin: boolean = false) {
		const count = isAdmin ? $selectedAdminSessionIds.size : $selectedSessionIds.size;
		if (count === 0) return;

		if (confirm(`Delete ${count} selected session${count > 1 ? 's' : ''}?`)) {
			await tabs.deleteSelectedSessions(isAdmin);
		}
	}

	function handleToggleSelection(e: Event, sessionId: string, isAdmin: boolean = false) {
		e.stopPropagation();
		tabs.toggleSessionSelection(sessionId, isAdmin);
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
			allowed_tools: [],
			disallowed_tools: [],
			enabled_agents: [],
			include_partial_messages: true,
			continue_conversation: false,
			fork_session: false,
			system_prompt_type: 'preset',
			system_prompt_preset: 'claude_code',
			system_prompt_append: '',
			system_prompt_content: '',
			system_prompt_inject_env: false,
			ai_tools_image_generation: false,
			setting_sources: [],
			cwd: '',
			add_dirs: '',
			user: '',
			max_buffer_size: null
		};
		toolSelectionMode = 'all';
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
		const aiTools = config.ai_tools || {};

		const allowedTools = config.allowed_tools || [];
		const disallowedTools = config.disallowed_tools || [];

		// Determine tool selection mode based on what's configured
		if (allowedTools.length > 0) {
			toolSelectionMode = 'allow';
		} else if (disallowedTools.length > 0) {
			toolSelectionMode = 'disallow';
		} else {
			toolSelectionMode = 'all';
		}

		profileForm = {
			id: profile.id,
			name: profile.name,
			description: profile.description || '',
			model: config.model || 'sonnet',
			permission_mode: config.permission_mode || 'default',
			max_turns: config.max_turns || null,
			allowed_tools: allowedTools,
			disallowed_tools: disallowedTools,
			enabled_agents: config.enabled_agents || [],
			include_partial_messages: config.include_partial_messages !== false,
			continue_conversation: config.continue_conversation || false,
			fork_session: config.fork_session || false,
			system_prompt_type: sp.type || 'preset',
			system_prompt_preset: sp.preset || 'claude_code',
			system_prompt_append: sp.append || '',
			system_prompt_content: sp.content || '',
			system_prompt_inject_env: sp.inject_env_details || false,
			// AI tools - load from config.ai_tools, fallback to legacy sp.enable_ai_tools
			ai_tools_image_generation: aiTools.image_generation || sp.enable_ai_tools || false,
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

		// Handle tool configuration based on selection mode
		if (toolSelectionMode === 'allow' && profileForm.allowed_tools.length > 0) {
			config.allowed_tools = profileForm.allowed_tools;
		}
		if (toolSelectionMode === 'disallow' && profileForm.disallowed_tools.length > 0) {
			config.disallowed_tools = profileForm.disallowed_tools;
		}
		if (profileForm.enabled_agents.length > 0) {
			config.enabled_agents = profileForm.enabled_agents;
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

		// AI tools configuration - individual toggles for each AI tool
		if (profileForm.ai_tools_image_generation) {
			config.ai_tools = {
				image_generation: profileForm.ai_tools_image_generation
			};
		}

		if (profileForm.system_prompt_type === 'preset') {
			config.system_prompt = {
				type: 'preset',
				preset: 'claude_code'  // Always use claude_code preset
			};
			if (profileForm.system_prompt_append.trim()) {
				config.system_prompt.append = profileForm.system_prompt_append;
			}
		} else {
			// Custom system prompt - allow blank content
			config.system_prompt = {
				type: 'custom',
				content: profileForm.system_prompt_content,
				inject_env_details: profileForm.system_prompt_inject_env
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

	// Profile import/export
	let profileImportInput: HTMLInputElement;
	let profileImporting = false;

	async function exportProfile(profileId: string) {
		try {
			const data = await api.get<any>(`/export-import/profiles/${profileId}`);
			const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `profile-${profileId}-${new Date().toISOString().split('T')[0]}.json`;
			document.body.appendChild(a);
			a.click();
			document.body.removeChild(a);
			URL.revokeObjectURL(url);
		} catch (e: any) {
			alert('Export failed: ' + (e.detail || 'Unknown error'));
		}
	}

	function triggerProfileImport() {
		profileImportInput?.click();
	}

	async function handleProfileImport(event: Event) {
		const input = event.target as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;
		input.value = '';

		profileImporting = true;
		try {
			const content = await file.text();
			const data = JSON.parse(content);

			if (!data.profiles || data.profiles.length === 0) {
				alert('No profiles found in file');
				profileImporting = false;
				return;
			}

			const result = await api.post<any>('/export-import/import/json?overwrite_existing=false', data);

			if (result.profiles_imported > 0) {
				alert(`Imported ${result.profiles_imported} profile(s)`);
				await tabs.loadProfiles();
			} else if (result.profiles_skipped > 0) {
				alert(`Profile already exists. Use a different ID or delete the existing one first.`);
			} else if (result.errors?.length > 0) {
				alert('Import failed: ' + result.errors.join(', '));
			}
		} catch (e: any) {
			alert('Import failed: ' + (e.detail || e.message || 'Invalid JSON file'));
		}
		profileImporting = false;
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

	async function handleFileUpload(event: Event) {
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
				// File references are shown as chips only, @FilePath appended when message is sent
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
		if (!files[index]) return;
		// File references are shown as chips only, just remove from the array
		tabUploadedFiles[tabId] = files.filter((_, i) => i !== index);
	}

	// Voice recording functions
	async function startRecording() {
		recordingError = '';
		try {
			const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
			mediaRecorder = new MediaRecorder(stream, {
				mimeType: MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : 'audio/mp4'
			});
			audioChunks = [];

			mediaRecorder.ondataavailable = (event) => {
				if (event.data.size > 0) {
					audioChunks.push(event.data);
				}
			};

			mediaRecorder.onstop = async () => {
				// Stop all tracks to release the microphone
				stream.getTracks().forEach(track => track.stop());

				if (audioChunks.length === 0) {
					recordingError = 'No audio recorded';
					return;
				}

				const audioBlob = new Blob(audioChunks, { type: mediaRecorder?.mimeType || 'audio/webm' });
				await transcribeAudio(audioBlob);
			};

			mediaRecorder.start();
			isRecording = true;
		} catch (error: any) {
			console.error('Failed to start recording:', error);
			if (error.name === 'NotAllowedError') {
				recordingError = 'Microphone access denied. Please allow microphone access in your browser settings.';
			} else if (error.name === 'NotFoundError') {
				recordingError = 'No microphone found. Please connect a microphone and try again.';
			} else {
				recordingError = `Failed to start recording: ${error.message || 'Unknown error'}`;
			}
		}
	}

	function stopRecording() {
		if (mediaRecorder && mediaRecorder.state !== 'inactive') {
			mediaRecorder.stop();
		}
		isRecording = false;
	}

	async function transcribeAudio(audioBlob: Blob) {
		if (!$activeTabId) return;

		isTranscribing = true;
		recordingError = '';

		try {
			const formData = new FormData();
			formData.append('file', audioBlob, 'recording.webm');

			const response = await fetch('/api/v1/settings/transcribe', {
				method: 'POST',
				credentials: 'include',
				body: formData
			});

			if (!response.ok) {
				const error = await response.json().catch(() => ({ detail: 'Transcription failed' }));
				throw new Error(error.detail || 'Transcription failed');
			}

			const result = await response.json();
			const transcribedText = result.text?.trim();

			if (transcribedText) {
				// Append to current input
				const tabId = $activeTabId;
				const currentInput = tabInputs[tabId] || '';
				tabInputs[tabId] = currentInput ? `${currentInput} ${transcribedText}` : transcribedText;

				// Focus the textarea and trigger resize
				await tick();
				const textarea = textareas[tabId];
				if (textarea) {
					textarea.focus();
					// Trigger auto-resize by resetting height and setting to scrollHeight
					textarea.style.height = 'auto';
					textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
				}
			}
		} catch (error: any) {
			console.error('Transcription failed:', error);
			recordingError = error.message || 'Failed to transcribe audio';
		} finally {
			isTranscribing = false;
		}
	}

	function toggleRecording() {
		if (isRecording) {
			stopRecording();
		} else {
			startRecording();
		}
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
	}

	function setTabProject(tabId: string, projectId: string) {
		tabs.setTabProject(tabId, projectId);
	}
</script>

<svelte:head>
	<title>AI Hub</title>
</svelte:head>

<div class="h-dvh flex bg-background text-foreground">
	<!-- Icon Rail (Desktop) - 48px wide -->
	<nav class="hidden lg:flex flex-col w-12 bg-card border-r border-border z-50 flex-shrink-0">
		<div class="flex flex-col items-center pt-3 gap-1">
			<button on:click={handleNewTab} class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors hover:bg-accent group" title="New Chat">
				<svg class="w-5 h-5 text-primary group-hover:scale-110 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
				</svg>
			</button>
			<div class="w-6 h-px bg-border my-2"></div>
			<button on:click={() => toggleSidebarSection('sessions')} class="relative w-10 h-10 rounded-lg flex items-center justify-center transition-colors {activeSidebarSection === 'sessions' ? 'bg-primary text-primary-foreground' : 'hover:bg-accent text-muted-foreground hover:text-foreground'}" title="Sessions">
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
				</svg>
				{#if $allTabs.length > 1}
					<span class="absolute -top-1 -right-1 w-4 h-4 bg-primary text-primary-foreground text-[10px] font-bold rounded-full flex items-center justify-center">{$allTabs.length}</span>
				{/if}
			</button>
			{#if $isAdmin}
				<button on:click={() => toggleSidebarSection('projects')} class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors {activeSidebarSection === 'projects' ? 'bg-primary text-primary-foreground' : 'hover:bg-accent text-muted-foreground hover:text-foreground'}" title="Projects">
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
					</svg>
				</button>
				<button on:click={() => toggleSidebarSection('profiles')} class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors {activeSidebarSection === 'profiles' ? 'bg-primary text-primary-foreground' : 'hover:bg-accent text-muted-foreground hover:text-foreground'}" title="Profiles">
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
					</svg>
				</button>
				<button on:click={() => toggleSidebarSection('subagents')} class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors {activeSidebarSection === 'subagents' ? 'bg-primary text-primary-foreground' : 'hover:bg-accent text-muted-foreground hover:text-foreground'}" title="Subagents">
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
					</svg>
				</button>
			{/if}
		</div>
		<div class="mt-auto flex flex-col items-center pb-3 gap-1">
			{#if $isAdmin}
				<a href="/settings" class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors hover:bg-accent text-muted-foreground hover:text-foreground" title="Settings">
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
					</svg>
				</a>
			{/if}
			<button on:click={handleLogout} class="w-10 h-10 rounded-full bg-primary/20 flex items-center justify-center hover:bg-primary/30 transition-colors" title="Logout ({$apiUser ? $apiUser.name : $username})">
				<span class="text-sm font-medium text-primary">{($apiUser?.name || $username)?.[0]?.toUpperCase() || 'U'}</span>
			</button>
		</div>
	</nav>

	<!-- Expandable Sidebar Panel (Desktop) -->
	{#if activeSidebarSection !== 'none'}
		<button class="hidden lg:block fixed inset-0 z-30 bg-black/20" on:click={closeSidebar} aria-label="Close sidebar"></button>
		<aside class="hidden lg:flex fixed inset-y-0 left-12 z-40 w-72 bg-card border-r border-border flex-col shadow-l sidebar-slide-in">
			<div class="p-4 border-b border-border flex items-center justify-between">
				<span class="font-semibold text-foreground">{activeSidebarSection === 'sessions' ? 'Sessions' : activeSidebarSection === 'projects' ? 'Projects' : activeSidebarSection === 'profiles' ? 'Profiles' : 'Subagents'}</span>
				<div class="flex items-center gap-1">
					{#if activeSidebarSection === 'sessions'}
						<button on:click={() => sessionSearchExpanded = !sessionSearchExpanded} class="p-1.5 rounded-md transition-colors {sessionSearchExpanded ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-accent'}" title="Search sessions">
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
							</svg>
						</button>
					{/if}
					<button on:click={toggleSidebarPin} class="p-1.5 rounded-md transition-colors {sidebarPinned ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-accent'}" title={sidebarPinned ? 'Unpin sidebar' : 'Pin sidebar open'}>
						<svg class="w-4 h-4" fill={sidebarPinned ? 'currentColor' : 'none'} stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 5a2 2 0 012-2h10a2 2 0 012 2v16l-7-3.5L5 21V5z" />
						</svg>
					</button>
					<button on:click={() => { activeSidebarSection = 'none'; sidebarPinned = false; clearSearch(); }} class="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent transition-colors" title="Close">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>
			</div>
			<!-- Search Bar (Desktop) -->
			{#if activeSidebarSection === 'sessions' && sessionSearchExpanded}
				<div class="px-3 py-2 border-b border-border">
					<div class="relative">
						<svg class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
						</svg>
						<input
							type="text"
							bind:value={sessionSearchQuery}
							placeholder="Search sessions..."
							class="w-full pl-9 pr-8 py-1.5 text-sm bg-accent border border-border rounded-md text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
						/>
						{#if sessionSearchQuery}
							<button on:click={clearSearch} class="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 text-muted-foreground hover:text-foreground">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
								</svg>
							</button>
						{/if}
					</div>
				</div>
			{/if}

			<!-- Sessions Panel Content -->
			{#if activeSidebarSection === 'sessions'}
				<div class="flex-1 overflow-hidden flex flex-col">
					{#if $isAdmin}
						<div class="px-3 pt-3 flex gap-1">
							<button on:click={() => sidebarTab = 'my-chats'} class="flex-1 px-3 py-1.5 text-xs font-medium rounded-md transition-colors {sidebarTab === 'my-chats' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-accent'}">My Chats</button>
							<button on:click={() => sidebarTab = 'admin'} class="flex-1 px-3 py-1.5 text-xs font-medium rounded-md transition-colors {sidebarTab === 'admin' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-accent'}">Admin</button>
						</div>
					{/if}
					{#if sidebarTab === 'my-chats'}
					<div class="flex-1 overflow-y-auto px-3 pb-3 pt-2">
						<!-- Open Tabs Section -->
						{#if $allTabs.length > 0}
							<div class="mb-4">
								<div class="flex items-center justify-between px-2 mb-2">
									<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium">Open ({$allTabs.length})</div>
								</div>
								<div class="space-y-1">
									{#each $allTabs as tab}
										{@const realSession = tab.sessionId ? $sessions.find(s => s.id === tab.sessionId) : null}
										{@const tabSession = realSession || { id: tab.sessionId || tab.id, title: tab.title, status: 'active', total_cost_usd: 0, total_tokens_in: 0, total_tokens_out: 0, cache_creation_tokens: 0, cache_read_tokens: 0, context_tokens: 0, turn_count: tab.messages.filter(m => m.role === 'user').length, profile_id: '', project_id: null, created_at: '', updated_at: new Date().toISOString() }}
										<SessionCard
											session={tabSession}
											isOpen={true}
											isActive={tab.id === $activeTabId}
											isStreaming={tab.isStreaming}
											showCloseButton={true}
											on:click={() => { tabs.setActiveTab(tab.id); closeSidebar(); }}
											on:close={(e) => handleCloseTab(e, tab.id)}
										/>
									{/each}
								</div>
							</div>
						{/if}

						<!-- Selection Actions Bar -->
						{#if $selectionMode && filteredSessions.length > 0}
							<div class="flex items-center gap-2 px-2 py-2 mb-2 bg-accent rounded-lg">
								<button
									on:click={() => {
										if ($selectedSessionIds.size === filteredSessions.length) {
											tabs.deselectAllSessions(false);
										} else {
											tabs.selectAllSessions(false);
										}
									}}
									class="text-xs text-muted-foreground hover:text-foreground transition-colors"
								>
									{$selectedSessionIds.size === filteredSessions.length ? 'Deselect All' : 'Select All'}
								</button>
								<span class="text-xs text-muted-foreground">
									{$selectedSessionIds.size} selected
								</span>
								{#if $selectedSessionIds.size > 0}
									<button
										on:click={() => handleBatchDelete(false)}
										class="ml-auto text-xs text-destructive hover:text-destructive/80 font-medium transition-colors"
									>
										Delete ({$selectedSessionIds.size})
									</button>
								{/if}
							</div>
						{/if}

						<!-- Search Results (when searching) -->
						{#if sessionSearchQuery.trim()}
							<div class="flex items-center justify-between px-2 mb-2">
								<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium">
									{isSearching ? 'Searching...' : `Results (${searchResults.length})`}
								</div>
								<button
									on:click={clearSearch}
									class="text-xs text-muted-foreground hover:text-foreground transition-colors"
								>
									Clear
								</button>
							</div>

							<div class="space-y-1">
								{#if isSearching}
									<div class="flex items-center justify-center py-8">
										<svg class="w-5 h-5 animate-spin text-muted-foreground" fill="none" viewBox="0 0 24 24">
											<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
											<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
										</svg>
									</div>
								{:else if searchResults.length === 0}
									<p class="text-xs text-muted-foreground px-2 py-4 text-center">No sessions found matching "{sessionSearchQuery}"</p>
								{:else}
									{#each searchResults as result}
										<button
											on:click={() => { openSession(result.id); closeSidebar(); }}
											class="w-full text-left px-3 py-2.5 rounded-lg hover:bg-accent transition-colors group"
										>
											<div class="flex items-start gap-2">
												<div class="flex-1 min-w-0">
													<div class="text-sm font-medium text-foreground truncate">
														{result.title || 'Untitled session'}
													</div>
													{#if result.match_type === 'content' && result.match_snippet}
														<div class="text-xs text-muted-foreground mt-1 line-clamp-2">
															{result.match_snippet}
														</div>
													{/if}
													<div class="flex items-center gap-2 mt-1">
														<span class="text-xs text-muted-foreground/70">
															{new Date(result.updated_at).toLocaleDateString()}
														</span>
														{#if result.match_type === 'content'}
															<span class="text-xs bg-accent px-1.5 py-0.5 rounded text-muted-foreground">
																Content match
															</span>
														{/if}
													</div>
												</div>
												<svg class="w-4 h-4 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
												</svg>
											</div>
										</button>
									{/each}
								{/if}
							</div>
						{:else}
							<!-- History Header (when not searching) -->
							<div class="flex items-center justify-between px-2 mb-2">
								<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium">History</div>
								<div class="flex items-center gap-2">
									<button
										on:click={() => tabs.loadSessions()}
										class="text-muted-foreground hover:text-foreground transition-colors p-0.5"
										title="Refresh sessions"
										disabled={$sessionsLoading}
									>
										<svg
											class="w-3.5 h-3.5 {$sessionsLoading ? 'animate-spin' : ''}"
											fill="none"
											stroke="currentColor"
											viewBox="0 0 24 24"
										>
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
										</svg>
									</button>
									{#if filteredSessions.length > 0}
										<button
											on:click={() => tabs.toggleSelectionMode(false)}
											class="text-xs text-muted-foreground hover:text-foreground transition-colors"
											title={$selectionMode ? 'Exit selection mode' : 'Select multiple'}
										>
											{$selectionMode ? 'Cancel' : 'Select'}
										</button>
									{/if}
								</div>
							</div>

							<!-- Grouped Sessions -->
							<div class="space-y-3" class:opacity-50={$sessionsLoading} class:pointer-events-none={$sessionsLoading}>
								{#each groupedSessions as group}
									<div>
										<!-- Group Header -->
										<button
											on:click={() => toggleGroupCollapse(group.key)}
											class="flex items-center gap-2 px-2 py-1 w-full text-left hover:bg-accent/50 rounded transition-colors"
										>
											<svg
												class="w-3 h-3 text-muted-foreground transition-transform {collapsedGroups.has(group.key) ? '' : 'rotate-90'}"
												fill="none"
												stroke="currentColor"
												viewBox="0 0 24 24"
											>
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
											</svg>
											<span class="text-xs text-muted-foreground uppercase tracking-wider font-medium">{group.label}</span>
											<span class="text-xs text-muted-foreground/60">({group.sessions.length})</span>
										</button>

										<!-- Group Content -->
										{#if !collapsedGroups.has(group.key)}
											<div class="mt-1 space-y-1">
												{#each group.sessions as session}
													<SessionCard
														{session}
														selectionMode={$selectionMode}
														isSelected={$selectedSessionIds.has(session.id)}
														on:click={() => { openSession(session.id); closeSidebar(); }}
														on:delete={() => deleteSession(null, session.id)}
														on:select={() => tabs.toggleSessionSelection(session.id, false)}
													/>
												{/each}
											</div>
										{/if}
									</div>
								{/each}
								{#if $sessionsLoading}
									<p class="text-xs text-muted-foreground px-2 animate-pulse">Loading sessions...</p>
								{:else if filteredSessions.length === 0}
									<p class="text-xs text-muted-foreground px-2">No chat history yet</p>
								{/if}
							</div>
						{/if}
					</div>
				{/if}

				<!-- Admin Tab Content -->
				{#if sidebarTab === 'admin' && $isAdmin}
					<div class="flex-1 overflow-y-auto px-3 pb-3 pt-2">
						<!-- Open Tabs Section (Admin) -->
						{#if $allTabs.length > 0}
							<div class="mb-4">
								<div class="flex items-center justify-between px-2 mb-2">
									<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium">Open ({$allTabs.length})</div>
								</div>
								<div class="space-y-1">
									{#each $allTabs as tab}
										{@const realSession = tab.sessionId ? $sessions.find(s => s.id === tab.sessionId) : null}
										{@const tabSession = realSession || { id: tab.sessionId || tab.id, title: tab.title, status: 'active', total_cost_usd: 0, total_tokens_in: 0, total_tokens_out: 0, cache_creation_tokens: 0, cache_read_tokens: 0, context_tokens: 0, turn_count: tab.messages.filter(m => m.role === 'user').length, profile_id: '', project_id: null, created_at: '', updated_at: new Date().toISOString() }}
										<SessionCard
											session={tabSession}
											isOpen={true}
											isActive={tab.id === $activeTabId}
											isStreaming={tab.isStreaming}
											showCloseButton={true}
											on:click={() => { tabs.setActiveTab(tab.id); closeSidebar(); }}
											on:close={(e) => handleCloseTab(e, tab.id)}
										/>
									{/each}
								</div>
							</div>
						{/if}

						<!-- API User Filter -->
						<div class="mb-3">
							<label class="text-xs text-muted-foreground uppercase tracking-wider px-2 block mb-1">Filter by User</label>
							<select
								class="w-full bg-accent border border-border rounded-md px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
								value={$adminSessionsFilter ?? ''}
								on:change={(e) => tabs.setAdminSessionsFilter(e.currentTarget.value || null)}
							>
								<option value="">All API Users</option>
								{#each $apiUsers as user}
									<option value={user.id}>{user.name}</option>
								{/each}
							</select>
						</div>

						<!-- Selection Actions Bar -->
						{#if $adminSelectionMode && filteredAdminSessions.length > 0}
							<div class="flex items-center gap-2 px-2 py-2 mb-2 bg-accent rounded-lg">
								<button
									on:click={() => {
										if ($selectedAdminSessionIds.size === filteredAdminSessions.length) {
											tabs.deselectAllSessions(true);
										} else {
											tabs.selectAllSessions(true);
										}
									}}
									class="text-xs text-muted-foreground hover:text-foreground transition-colors"
								>
									{$selectedAdminSessionIds.size === filteredAdminSessions.length ? 'Deselect All' : 'Select All'}
								</button>
								<span class="text-xs text-muted-foreground">
									{$selectedAdminSessionIds.size} selected
								</span>
								{#if $selectedAdminSessionIds.size > 0}
									<button
										on:click={() => handleBatchDelete(true)}
										class="ml-auto text-xs text-destructive hover:text-destructive/80 font-medium transition-colors"
									>
										Delete ({$selectedAdminSessionIds.size})
									</button>
								{/if}
							</div>
						{/if}

						<!-- Search Results (when searching in Admin tab) -->
						{#if sessionSearchQuery.trim()}
							<div class="flex items-center justify-between px-2 mb-2">
								<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium">
									{isSearching ? 'Searching...' : `Results (${searchResults.length})`}
								</div>
								<button
									on:click={clearSearch}
									class="text-xs text-muted-foreground hover:text-foreground transition-colors"
								>
									Clear
								</button>
							</div>

							<div class="space-y-1">
								{#if isSearching}
									<div class="flex items-center justify-center py-8">
										<svg class="w-5 h-5 animate-spin text-muted-foreground" fill="none" viewBox="0 0 24 24">
											<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
											<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
										</svg>
									</div>
								{:else if searchResults.length === 0}
									<p class="text-xs text-muted-foreground px-2 py-4 text-center">No sessions found matching "{sessionSearchQuery}"</p>
								{:else}
									{#each searchResults as result}
										<button
											on:click={() => { openSession(result.id); closeSidebar(); }}
											class="w-full text-left px-3 py-2.5 rounded-lg hover:bg-accent transition-colors group"
										>
											<div class="flex items-start gap-2">
												<div class="flex-1 min-w-0">
													<div class="text-sm font-medium text-foreground truncate">
														{result.title || 'Untitled session'}
													</div>
													{#if result.match_type === 'content' && result.match_snippet}
														<div class="text-xs text-muted-foreground mt-1 line-clamp-2">
															{result.match_snippet}
														</div>
													{/if}
													<div class="flex items-center gap-2 mt-1">
														<span class="text-xs text-muted-foreground/70">
															{new Date(result.updated_at).toLocaleDateString()}
														</span>
														{#if result.match_type === 'content'}
															<span class="text-xs bg-accent px-1.5 py-0.5 rounded text-muted-foreground">
																Content match
															</span>
														{/if}
													</div>
												</div>
												<svg class="w-4 h-4 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
												</svg>
											</div>
										</button>
									{/each}
								{/if}
							</div>
						{:else}
							<!-- Header with label and selection toggle (when not searching) -->
							<div class="flex items-center justify-between px-2 mb-2">
								<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium">API User Sessions</div>
								<div class="flex items-center gap-2">
									<button
										on:click={() => tabs.loadAdminSessions($adminSessionsFilter)}
										class="text-muted-foreground hover:text-foreground transition-colors p-0.5"
										title="Refresh sessions"
									>
										<svg
											class="w-3.5 h-3.5"
											fill="none"
											stroke="currentColor"
											viewBox="0 0 24 24"
										>
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
										</svg>
									</button>
									{#if filteredAdminSessions.length > 0}
										<button
											on:click={() => tabs.toggleSelectionMode(true)}
											class="text-xs text-muted-foreground hover:text-foreground transition-colors"
											title={$adminSelectionMode ? 'Exit selection mode' : 'Select multiple'}
										>
											{$adminSelectionMode ? 'Cancel' : 'Select'}
										</button>
									{/if}
								</div>
							</div>

							<!-- Grouped Admin Sessions -->
							<div class="space-y-3">
								{#each groupedAdminSessions as group}
									<div>
										<!-- Group Header -->
										<button
											on:click={() => toggleGroupCollapse(group.key)}
											class="flex items-center gap-2 px-2 py-1 w-full text-left hover:bg-accent/50 rounded transition-colors"
										>
											<svg
												class="w-3 h-3 text-muted-foreground transition-transform {collapsedGroups.has(group.key) ? '' : 'rotate-90'}"
												fill="none"
												stroke="currentColor"
												viewBox="0 0 24 24"
											>
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
											</svg>
											<span class="text-xs text-muted-foreground uppercase tracking-wider font-medium">{group.label}</span>
											<span class="text-xs text-muted-foreground/60">({group.sessions.length})</span>
										</button>

										<!-- Group Content -->
										{#if !collapsedGroups.has(group.key)}
											<div class="mt-1 space-y-1">
												{#each group.sessions as session}
													<SessionCard
														{session}
														selectionMode={$adminSelectionMode}
														isSelected={$selectedAdminSessionIds.has(session.id)}
														on:click={() => { openSession(session.id); closeSidebar(); }}
														on:delete={() => deleteSession(null, session.id)}
														on:select={() => tabs.toggleSessionSelection(session.id, true)}
													/>
												{/each}
											</div>
										{/if}
									</div>
								{/each}
								{#if filteredAdminSessions.length === 0}
									<p class="text-xs text-muted-foreground px-2">No API user sessions found</p>
								{/if}
							</div>
						{/if}
					</div>
				{/if}
				</div>
			{/if}

			<!-- Projects Panel Content -->
			{#if activeSidebarSection === 'projects'}
				{@const projectsOrganized = organizeByGroups($projects, 'projects', $groups)}
				<div class="flex-1 overflow-y-auto p-3 flex flex-col">
					{#if $activeTab?.sessionId}
						<div class="flex items-center gap-2 p-3 mb-4 bg-muted/50 rounded-lg border border-border">
							<svg class="w-4 h-4 text-muted-foreground flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
							</svg>
							<p class="text-xs text-muted-foreground">Project is locked for this chat</p>
						</div>
					{/if}
					<div class="flex-1 overflow-y-auto space-y-1 -mx-1 px-1">
						<!-- Grouped projects -->
						{#each projectsOrganized.groupOrder as group}
							{@const groupProjects = projectsOrganized.grouped.get(group.name) || []}
							{#if groupProjects.length > 0}
								<div class="mb-2">
									<button
										class="w-full flex items-center gap-2 px-2 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
										on:click={() => groups.toggleGroupCollapsed('projects', group.name)}
									>
										<svg class="w-3 h-3 transition-transform {group.collapsed ? '-rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
										</svg>
										<span class="uppercase tracking-wide">{group.name}</span>
										<span class="text-muted-foreground/60">({groupProjects.length})</span>
									</button>
									{#if !group.collapsed}
										<div class="space-y-1 ml-2">
											{#each groupProjects as project}
												<button
													class="w-full flex items-center gap-3 p-3 bg-accent rounded-lg transition-colors text-left {$activeTab?.project === project.id ? 'ring-2 ring-primary' : ''} {$activeTab?.sessionId ? 'opacity-50 cursor-not-allowed' : 'hover:bg-accent/80'}"
													on:click={() => { if ($activeTabId && !$activeTab?.sessionId) { setTabProject($activeTabId, project.id); closeSidebar(); } }}
													disabled={!!$activeTab?.sessionId}
												>
													<svg class="w-5 h-5 text-muted-foreground flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
													</svg>
													<div class="flex-1 min-w-0">
														<p class="text-sm text-foreground font-medium truncate">{project.name}</p>
														<p class="text-xs text-muted-foreground truncate">/workspace/{project.path}/</p>
													</div>
												</button>
											{/each}
										</div>
									{/if}
								</div>
							{/if}
						{/each}
						<!-- Ungrouped projects -->
						{#if projectsOrganized.ungrouped.length > 0}
							{#if projectsOrganized.groupOrder.length > 0}
								<div class="px-2 py-1.5 text-xs font-medium text-muted-foreground uppercase tracking-wide">Other</div>
							{/if}
							<div class="space-y-1 {projectsOrganized.groupOrder.length > 0 ? 'ml-2' : ''}">
								{#each projectsOrganized.ungrouped as project}
									<button
										class="w-full flex items-center gap-3 p-3 bg-accent rounded-lg transition-colors text-left {$activeTab?.project === project.id ? 'ring-2 ring-primary' : ''} {$activeTab?.sessionId ? 'opacity-50 cursor-not-allowed' : 'hover:bg-accent/80'}"
										on:click={() => { if ($activeTabId && !$activeTab?.sessionId) { setTabProject($activeTabId, project.id); closeSidebar(); } }}
										disabled={!!$activeTab?.sessionId}
									>
										<svg class="w-5 h-5 text-muted-foreground flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
										</svg>
										<div class="flex-1 min-w-0">
											<p class="text-sm text-foreground font-medium truncate">{project.name}</p>
											<p class="text-xs text-muted-foreground truncate">/workspace/{project.path}/</p>
										</div>
									</button>
								{/each}
							</div>
						{/if}
						{#if $projects.length === 0}
							<p class="text-xs text-muted-foreground px-2 py-4 text-center">No projects yet</p>
						{/if}
					</div>
					<div class="mt-auto pt-3">
						<button on:click={() => showProjectModal = true} class="w-full py-2 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-muted-foreground transition-colors">{$projects.length === 0 ? '+ Create Project' : 'Manage Projects'}</button>
					</div>
				</div>
			{/if}

			<!-- Profiles Panel Content -->
			{#if activeSidebarSection === 'profiles'}
				{@const profilesOrganized = organizeByGroups($profiles, 'profiles', $groups)}
				<div class="flex-1 overflow-y-auto p-3 flex flex-col">
					<div class="flex-1 overflow-y-auto space-y-1 -mx-1 px-1">
						<!-- Grouped profiles -->
						{#each profilesOrganized.groupOrder as group}
							{@const groupProfiles = profilesOrganized.grouped.get(group.name) || []}
							{#if groupProfiles.length > 0}
								<div class="mb-2">
									<button
										class="w-full flex items-center gap-2 px-2 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
										on:click={() => groups.toggleGroupCollapsed('profiles', group.name)}
									>
										<svg class="w-3 h-3 transition-transform {group.collapsed ? '-rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
										</svg>
										<span class="uppercase tracking-wide">{group.name}</span>
										<span class="text-muted-foreground/60">({groupProfiles.length})</span>
									</button>
									{#if !group.collapsed}
										<div class="space-y-1 ml-2">
											{#each groupProfiles as profile}
												<button
													class="w-full flex items-center gap-3 p-3 bg-accent rounded-lg hover:bg-accent/80 transition-colors text-left {$activeTab?.profile === profile.id ? 'ring-2 ring-primary' : ''}"
													on:click={() => { if ($activeTabId) setTabProfile($activeTabId, profile.id); closeSidebar(); }}
												>
													<svg class="w-5 h-5 text-muted-foreground flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
													</svg>
													<div class="flex-1 min-w-0">
														<p class="text-sm text-foreground font-medium truncate">{profile.name}</p>
														<p class="text-xs text-muted-foreground truncate">{profile.description || 'No description'}</p>
													</div>
													{#if profile.is_builtin}
														<span class="text-[10px] px-1.5 py-0.5 bg-muted text-muted-foreground rounded">Built-in</span>
													{/if}
												</button>
											{/each}
										</div>
									{/if}
								</div>
							{/if}
						{/each}
						<!-- Ungrouped profiles -->
						{#if profilesOrganized.ungrouped.length > 0}
							{#if profilesOrganized.groupOrder.length > 0}
								<div class="px-2 py-1.5 text-xs font-medium text-muted-foreground uppercase tracking-wide">Other</div>
							{/if}
							<div class="space-y-1 {profilesOrganized.groupOrder.length > 0 ? 'ml-2' : ''}">
								{#each profilesOrganized.ungrouped as profile}
									<button
										class="w-full flex items-center gap-3 p-3 bg-accent rounded-lg hover:bg-accent/80 transition-colors text-left {$activeTab?.profile === profile.id ? 'ring-2 ring-primary' : ''}"
										on:click={() => { if ($activeTabId) setTabProfile($activeTabId, profile.id); closeSidebar(); }}
									>
										<svg class="w-5 h-5 text-muted-foreground flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
										</svg>
										<div class="flex-1 min-w-0">
											<p class="text-sm text-foreground font-medium truncate">{profile.name}</p>
											<p class="text-xs text-muted-foreground truncate">{profile.description || 'No description'}</p>
										</div>
										{#if profile.is_builtin}
											<span class="text-[10px] px-1.5 py-0.5 bg-muted text-muted-foreground rounded">Built-in</span>
										{/if}
									</button>
								{/each}
							</div>
						{/if}
						{#if $profiles.length === 0}
							<p class="text-xs text-muted-foreground px-2 py-4 text-center">No profiles yet</p>
						{/if}
					</div>
					<div class="mt-auto pt-3">
						<button on:click={() => showProfileModal = true} class="w-full py-2 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-muted-foreground transition-colors">{$profiles.length === 0 ? '+ Create Profile' : 'Manage Profiles'}</button>
					</div>
				</div>
			{/if}

			<!-- Subagents Panel Content -->
			{#if activeSidebarSection === 'subagents'}
				{@const subagentsOrganized = organizeByGroups(allSubagents, 'subagents', $groups)}
				<div class="flex-1 overflow-y-auto p-3 flex flex-col">
					<div class="flex-1 overflow-y-auto space-y-1 -mx-1 px-1">
						<!-- Grouped subagents -->
						{#each subagentsOrganized.groupOrder as group}
							{@const groupAgents = subagentsOrganized.grouped.get(group.name) || []}
							{#if groupAgents.length > 0}
								<div class="mb-2">
									<button
										class="w-full flex items-center gap-2 px-2 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
										on:click={() => groups.toggleGroupCollapsed('subagents', group.name)}
									>
										<svg class="w-3 h-3 transition-transform {group.collapsed ? '-rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
										</svg>
										<span class="uppercase tracking-wide">{group.name}</span>
										<span class="text-muted-foreground/60">({groupAgents.length})</span>
									</button>
									{#if !group.collapsed}
										<div class="space-y-1 ml-2">
											{#each groupAgents as agent}
												<div class="w-full p-3 bg-accent rounded-lg">
													<div class="flex items-start gap-3">
														<svg class="w-5 h-5 text-muted-foreground flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
														</svg>
														<div class="flex-1 min-w-0">
															<div class="flex items-center gap-2">
																<p class="text-sm text-foreground font-medium truncate">{agent.name}</p>
																{#if agent.is_builtin}
																	<span class="text-[10px] px-1.5 py-0.5 bg-muted text-muted-foreground rounded">Built-in</span>
																{/if}
															</div>
															<p class="text-xs text-muted-foreground line-clamp-2">{agent.description}</p>
															{#if agent.model}
																<span class="text-[10px] text-primary mt-1 inline-block">{agent.model}</span>
															{/if}
														</div>
													</div>
												</div>
											{/each}
										</div>
									{/if}
								</div>
							{/if}
						{/each}
						<!-- Ungrouped subagents -->
						{#if subagentsOrganized.ungrouped.length > 0}
							{#if subagentsOrganized.groupOrder.length > 0}
								<div class="px-2 py-1.5 text-xs font-medium text-muted-foreground uppercase tracking-wide">Other</div>
							{/if}
							<div class="space-y-1 {subagentsOrganized.groupOrder.length > 0 ? 'ml-2' : ''}">
								{#each subagentsOrganized.ungrouped as agent}
									<div class="w-full p-3 bg-accent rounded-lg">
										<div class="flex items-start gap-3">
											<svg class="w-5 h-5 text-muted-foreground flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
											</svg>
											<div class="flex-1 min-w-0">
												<div class="flex items-center gap-2">
													<p class="text-sm text-foreground font-medium truncate">{agent.name}</p>
													{#if agent.is_builtin}
														<span class="text-[10px] px-1.5 py-0.5 bg-muted text-muted-foreground rounded">Built-in</span>
													{/if}
												</div>
												<p class="text-xs text-muted-foreground line-clamp-2">{agent.description}</p>
												{#if agent.model}
													<span class="text-[10px] text-primary mt-1 inline-block">{agent.model}</span>
												{/if}
											</div>
										</div>
									</div>
								{/each}
							</div>
						{/if}
						{#if allSubagents.length === 0}
							<div class="text-center py-8">
								<svg class="w-12 h-12 mx-auto text-muted-foreground/30 mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
								</svg>
								<p class="text-xs text-muted-foreground">No subagents configured</p>
							</div>
						{/if}
					</div>
					<div class="mt-auto pt-3">
						<button on:click={() => showSubagentManager = true} class="w-full py-2 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-muted-foreground transition-colors">{allSubagents.length === 0 ? '+ Create Subagent' : 'Manage Subagents'}</button>
					</div>
				</div>
			{/if}
		</aside>
	{/if}

	<!-- Mobile Sidebar (full width overlay) -->
	<aside class="lg:hidden fixed inset-y-0 left-0 z-50 w-72 bg-card border-r border-border transform transition-transform duration-200 {sidebarOpen ? 'translate-x-0' : '-translate-x-full'}">
		<div class="h-full flex flex-col">
			<div class="p-4 border-b border-border flex items-center justify-between">
				<div class="flex items-center gap-2">
					<div class="w-8 h-8 rounded-lg bg-primary flex items-center justify-center shadow-s">
						<svg class="w-5 h-5 text-primary-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
						</svg>
					</div>
					<span class="font-semibold text-foreground">AI Hub</span>
				</div>
				<button class="text-muted-foreground hover:text-foreground p-1" on:click={() => (sidebarOpen = false)}>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>
			<div class="p-3 space-y-2">
				<!-- Mobile Search (tap to expand) -->
				{#if sessionSearchExpanded}
					<div class="relative">
						<svg class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
						</svg>
						<input
							type="text"
							bind:value={sessionSearchQuery}
							placeholder="Search sessions..."
							class="w-full pl-9 pr-8 py-2 text-sm bg-accent border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
						/>
						<button on:click={clearSearch} class="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-muted-foreground hover:text-foreground">
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
							</svg>
						</button>
					</div>
				{:else}
					<button
						on:click={() => sessionSearchExpanded = true}
						class="w-full flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground bg-accent/50 hover:bg-accent rounded-lg transition-colors"
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
						</svg>
						<span>Search sessions...</span>
					</button>
				{/if}
			</div>
			<!-- Mobile Tab Toggle (My Chats / Admin) -->
			{#if $isAdmin}
				<div class="px-3 pb-2">
					<div class="flex bg-muted rounded-lg p-0.5">
						<button on:click={() => sidebarTab = 'my-chats'} class="flex-1 px-3 py-1.5 text-xs font-medium rounded-md transition-colors {sidebarTab === 'my-chats' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'}">My Chats</button>
						<button on:click={() => sidebarTab = 'admin'} class="flex-1 px-3 py-1.5 text-xs font-medium rounded-md transition-colors {sidebarTab === 'admin' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'}">Admin</button>
					</div>
				</div>
			{/if}

			<div class="flex-1 overflow-y-auto px-3 pb-3">
				{#if sidebarTab === 'my-chats'}
					<!-- Open Tabs Section (Mobile) -->
					{#if $allTabs.length > 0}
						<div class="mb-4">
							<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium px-2 mb-2">Open ({$allTabs.length})</div>
							<div class="space-y-1">
								{#each $allTabs as tab}
									{@const realSession = tab.sessionId ? $sessions.find(s => s.id === tab.sessionId) : null}
									{@const tabSession = realSession || { id: tab.sessionId || tab.id, title: tab.title, status: 'active', total_cost_usd: 0, total_tokens_in: 0, total_tokens_out: 0, cache_creation_tokens: 0, cache_read_tokens: 0, context_tokens: 0, turn_count: tab.messages.filter(m => m.role === 'user').length, profile_id: '', project_id: null, created_at: '', updated_at: new Date().toISOString() }}
									<SessionCard
										session={tabSession}
										isOpen={true}
										isActive={tab.id === $activeTabId}
										isStreaming={tab.isStreaming}
										showCloseButton={true}
										abbreviated={true}
										on:click={() => { tabs.setActiveTab(tab.id); sidebarOpen = false; }}
										on:close={(e) => handleCloseTab(e, tab.id)}
									/>
								{/each}
							</div>
						</div>
					{/if}

					<!-- History Header (Mobile) -->
					<div class="flex items-center justify-between px-2 mb-2">
						<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium">History</div>
						<button
							on:click={() => tabs.loadSessions()}
							class="text-muted-foreground hover:text-foreground transition-colors p-0.5"
							title="Refresh sessions"
							disabled={$sessionsLoading}
						>
							<svg
								class="w-3.5 h-3.5 {$sessionsLoading ? 'animate-spin' : ''}"
								fill="none"
								stroke="currentColor"
								viewBox="0 0 24 24"
							>
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
							</svg>
						</button>
					</div>

					<!-- Grouped Sessions (Mobile) -->
					<div class="space-y-3">
						{#each groupedSessions as group}
							<div>
								<!-- Group Header -->
								<button
									on:click={() => toggleGroupCollapse(group.key)}
									class="flex items-center gap-2 px-2 py-1.5 w-full text-left active:bg-accent/50 rounded transition-colors"
								>
									<svg
										class="w-3 h-3 text-muted-foreground transition-transform {collapsedGroups.has(group.key) ? '' : 'rotate-90'}"
										fill="none"
										stroke="currentColor"
										viewBox="0 0 24 24"
									>
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
									</svg>
									<span class="text-xs text-muted-foreground uppercase tracking-wider font-medium">{group.label}</span>
									<span class="text-xs text-muted-foreground/60">({group.sessions.length})</span>
								</button>

								<!-- Group Content -->
								{#if !collapsedGroups.has(group.key)}
									<div class="mt-1 space-y-1">
										{#each group.sessions as session}
											<SessionCard
												{session}
												abbreviated={true}
												on:click={() => { openSession(session.id); sidebarOpen = false; }}
												on:delete={() => deleteSession(null, session.id)}
											/>
										{/each}
									</div>
								{/if}
							</div>
						{/each}
						{#if filteredSessions.length === 0}
							<p class="text-xs text-muted-foreground px-2">{sessionSearchQuery ? 'No matching sessions' : 'No chat history yet'}</p>
						{/if}
					</div>
				{:else if sidebarTab === 'admin'}
					<!-- Admin Tab Content (Mobile) -->
					<!-- Open Tabs Section (Mobile Admin) -->
					{#if $allTabs.length > 0}
						<div class="mb-4">
							<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium px-2 mb-2">Open ({$allTabs.length})</div>
							<div class="space-y-1">
								{#each $allTabs as tab}
									{@const realSession = tab.sessionId ? $sessions.find(s => s.id === tab.sessionId) : null}
									{@const tabSession = realSession || { id: tab.sessionId || tab.id, title: tab.title, status: 'active', total_cost_usd: 0, total_tokens_in: 0, total_tokens_out: 0, cache_creation_tokens: 0, cache_read_tokens: 0, context_tokens: 0, turn_count: tab.messages.filter(m => m.role === 'user').length, profile_id: '', project_id: null, created_at: '', updated_at: new Date().toISOString() }}
									<SessionCard
										session={tabSession}
										isOpen={true}
										isActive={tab.id === $activeTabId}
										isStreaming={tab.isStreaming}
										showCloseButton={true}
										abbreviated={true}
										on:click={() => { tabs.setActiveTab(tab.id); sidebarOpen = false; }}
										on:close={(e) => handleCloseTab(e, tab.id)}
									/>
								{/each}
							</div>
						</div>
					{/if}

					<!-- API User Filter -->
					<div class="flex items-center justify-between px-2 mb-2">
						<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium">API User Sessions</div>
						<button
							on:click={() => tabs.loadAdminSessions($adminSessionsFilter)}
							class="text-muted-foreground hover:text-foreground transition-colors p-0.5"
							title="Refresh sessions"
						>
							<svg
								class="w-3.5 h-3.5"
								fill="none"
								stroke="currentColor"
								viewBox="0 0 24 24"
							>
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
							</svg>
						</button>
					</div>
					<div class="mb-3">
						<label class="text-xs text-muted-foreground uppercase tracking-wider px-2 block mb-1">Filter by User</label>
						<select
							class="w-full bg-accent border border-border rounded-md px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
							value={$adminSessionsFilter ?? ''}
							on:change={(e) => tabs.setAdminSessionsFilter(e.currentTarget.value || null)}
						>
							<option value="">All API Users</option>
							{#each $apiUsers as user}
								<option value={user.id}>{user.name}</option>
							{/each}
						</select>
					</div>

					<!-- Grouped Admin Sessions (Mobile) -->
					<div class="space-y-3">
						{#each groupedAdminSessions as group}
							<div>
								<!-- Group Header -->
								<button
									on:click={() => toggleGroupCollapse(group.key)}
									class="flex items-center gap-2 px-2 py-1.5 w-full text-left active:bg-accent/50 rounded transition-colors"
								>
									<svg
										class="w-3 h-3 text-muted-foreground transition-transform {collapsedGroups.has(group.key) ? '' : 'rotate-90'}"
										fill="none"
										stroke="currentColor"
										viewBox="0 0 24 24"
									>
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
									</svg>
									<span class="text-xs text-muted-foreground uppercase tracking-wider font-medium">{group.label}</span>
									<span class="text-xs text-muted-foreground/60">({group.sessions.length})</span>
								</button>

								<!-- Group Content -->
								{#if !collapsedGroups.has(group.key)}
									<div class="mt-1 space-y-1">
										{#each group.sessions as session}
											<SessionCard
												{session}
												abbreviated={true}
												on:click={() => { openSession(session.id); sidebarOpen = false; }}
												on:delete={() => deleteSession(null, session.id)}
											/>
										{/each}
									</div>
								{/if}
							</div>
						{/each}
						{#if filteredAdminSessions.length === 0}
							<p class="text-xs text-muted-foreground px-2">{sessionSearchQuery ? 'No matching sessions' : 'No API user sessions found'}</p>
						{/if}
					</div>
				{/if}
			</div>
			<div class="p-3 border-t border-border">
				<div class="flex items-center justify-between">
					<div class="flex items-center gap-2">
						<div class="w-8 h-8 rounded-full bg-primary/20 flex items-center justify-center shadow-s">
							<span class="text-sm text-primary">{$username?.[0]?.toUpperCase() || 'U'}</span>
						</div>
						<span class="text-sm text-muted-foreground">{$username}</span>
					</div>
					<button on:click={handleLogout} class="text-muted-foreground hover:text-foreground text-sm">Logout</button>
				</div>
			</div>
		</div>
	</aside>

	<!-- Mobile Sidebar Backdrop -->
	{#if sidebarOpen}
		<button class="lg:hidden fixed inset-0 z-40 bg-black/50" on:click={() => (sidebarOpen = false)} aria-label="Close sidebar"></button>
	{/if}

	<!-- Mobile Tools Menu Popup -->
	{#if showToolsMenu}
		<button class="lg:hidden fixed inset-0 z-50 bg-black/30" on:click={() => showToolsMenu = false} aria-label="Close tools menu"></button>
		<div class="lg:hidden fixed bottom-16 left-1/2 -translate-x-1/2 z-50 bg-card border border-border rounded-xl shadow-lg p-2 min-w-[200px]">
			<div class="flex flex-col gap-1">
				<button
					on:click={() => { showToolsMenu = false; showProjectModal = true; }}
					class="flex items-center gap-3 px-4 py-3 rounded-lg hover:bg-accent transition-colors text-left"
				>
					<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
					</svg>
					<span class="text-sm font-medium">Projects</span>
				</button>
				<button
					on:click={() => { showToolsMenu = false; showProfileModal = true; }}
					class="flex items-center gap-3 px-4 py-3 rounded-lg hover:bg-accent transition-colors text-left"
				>
					<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
					</svg>
					<span class="text-sm font-medium">Profiles</span>
				</button>
				<button
					on:click={() => { showToolsMenu = false; showSubagentManager = true; }}
					class="flex items-center gap-3 px-4 py-3 rounded-lg hover:bg-accent transition-colors text-left"
				>
					<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
					</svg>
					<span class="text-sm font-medium">Subagents</span>
				</button>
			</div>
		</div>
	{/if}

	<!-- Mobile Bottom Navigation -->
	<nav class="lg:hidden fixed bottom-0 left-0 right-0 z-50 bg-card border-t border-border safe-bottom">
		<div class="flex items-center justify-around h-14">
			<button on:click={handleNewTab} class="flex flex-col items-center justify-center w-16 h-full text-muted-foreground hover:text-foreground transition-colors">
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
				</svg>
				<span class="text-xs mt-0.5">New</span>
			</button>
			<button on:click={() => sidebarOpen = true} class="relative flex flex-col items-center justify-center w-16 h-full text-muted-foreground hover:text-foreground transition-colors">
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
				</svg>
				<span class="text-xs mt-0.5">Chats</span>
				{#if $allTabs.length > 1}
					<span class="absolute top-1 right-2 w-4 h-4 bg-primary text-primary-foreground text-[10px] font-bold rounded-full flex items-center justify-center">{$allTabs.length}</span>
				{/if}
			</button>
			{#if $isAdmin}
				<button on:click={() => showToolsMenu = !showToolsMenu} class="relative flex flex-col items-center justify-center w-16 h-full text-muted-foreground hover:text-foreground transition-colors">
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
					</svg>
					<span class="text-xs mt-0.5">Tools</span>
				</button>
				<a href="/settings" class="flex flex-col items-center justify-center w-16 h-full text-muted-foreground hover:text-foreground transition-colors">
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
					</svg>
					<span class="text-xs mt-0.5">Settings</span>
				</a>
			{/if}
		</div>
	</nav>

	<!-- Main Content -->
	<main class="flex-1 flex flex-col min-w-0 bg-background pb-14 lg:pb-0">
		<!-- Context Bar -->
		{#if $activeTab}
			{@const currentTab = $activeTab}
			{@const tabId = currentTab.id}
			<div class="bg-card border-b border-border h-12 flex items-center px-2 sm:px-4">
				<!-- Mobile Menu Button -->
				<button class="lg:hidden p-2 mr-2 text-muted-foreground hover:text-foreground rounded-md hover:bg-accent transition-colors" on:click={() => (sidebarOpen = true)}>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
					</svg>
				</button>

				<!-- Profile Selector (clickable text style with dropdown) -->
				{#if $apiUser?.profile_id}
					<!-- API user with locked profile - show as locked indicator -->
					<div class="flex items-center gap-1 px-2 py-1 text-sm text-foreground opacity-75">
						<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
						</svg>
						<span class="hidden sm:inline max-w-[120px] truncate">{$profiles.find((p) => p.id === $apiUser.profile_id)?.name || $apiUser.profile_id}</span>
						<svg class="w-3 h-3 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24" title="Profile locked by API key">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
						</svg>
					</div>
				{:else}
					{@const headerProfilesOrganized = organizeByGroups($profiles, 'profiles', $groups)}
					{@const hasProfileGroups = headerProfilesOrganized.groupOrder.length > 0}
					<div class="relative group">
						<button
							class="flex items-center gap-1 px-2 py-1 text-sm text-foreground hover:bg-accent rounded-md transition-colors"
						>
							<svg class="w-4 h-4 {currentTab.profile ? 'text-muted-foreground' : 'text-amber-500'}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
							</svg>
							<span class="hidden sm:inline max-w-[120px] truncate {!currentTab.profile ? 'text-amber-500' : ''}">{$profiles.find((p) => p.id === currentTab.profile)?.name || 'Select Profile'}</span>
							<svg class="w-3 h-3 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
							</svg>
						</button>
						<!-- Profile dropdown -->
						<div class="absolute left-0 top-full mt-1 w-56 bg-card border border-border rounded-lg shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50 max-h-80 overflow-y-auto">
							<div class="py-1">
								{#if $profiles.length === 0}
									<p class="px-3 py-2 text-sm text-muted-foreground">No profiles yet</p>
								{:else}
									<!-- Grouped profiles -->
									{#each headerProfilesOrganized.groupOrder as group}
										{@const groupProfiles = headerProfilesOrganized.grouped.get(group.name) || []}
										{#if groupProfiles.length > 0}
											<div class="py-1">
												<button
													class="w-full flex items-center gap-2 px-3 py-1 text-xs font-medium text-muted-foreground hover:text-foreground uppercase tracking-wide bg-muted/30 transition-colors"
													on:click|stopPropagation={() => groups.toggleGroupCollapsed('profiles', group.name)}
												>
													<svg class="w-3 h-3 transition-transform {group.collapsed ? '-rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
													</svg>
													<span>{group.name}</span>
													<span class="text-muted-foreground/60 ml-auto">({groupProfiles.length})</span>
												</button>
												{#if !group.collapsed}
													{#each groupProfiles as profile}
														<button
															on:click={() => setTabProfile(tabId, profile.id)}
															class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors {currentTab.profile === profile.id ? 'text-primary bg-accent/50' : 'text-foreground'}"
														>
															{profile.name}
														</button>
													{/each}
												{/if}
											</div>
										{/if}
									{/each}
									<!-- Ungrouped profiles -->
									{#if headerProfilesOrganized.ungrouped.length > 0}
										{#if hasProfileGroups}
											<div class="py-1">
												<div class="px-3 py-1 text-xs font-medium text-muted-foreground uppercase tracking-wide bg-muted/30">
													Other
												</div>
											</div>
										{/if}
										{#each headerProfilesOrganized.ungrouped as profile}
											<button
												on:click={() => setTabProfile(tabId, profile.id)}
												class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors {currentTab.profile === profile.id ? 'text-primary bg-accent/50' : 'text-foreground'}"
											>
												{profile.name}
											</button>
										{/each}
									{/if}
								{/if}
								<div class="border-t border-border my-1"></div>
								<button
									on:click={() => (showProfileModal = true)}
									class="w-full px-3 py-2 text-left text-sm text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
								>
									{$profiles.length === 0 ? 'Create Profile...' : 'Manage Profiles...'}
								</button>
							</div>
						</div>
					</div>
				{/if}

				<!-- Breadcrumb separator -->
				<span class="text-muted-foreground/50 mx-1 hidden sm:inline">/</span>

				<!-- Project Selector (locked when session exists or for API users) -->
				{#if $apiUser?.project_id}
					<!-- API user with locked project - show as locked indicator -->
					<div class="flex items-center gap-1 px-2 py-1 text-sm text-foreground opacity-75">
						<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
						</svg>
						<span class="hidden sm:inline max-w-[120px] truncate">{$projects.find((p) => p.id === $apiUser.project_id)?.name || $apiUser.project_id}</span>
						<svg class="w-3 h-3 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24" title="Project locked by API key">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
						</svg>
					</div>
				{:else}
					<div class="relative group">
						<button
							class="flex items-center gap-1 px-2 py-1 text-sm text-foreground {currentTab.sessionId ? 'cursor-default opacity-75' : 'hover:bg-accent'} rounded-md transition-colors"
							title={currentTab.sessionId ? 'Project is locked for this chat' : 'Select project'}
						>
							<svg class="w-4 h-4 {currentTab.project ? 'text-muted-foreground' : 'text-amber-500'}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
							</svg>
							<span class="hidden sm:inline max-w-[120px] truncate {!currentTab.project ? 'text-amber-500' : ''}">{$projects.find((p) => p.id === currentTab.project)?.name || 'Select Project'}</span>
							{#if currentTab.sessionId}
								<!-- Lock icon when session exists -->
								<svg class="w-3 h-3 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
								</svg>
							{:else}
								<svg class="w-3 h-3 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							{/if}
						</button>
						<!-- Project dropdown (only show when no session) -->
						{#if !currentTab.sessionId}
							{@const headerProjectsOrganized = organizeByGroups($projects, 'projects', $groups)}
							{@const hasGroups = headerProjectsOrganized.groupOrder.length > 0}
							<div class="absolute left-0 top-full mt-1 w-56 bg-card border border-border rounded-lg shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50 max-h-80 overflow-y-auto">
								<div class="py-1">
									{#if $projects.length === 0}
										<p class="px-3 py-2 text-sm text-muted-foreground">No projects yet</p>
									{:else}
										<!-- Grouped projects -->
										{#each headerProjectsOrganized.groupOrder as group}
											{@const groupProjects = headerProjectsOrganized.grouped.get(group.name) || []}
											{#if groupProjects.length > 0}
												<div class="py-1">
													<button
														class="w-full flex items-center gap-2 px-3 py-1 text-xs font-medium text-muted-foreground hover:text-foreground uppercase tracking-wide bg-muted/30 transition-colors"
														on:click|stopPropagation={() => groups.toggleGroupCollapsed('projects', group.name)}
													>
														<svg class="w-3 h-3 transition-transform {group.collapsed ? '-rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
														</svg>
														<span>{group.name}</span>
														<span class="text-muted-foreground/60 ml-auto">({groupProjects.length})</span>
													</button>
													{#if !group.collapsed}
														{#each groupProjects as project}
															<button
																on:click={() => setTabProject(tabId, project.id)}
																class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors {currentTab.project === project.id ? 'text-primary bg-accent/50' : 'text-foreground'}"
															>
																{project.name}
															</button>
														{/each}
													{/if}
												</div>
											{/if}
										{/each}
										<!-- Ungrouped projects -->
										{#if headerProjectsOrganized.ungrouped.length > 0}
											{#if hasGroups}
												<div class="py-1">
													<div class="px-3 py-1 text-xs font-medium text-muted-foreground uppercase tracking-wide bg-muted/30">
														Other
													</div>
												</div>
											{/if}
											{#each headerProjectsOrganized.ungrouped as project}
												<button
													on:click={() => setTabProject(tabId, project.id)}
													class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors {currentTab.project === project.id ? 'text-primary bg-accent/50' : 'text-foreground'}"
												>
													{project.name}
												</button>
											{/each}
										{/if}
									{/if}
									<div class="border-t border-border my-1"></div>
									<button
										on:click={() => (showProjectModal = true)}
										class="w-full px-3 py-2 text-left text-sm text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
									>
										{$projects.length === 0 ? 'Create Project...' : 'Manage Projects...'}
									</button>
								</div>
							</div>
						{/if}
					</div>
				{/if}

				<!-- Rewind Button (after breadcrumb, only when session is active) -->
				{#if currentTab.sessionId}
					<button
						on:click={() => openRewindModal(tabId)}
						class="flex items-center gap-1 px-2 py-1 text-xs text-muted-foreground hover:text-foreground hover:bg-accent rounded-md transition-colors disabled:opacity-50 ml-2"
						title="Rewind conversation"
						disabled={currentTab.isStreaming}
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12.066 11.2a1 1 0 000 1.6l5.334 4A1 1 0 0019 16V8a1 1 0 00-1.6-.8l-5.333 4zM4.066 11.2a1 1 0 000 1.6l5.334 4A1 1 0 0011 16V8a1 1 0 00-1.6-.8l-5.334 4z" />
						</svg>
						<span class="hidden sm:inline">Rewind</span>
					</button>
				{/if}

				<!-- Spacer -->
				<div class="flex-1"></div>

				<!-- Right side: Token counts, connection status -->
				<div class="flex items-center gap-2 sm:gap-3">
					<!-- Context usage dropdown (only show if any tokens > 0) -->
					{#if currentTab.totalTokensIn > 0 || currentTab.totalTokensOut > 0 || currentTab.totalCacheCreationTokens > 0 || currentTab.totalCacheReadTokens > 0}
						{@const autocompactBuffer = 45000}
						{@const contextUsed = (currentTab.contextUsed ?? (currentTab.totalTokensIn + currentTab.totalCacheCreationTokens + currentTab.totalCacheReadTokens)) + autocompactBuffer}
						{@const contextMax = 200000}
						{@const contextPercent = Math.min((contextUsed / contextMax) * 100, 100)}
						<div class="relative group">
							<button
								class="flex items-center gap-1.5 px-2 py-1 text-xs text-muted-foreground hover:text-foreground hover:bg-accent rounded-md transition-colors"
								title="Context usage: {formatTokenCount(contextUsed)} / {formatTokenCount(contextMax)}"
							>
								<!-- Circular progress indicator -->
								<svg class="w-4 h-4 -rotate-90" viewBox="0 0 20 20">
									<circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" stroke-width="2" opacity="0.2" />
									<circle
										cx="10" cy="10" r="8" fill="none"
										stroke={contextPercent > 80 ? '#ef4444' : contextPercent > 60 ? '#f59e0b' : '#22c55e'}
										stroke-width="2"
										stroke-dasharray={2 * Math.PI * 8}
										stroke-dashoffset={2 * Math.PI * 8 * (1 - contextPercent / 100)}
										stroke-linecap="round"
									/>
								</svg>
								<span>{Math.round(contextPercent)}%</span>
								<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>
							<!-- Token dropdown -->
							<div class="absolute right-0 top-full mt-1 w-52 bg-card border border-border rounded-lg shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50">
								<div class="py-2 px-3 space-y-2">
									<!-- Context header -->
									<div class="flex items-center justify-between text-xs pb-1 border-b border-border">
										<span class="text-muted-foreground">Context</span>
										<span class="text-foreground font-medium">{formatTokenCount(contextUsed)} / {formatTokenCount(contextMax)}</span>
									</div>
									<div class="flex items-center justify-between text-xs">
										<span class="flex items-center gap-1.5 text-muted-foreground">
											<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16V4m0 0L3 8m4-4l4 4" />
											</svg>
											Input
										</span>
										<span class="text-foreground font-medium">{formatTokenCount(currentTab.totalTokensIn)}</span>
									</div>
									<div class="flex items-center justify-between text-xs">
										<span class="flex items-center gap-1.5 text-muted-foreground">
											<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 8v12m0 0l4-4m-4 4l-4-4" />
											</svg>
											Output
										</span>
										<span class="text-foreground font-medium">{formatTokenCount(currentTab.totalTokensOut)}</span>
									</div>
									{#if currentTab.totalCacheCreationTokens > 0}
										<div class="flex items-center justify-between text-xs">
											<span class="flex items-center gap-1.5 text-muted-foreground">
												<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
												</svg>
												Cache Creation
											</span>
											<span class="text-foreground font-medium">{formatTokenCount(currentTab.totalCacheCreationTokens)}</span>
										</div>
									{/if}
									{#if currentTab.totalCacheReadTokens > 0}
										<div class="flex items-center justify-between text-xs">
											<span class="flex items-center gap-1.5 text-blue-400">
												<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
												</svg>
												Cache Read
											</span>
											<span class="text-blue-400 font-medium">{formatTokenCount(currentTab.totalCacheReadTokens)}</span>
										</div>
									{/if}
								</div>
							</div>
						</div>
					{/if}

					<!-- Connection Status (always far right) -->
					<ConnectionStatus wsConnected={currentTab.wsConnected} />
				</div>
			</div>
		{:else}
			<!-- Empty state context bar (no active tab) - just show mobile menu button -->
			<div class="bg-card border-b border-border h-12 flex items-center px-2 sm:px-4">
				<button class="lg:hidden p-2 mr-2 text-muted-foreground hover:text-foreground rounded-md hover:bg-accent transition-colors" on:click={() => (sidebarOpen = true)}>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
					</svg>
				</button>
			</div>
		{/if}

		<!-- Active Tab Content -->
		{#if $activeTab}
			{@const currentTab = $activeTab}
			{@const tabId = currentTab.id}

			<!-- Messages Area -->
			<div
				use:autoScroll={tabId}
				class="flex-1 overflow-y-auto"
			>
				{#if currentTab.messages.length === 0}
					<!-- Empty State -->
					<div class="h-full flex items-center justify-center">
						<div class="text-center max-w-md px-6">
							<div class="w-16 h-16 mx-auto mb-6 rounded-2xl bg-primary/10 flex items-center justify-center shadow-s">
								<svg class="w-8 h-8 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path
										stroke-linecap="round"
										stroke-linejoin="round"
										stroke-width="1.5"
										d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
									/>
								</svg>
							</div>
							<h2 class="text-xl font-semibold text-foreground mb-2">Start a Conversation</h2>
							<p class="text-muted-foreground mb-4">Ask Claude anything - code, questions, ideas, or just chat.</p>
							<div class="text-sm text-muted-foreground">
								<span>Profile:</span>
								<span class="text-foreground ml-1">{$profiles.find((p) => p.id === currentTab.profile)?.name || currentTab.profile}</span>
							</div>
						</div>
					</div>
				{:else}
					<!-- Messages -->
					<div class="max-w-5xl mx-auto px-4 sm:px-8 py-4 space-y-4">
						{#each currentTab.messages as message}
							{#if message.role === 'user'}
								<!-- User Message - Anvil Style -->
								<div class="flex gap-3 w-full">
									<!-- Avatar -->
									<div class="flex-shrink-0 w-8 h-8 rounded-full bg-primary flex items-center justify-center shadow-s">
										<svg class="w-4 h-4 text-primary-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
										</svg>
									</div>
									<!-- Content -->
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2 mb-1">
											<span class="font-semibold text-sm text-foreground">You</span>
											<span class="text-xs text-muted-foreground">{formatTime()}</span>
										</div>
										<div class="bg-card border border-border rounded-lg p-4 shadow-s overflow-hidden">
											<p class="whitespace-pre-wrap break-words overflow-wrap-anywhere text-foreground">{message.content}</p>
										</div>
									</div>
								</div>
							{:else if message.type === 'text' || !message.type}
								<!-- Assistant Message - Anvil Style -->
								{@const currentProfile = $profiles.find(p => p.id === currentTab.profile)}
								{@const hasPartialMessages = currentProfile?.config?.include_partial_messages !== false}
								<div class="flex gap-3 w-full">
									<!-- Avatar -->
									<div class="flex-shrink-0 w-8 h-8 rounded-full bg-accent flex items-center justify-center shadow-s">
										<svg class="w-4 h-4 text-accent-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
										</svg>
									</div>
									<!-- Content -->
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2 mb-1">
											<span class="font-semibold text-sm text-foreground">Claude</span>
											<span class="text-xs text-muted-foreground">{formatTime()}</span>
											{#if message.streaming}
												<span class="flex gap-0.5">
													<span class="w-1.5 h-1.5 bg-primary rounded-full animate-bounce" style="animation-delay: 0ms"></span>
													<span class="w-1.5 h-1.5 bg-primary rounded-full animate-bounce" style="animation-delay: 150ms"></span>
													<span class="w-1.5 h-1.5 bg-primary rounded-full animate-bounce" style="animation-delay: 300ms"></span>
												</span>
											{/if}
											{#if message.content && !message.streaming}
												<!-- Action buttons container -->
												<div class="ml-auto flex items-center gap-1">
													<!-- Download and Copy URL buttons for generated images -->
													{#if extractGeneratedImageUrl(message.content)}
														{@const imageUrl = extractGeneratedImageUrl(message.content)}
														{@const filename = getFilenameFromUrl(imageUrl || '')}
														<a
															href={imageUrl}
															download={filename}
															class="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-muted-foreground hover:bg-muted/50 hover:text-foreground transition-colors no-underline"
															title="Download image"
														>
															<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
															</svg>
															<span>Download</span>
														</a>
														<button
															class="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-muted-foreground hover:bg-muted/50 hover:text-foreground transition-colors"
															on:click={() => copyUrlToClipboard(imageUrl || '', message.id)}
															title="Copy image URL"
														>
															{#if copiedUrlMessageId === message.id}
																<svg class="w-3.5 h-3.5 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																	<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
																</svg>
																<span class="text-green-500">Copied</span>
															{:else}
																<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																	<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
																</svg>
																<span>Copy URL</span>
															{/if}
														</button>
														<span class="w-px h-4 bg-border"></span>
													{/if}
													<!-- Copy response button -->
													<button
														class="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-muted-foreground hover:bg-muted/50 hover:text-foreground transition-colors"
														on:click={() => copyToClipboard(message.content, message.id)}
														title="Copy response"
													>
														{#if copiedMessageId === message.id}
															<svg class="w-3.5 h-3.5 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
															</svg>
															<span class="text-green-500">Copied</span>
														{:else}
															<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
															</svg>
															<span>Copy</span>
														{/if}
													</button>
												</div>
											{/if}
										</div>
										<div class="bg-card border border-border rounded-lg p-4 shadow-s overflow-hidden">
											<div class="prose prose-sm max-w-none break-words overflow-x-auto">
												{#if message.content}
													{@html renderMarkdown(message.content)}
													{#if message.streaming}
														<span class="inline-block w-2 h-4 ml-0.5 bg-primary animate-pulse"></span>
													{/if}
												{:else if message.streaming && !hasPartialMessages}
													<!-- Only show placeholder dots when partial messages is disabled -->
													<div class="flex gap-1 py-2">
														<span class="w-2 h-2 bg-muted-foreground rounded-full animate-pulse"></span>
														<span class="w-2 h-2 bg-muted-foreground rounded-full animate-pulse" style="animation-delay: 200ms"></span>
														<span class="w-2 h-2 bg-muted-foreground rounded-full animate-pulse" style="animation-delay: 400ms"></span>
													</div>
												{/if}
											</div>
										</div>
										{#if message.metadata && !message.streaming}
											<div class="mt-2 text-xs text-muted-foreground flex items-center gap-3">
												{#if message.metadata.total_cost_usd}
													<span>{formatCost(message.metadata.total_cost_usd as number)}</span>
												{/if}
												{#if message.metadata.duration_ms}
													<span>{((message.metadata.duration_ms as number) / 1000).toFixed(1)}s</span>
												{/if}
											</div>
										{/if}
									</div>
								</div>
							{:else if message.type === 'tool_use' && message.toolName === 'TodoWrite'}
								<!-- TodoWrite - Render as TodoList component -->
								{#if message.toolInput?.todos && Array.isArray(message.toolInput.todos)}
									<div class="flex gap-3 w-full">
										<!-- Empty spacer to align with message content -->
										<div class="flex-shrink-0 w-8"></div>
										<div class="flex-1 min-w-0">
											<TodoList todos={message.toolInput.todos} />
										</div>
									</div>
								{/if}
							{:else if message.type === 'tool_use'}
								<!-- Tool Use with grouped Result - Expandable Card -->
								<div class="flex gap-3 w-full">
									<div class="flex-shrink-0 w-8 h-8 rounded-full bg-yellow-500/10 flex items-center justify-center shadow-s">
										<svg class="w-4 h-4 text-yellow-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
										</svg>
									</div>
									<div class="flex-1 min-w-0">
										<details class="w-full border border-border rounded-lg overflow-hidden shadow-s group">
											<summary class="w-full px-4 py-2 bg-muted/30 hover:bg-muted/50 flex items-center gap-2 cursor-pointer list-none transition-colors">
												{#if message.toolStatus === 'running' || message.streaming}
													<svg class="w-4 h-4 text-primary animate-spin flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
													</svg>
												{:else if message.toolStatus === 'error'}
													<svg class="w-4 h-4 text-red-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
													</svg>
												{:else}
													<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
													</svg>
												{/if}
												<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
												</svg>
												<span class="text-sm font-medium text-foreground">{message.toolName}</span>
												<span class="text-muted-foreground">•</span>
												{#if message.toolStatus === 'running' || message.streaming}
													<span class="text-primary text-sm">Executing...</span>
												{:else if message.toolStatus === 'error'}
													<span class="text-red-500 text-sm">Error</span>
												{:else}
													<span class="text-green-500 text-sm">Complete</span>
												{/if}
												<svg class="w-4 h-4 text-muted-foreground ml-auto transition-transform group-open:rotate-180 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
												</svg>
											</summary>
											<div class="bg-card border-t border-border">
												{#if message.partialToolInput || message.toolInput}
													<div class="px-4 py-3 border-b border-border/50">
														<div class="text-xs text-muted-foreground mb-1 font-medium">Input</div>
														{#if message.streaming && message.partialToolInput}
															<!-- Show raw streaming input with cursor -->
															<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{message.partialToolInput}<span class="inline-block w-1.5 h-3 ml-0.5 bg-primary animate-pulse"></span></pre>
														{:else if message.toolInput && Object.keys(message.toolInput).length > 0}
															<!-- Show formatted JSON when complete -->
															<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{JSON.stringify(message.toolInput, null, 2)}</pre>
														{:else if message.partialToolInput}
															<!-- Fallback: show partial input if toolInput is empty but we have partial -->
															<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{message.partialToolInput}</pre>
														{/if}
													</div>
												{/if}
												{#if message.toolResult}
													<div class="px-4 py-3">
														<div class="text-xs text-muted-foreground mb-1 font-medium">Result</div>
														{#if toolResultHasImage(message.toolResult)}
															<!-- Show compact message for images - full image displayed outside tool group -->
															<span class="text-xs text-green-500">✓ Image generated successfully (see below)</span>
														{:else}
															<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{message.toolResult}</pre>
														{/if}
													</div>
												{/if}
											</div>
										</details>
									</div>
								</div>
							{:else if message.type === 'tool_result'}
								<!-- Standalone Tool Result (fallback for ungrouped results) -->
								<div class="flex gap-3 w-full">
									<div class="flex-shrink-0 w-8 h-8 rounded-full bg-blue-500/10 flex items-center justify-center shadow-s">
										<svg class="w-4 h-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
										</svg>
									</div>
									<div class="flex-1 min-w-0">
										<details class="w-full border border-border rounded-lg overflow-hidden shadow-s group">
											<summary class="w-full px-4 py-2 bg-muted/30 hover:bg-muted/50 flex items-center gap-2 cursor-pointer list-none transition-colors">
												<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
												</svg>
												<svg class="w-4 h-4 text-blue-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
												</svg>
												<span class="text-sm font-medium text-foreground">Tool Result</span>
												<span class="text-muted-foreground">•</span>
												<span class="text-xs font-mono text-muted-foreground truncate max-w-[200px]">{message.toolId}</span>
												<span class="text-muted-foreground">•</span>
												<span class="text-green-500 text-sm">Success</span>
												<svg class="w-4 h-4 text-muted-foreground ml-auto transition-transform group-open:rotate-180 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
												</svg>
											</summary>
											<div class="px-4 py-3 bg-card border-t border-border">
												{#if toolResultHasImage(message.content)}
													<!-- Show compact message for images - full image displayed outside tool group -->
													<span class="text-xs text-green-500">✓ Image generated successfully (see below)</span>
												{:else}
													<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{message.content}</pre>
												{/if}
											</div>
										</details>
									</div>
								</div>
							{:else if message.type === 'system'}
								<!-- System Message (e.g., /context output, status updates) -->
								<div class="flex gap-3 w-full">
									<div class="flex-shrink-0 w-8 h-8 rounded-full bg-purple-500/10 flex items-center justify-center shadow-s">
										<svg class="w-4 h-4 text-purple-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
										</svg>
									</div>
									<div class="flex-1 min-w-0">
										<SystemMessage {message} />
									</div>
								</div>
							{:else if message.type === 'subagent'}
								<!-- Subagent Message - Collapsible container for all subagent work -->
								<div class="flex gap-3 w-full">
									<div class="flex-shrink-0 w-8 h-8 rounded-full bg-indigo-500/10 flex items-center justify-center shadow-s">
										<svg class="w-4 h-4 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
										</svg>
									</div>
									<div class="flex-1 min-w-0">
										<SubagentMessage {message} />
									</div>
								</div>
							{/if}
						{/each}

						<!-- Error -->
						{#if currentTab.error}
							<div class="bg-destructive/10 border border-destructive/30 text-destructive px-4 py-3 rounded-lg flex items-center justify-between shadow-s">
								<span class="text-sm">{currentTab.error}</span>
								<button on:click={() => tabs.clearTabError(tabId)} class="text-destructive hover:opacity-80">&times;</button>
							</div>
						{/if}
					</div>
				{/if}
			</div>

			<!-- Permission Queue (shown when there are pending permissions) -->
			{#if currentTab.pendingPermissions && currentTab.pendingPermissions.length > 0}
				<div class="border-t border-amber-500/30 bg-amber-900/5 p-3 sm:p-4">
					<div class="max-w-5xl mx-auto">
						<PermissionQueue
							requests={currentTab.pendingPermissions}
							on:respond={(e) => handlePermissionRespond(tabId, e)}
						/>
					</div>
				</div>
			{/if}

			<!-- User Question Queue (shown when there are pending questions from AskUserQuestion tool) -->
			{#if currentTab.pendingQuestions && currentTab.pendingQuestions.length > 0}
				<div class="border-t border-blue-500/30 bg-blue-900/5 p-3 sm:p-4">
					<div class="max-w-5xl mx-auto">
						{#each currentTab.pendingQuestions as question (question.request_id)}
							<UserQuestion
								data={question}
								on:respond={(e) => handleUserQuestionRespond(tabId, e)}
							/>
						{/each}
					</div>
				</div>
			{/if}

			<!-- Input Area -->
			<div class="border-t border-border/50 bg-background/80 backdrop-blur-sm p-3 sm:p-4">
				<div class="max-w-5xl mx-auto">
					<!-- Hidden file input -->
					<input type="file" bind:this={fileInput} on:change={handleFileUpload} class="hidden" multiple accept="*/*" />

					<!-- Cursor-Style Unified Input Container -->
					<!-- Recording Error Message -->
						{#if recordingError}
							<div class="mb-2 px-3 py-2 bg-destructive/10 border border-destructive/30 rounded-lg text-sm text-destructive flex items-center justify-between">
								<span>{recordingError}</span>
								<button type="button" on:click={() => recordingError = ''} class="ml-2 hover:opacity-70">&times;</button>
							</div>
						{/if}

						<form on:submit|preventDefault={() => handleSubmit(tabId)} class="relative">
						<div class="bg-card/80 backdrop-blur border border-border/60 rounded-2xl shadow-lg transition-all duration-200 focus-within:border-primary/50 focus-within:shadow-primary/5 focus-within:shadow-xl">

							<!-- Uploaded Files (inside container) -->
							{#if (tabUploadedFiles[tabId] || []).length > 0}
								<div class="px-3 pt-3 pb-0 flex flex-wrap gap-1.5">
									{#each tabUploadedFiles[tabId] as file, index}
										<div class="flex items-center gap-1.5 bg-accent/50 text-xs px-2 py-1 rounded-lg group">
											<svg class="w-3.5 h-3.5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
											</svg>
											<span class="text-foreground truncate max-w-[100px]" title={file.path}>{file.filename}</span>
											<button
												type="button"
												on:click={() => removeUploadedFile(tabId, index)}
												class="text-muted-foreground hover:text-destructive opacity-60 group-hover:opacity-100 transition-opacity"
											>
												<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
												</svg>
											</button>
										</div>
									{/each}
								</div>
							{/if}

							<!-- Main Input Row -->
							<div class="flex items-center gap-1 p-2 sm:p-2.5">
								<!-- Textarea Container -->
								<div class="flex-1 relative min-w-0">
									<!-- Command Autocomplete -->
									<CommandAutocomplete
										bind:this={commandAutocompleteRefs[tabId]}
										inputValue={tabInputs[tabId] || ''}
										projectId={currentTab.project}
										visible={showCommandAutocomplete[tabId] || false}
										onSelect={(cmd) => handleCommandSelect(tabId, cmd)}
										onClose={() => {
											showCommandAutocomplete[tabId] = false;
											showCommandAutocomplete = showCommandAutocomplete;
										}}
									/>

									<!-- File Autocomplete (@ mentions) -->
									<FileAutocomplete
										bind:this={fileAutocompleteRefs[tabId]}
										inputValue={tabInputs[tabId] || ''}
										projectId={currentTab.project}
										visible={showFileAutocomplete[tabId] || false}
										onSelect={(file) => handleFileSelect(tabId, file)}
										onClose={() => {
											showFileAutocomplete[tabId] = false;
											showFileAutocomplete = showFileAutocomplete;
										}}
									/>

									<textarea
										bind:this={textareas[tabId]}
										bind:value={tabInputs[tabId]}
										on:input={() => handleInputChange(tabId)}
										on:keydown={(e) => handleKeyDown(e, tabId)}
										placeholder={currentTab.isStreaming ? "Queue a message while Claude works..." : "Message Claude... (/ commands, @ files)"}
										class="w-full bg-transparent border-0 px-2 sm:px-3 py-2 text-foreground placeholder-muted-foreground/60 resize-none focus:outline-none focus:ring-0 min-h-[44px] max-h-[200px] leading-relaxed text-sm sm:text-base"
										rows="1"
										disabled={!$claudeAuthenticated}
									></textarea>
								</div>

								<!-- Voice/Send/Stop/Queue Buttons -->
								<div class="flex items-center gap-1">
									<!-- Voice Recording Button - always available -->
									<button
										type="button"
										on:click={toggleRecording}
										disabled={!$claudeAuthenticated || isUploading || isTranscribing}
										class="w-9 h-9 sm:w-8 sm:h-8 flex items-center justify-center rounded-xl transition-all disabled:opacity-30 disabled:cursor-not-allowed {isRecording ? 'bg-destructive/20 text-destructive animate-pulse' : isTranscribing ? 'bg-primary/15 text-primary' : 'hover:bg-accent text-muted-foreground hover:text-foreground'}"
										title={isRecording ? 'Stop recording' : isTranscribing ? 'Transcribing...' : 'Voice input'}
									>
										{#if isTranscribing}
											<svg class="w-4 h-4 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
											</svg>
										{:else}
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
											</svg>
										{/if}
									</button>
									{#if currentTab.isStreaming}
										<!-- When streaming: show queue button if text entered, always show stop button -->
										{#if (tabInputs[tabId] || '').trim()}
											<button
												type="submit"
												class="w-9 h-9 sm:w-8 sm:h-8 flex items-center justify-center bg-amber-500/15 text-amber-600 hover:bg-amber-500/25 rounded-xl transition-all"
												title="Queue message (will be sent when Claude finishes)"
											>
												<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
												</svg>
											</button>
										{/if}
										<button
											type="button"
											on:click={() => tabs.stopGeneration(tabId)}
											class="w-9 h-9 sm:w-8 sm:h-8 flex items-center justify-center bg-destructive/15 text-destructive hover:bg-destructive/25 rounded-xl transition-all"
											title="Stop generating"
										>
											<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
												<rect x="6" y="6" width="12" height="12" rx="2" />
											</svg>
										</button>
									{:else}
										<!-- Send Button -->
										<button
											type="submit"
											class="w-9 h-9 sm:w-8 sm:h-8 flex items-center justify-center bg-primary hover:bg-primary/90 text-primary-foreground rounded-xl transition-all disabled:opacity-30 disabled:cursor-not-allowed"
											disabled={!(tabInputs[tabId] || '').trim() || !$claudeAuthenticated || isUploading || isRecording || isTranscribing}
											title={isUploading ? "Uploading files..." : isRecording ? "Recording..." : isTranscribing ? "Transcribing..." : "Send message"}
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 12h14M12 5l7 7-7 7" />
											</svg>
										</button>
									{/if}
								</div>
							</div>
						</div>

						<!-- Bottom Controls Row (Pills + Attach) -->
						<div class="mt-2 flex flex-wrap items-center justify-center gap-1.5">
							<!-- Model/Mode Pills (Admin only) -->
							{#if currentTab && $isAdmin}
								{@const currentProfile = $profiles.find(p => p.id === currentTab.profile)}
								{@const profileModel = currentProfile?.config?.model || 'sonnet'}
								{@const profilePermissionMode = currentProfile?.config?.permission_mode || 'default'}
								{@const effectiveModel = currentTab.modelOverride || profileModel}
								{@const effectiveMode = currentTab.permissionModeOverride || profilePermissionMode}
								{@const modelLabels = { sonnet: 'Sonnet', opus: 'Opus', haiku: 'Haiku' } as Record<string, string>}
								{@const modeLabels = { default: 'Ask', acceptEdits: 'Auto-Accept', plan: 'Plan', bypassPermissions: 'Bypass' } as Record<string, string>}
								<!-- Model Selector Pill -->
								<div class="relative">
									<button
										type="button"
										on:click={() => { showModelPopup = !showModelPopup; showModePopup = false; }}
										class="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all disabled:opacity-40 {currentTab.modelOverride ? 'bg-primary/15 text-primary border border-primary/30' : 'bg-accent/50 text-muted-foreground hover:text-foreground border border-transparent hover:border-border/50'}"
										disabled={currentTab.isStreaming}
									>
										<span>{modelLabels[effectiveModel] || effectiveModel}</span>
										<svg class="w-2.5 h-2.5 opacity-60 transition-transform {showModelPopup ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7" />
										</svg>
									</button>
									{#if showModelPopup}
										<div class="fixed inset-0 z-40" on:click={() => showModelPopup = false}></div>
										<div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 bg-card border border-border rounded-xl shadow-xl overflow-hidden z-50 min-w-[90px]">
											{#each [['sonnet', 'Sonnet'], ['opus', 'Opus'], ['haiku', 'Haiku']] as [value, label]}
												<button
													type="button"
													on:click={() => {
														if (value === profileModel) {
															tabs.setTabModelOverride(tabId, null);
														} else {
															tabs.setTabModelOverride(tabId, value);
														}
														showModelPopup = false;
													}}
													class="w-full px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors flex items-center justify-between gap-3 {effectiveModel === value ? 'bg-accent/50 text-foreground font-medium' : 'text-muted-foreground'}"
												>
													<span>{label}</span>
													{#if effectiveModel === value}
														<svg class="w-3 h-3 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
														</svg>
													{/if}
												</button>
											{/each}
										</div>
									{/if}
								</div>

								<!-- Permission Mode Selector Pill -->
								<div class="relative">
									<button
										type="button"
										on:click={() => { showModePopup = !showModePopup; showModelPopup = false; }}
										class="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all disabled:opacity-40 {currentTab.permissionModeOverride ? 'bg-primary/15 text-primary border border-primary/30' : 'bg-accent/50 text-muted-foreground hover:text-foreground border border-transparent hover:border-border/50'}"
										disabled={currentTab.isStreaming}
									>
										<span>{modeLabels[effectiveMode] || effectiveMode}</span>
										<svg class="w-2.5 h-2.5 opacity-60 transition-transform {showModePopup ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7" />
										</svg>
									</button>
									{#if showModePopup}
										<div class="fixed inset-0 z-40" on:click={() => showModePopup = false}></div>
										<div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 bg-card border border-border rounded-xl shadow-xl overflow-hidden z-50 min-w-[100px]">
											{#each [['default', 'Ask'], ['acceptEdits', 'Auto-Accept'], ['plan', 'Plan'], ['bypassPermissions', 'Bypass']] as [value, label]}
												<button
													type="button"
													on:click={() => {
														if (value === profilePermissionMode) {
															tabs.setTabPermissionModeOverride(tabId, null);
														} else {
															tabs.setTabPermissionModeOverride(tabId, value);
														}
														showModePopup = false;
													}}
													class="w-full px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors flex items-center justify-between gap-3 {effectiveMode === value ? 'bg-accent/50 text-foreground font-medium' : 'text-muted-foreground'}"
												>
													<span>{label}</span>
													{#if effectiveMode === value}
														<svg class="w-3 h-3 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
														</svg>
													{/if}
												</button>
											{/each}
										</div>
									{/if}
								</div>
							{/if}

							<!-- Attach File Button -->
							<button
								type="button"
								on:click={triggerFileUpload}
								class="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium rounded-full bg-accent/50 text-muted-foreground hover:text-foreground border border-transparent hover:border-border/50 transition-all disabled:opacity-40"
								disabled={currentTab.isStreaming || !$claudeAuthenticated || isUploading}
								title={currentTab.project ? 'Attach file' : 'Select a project to attach files'}
							>
								{#if isUploading}
									<svg class="w-3 h-3 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
										<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
									</svg>
								{:else}
									<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
									</svg>
								{/if}
								<span>Attach</span>
							</button>

							<!-- Reset Button (Admin only) -->
							{#if currentTab && $isAdmin && (currentTab.modelOverride || currentTab.permissionModeOverride)}
								<button
									type="button"
									on:click={() => {
										tabs.setTabModelOverride(tabId, null);
										tabs.setTabPermissionModeOverride(tabId, null);
									}}
									class="inline-flex items-center gap-1 px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground rounded-full hover:bg-accent/50 transition-all disabled:opacity-40"
									title="Reset to defaults"
									disabled={currentTab.isStreaming}
								>
									<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
									</svg>
									<span class="hidden sm:inline">Reset</span>
								</button>
							{/if}
						</div>
					</form>
				</div>
			</div>
		{:else}
			<!-- Empty state when no tabs are open -->
			<div class="flex-1 flex flex-col items-center justify-center p-8 text-center">
				<div class="max-w-md space-y-6">
					<div class="w-16 h-16 mx-auto rounded-2xl bg-gradient-to-br from-primary/20 to-primary/10 flex items-center justify-center">
						<svg class="w-8 h-8 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
						</svg>
					</div>

					<div class="space-y-2">
						<h2 class="text-xl font-semibold text-foreground">No open chats</h2>
						<p class="text-sm text-muted-foreground">
							Start a new conversation or open one from your history
						</p>
					</div>

					<div class="flex flex-col sm:flex-row gap-3 justify-center">
						<button
							on:click={handleNewTab}
							class="inline-flex items-center justify-center gap-2 px-4 py-2.5 bg-primary hover:bg-primary/90 text-primary-foreground rounded-lg font-medium transition-colors"
						>
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
							</svg>
							New Chat
						</button>
						<button
							on:click={() => (sidebarOpen = true)}
							class="lg:hidden inline-flex items-center justify-center gap-2 px-4 py-2.5 bg-accent hover:bg-accent/80 text-foreground rounded-lg font-medium transition-colors"
						>
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
							</svg>
							View History
						</button>
					</div>
				</div>
			</div>
		{/if}
	</main>
</div>

<!-- Spotlight Search (Cmd+K) -->
<SpotlightSearch
	visible={showSpotlight}
	sessions={$sessions}
	currentProjectId={$activeTab?.project}
	onClose={() => showSpotlight = false}
	onSelectSession={handleSpotlightSelectSession}
	onSelectCommand={handleSpotlightSelectCommand}
	onNewChat={handleSpotlightNewChat}
	onOpenSettings={handleSpotlightOpenSettings}
/>

<!-- Subagent Manager Panel -->
{#if showSubagentManager}
	<SubagentManager onClose={() => showSubagentManager = false} onUpdate={() => loadSubagents()} />
{/if}

<!-- Profile Modal -->
{#if showProfileModal}
	<div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" on:click={() => (showProfileModal = false)}>
		<div class="bg-card rounded-xl w-full max-w-lg max-h-[80vh] overflow-y-auto shadow-l border border-border" on:click|stopPropagation>
			<div class="p-4 border-b border-border flex items-center justify-between">
				<h2 class="text-lg font-semibold text-foreground">
					{showNewProfileForm ? (editingProfile ? 'Edit Profile' : 'New Profile') : 'Profiles'}
				</h2>
				<button
					class="text-muted-foreground hover:text-foreground"
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
								<label class="block text-xs text-muted-foreground mb-1">ID</label>
								<input
									bind:value={profileForm.id}
									disabled={!!editingProfile}
									class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground disabled:opacity-50"
									placeholder="my-profile"
								/>
							</div>
							<div>
								<label class="block text-xs text-muted-foreground mb-1">Name</label>
								<input bind:value={profileForm.name} class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground" placeholder="My Profile" />
							</div>
						</div>

						<div>
							<label class="block text-xs text-muted-foreground mb-1">Description</label>
							<input bind:value={profileForm.description} class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground" placeholder="Optional" />
						</div>

						<div class="grid grid-cols-3 gap-4">
							<div>
								<label class="block text-xs text-muted-foreground mb-1">Model</label>
								<select bind:value={profileForm.model} class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground">
									<option value="sonnet">Sonnet</option>
									<option value="opus">Opus</option>
									<option value="haiku">Haiku</option>
								</select>
							</div>
							<div>
								<label class="block text-xs text-muted-foreground mb-1">Permission</label>
								<select bind:value={profileForm.permission_mode} class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground">
									<option value="default">Default</option>
									<option value="acceptEdits">Accept Edits</option>
									<option value="plan">Plan</option>
									<option value="bypassPermissions">Bypass</option>
								</select>
							</div>
							<div>
								<label class="block text-xs text-muted-foreground mb-1">Max Turns</label>
								<input type="number" bind:value={profileForm.max_turns} class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground" placeholder="Unlimited" />
							</div>
						</div>

						<!-- Tool Configuration Accordion -->
						<div class="border border-border rounded-lg overflow-hidden">
							<button
								type="button"
								on:click={() => toggleSection('toolConfig')}
								class="w-full px-3 py-2 bg-accent flex items-center justify-between text-sm text-foreground hover:bg-muted"
							>
								<span>Tool Configuration</span>
								<svg class="w-4 h-4 transition-transform {expandedSections.toolConfig ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>
							{#if expandedSections.toolConfig}
								<div class="p-3 space-y-3 bg-card">
									<!-- Tool selection mode -->
									<div>
										<label class="block text-xs text-muted-foreground mb-2">Tool Access Mode</label>
										<div class="flex gap-2">
											<button
												type="button"
												on:click={() => { toolSelectionMode = 'all'; profileForm.allowed_tools = []; profileForm.disallowed_tools = []; }}
												class="px-3 py-1.5 text-xs rounded-lg transition-colors {toolSelectionMode === 'all' ? 'bg-violet-600 text-white' : 'bg-muted text-foreground hover:bg-muted/80'}"
											>
												All Tools
											</button>
											<button
												type="button"
												on:click={() => { toolSelectionMode = 'allow'; profileForm.disallowed_tools = []; }}
												class="px-3 py-1.5 text-xs rounded-lg transition-colors {toolSelectionMode === 'allow' ? 'bg-violet-600 text-white' : 'bg-muted text-foreground hover:bg-muted/80'}"
											>
												Allow Only
											</button>
											<button
												type="button"
												on:click={() => { toolSelectionMode = 'disallow'; profileForm.allowed_tools = []; }}
												class="px-3 py-1.5 text-xs rounded-lg transition-colors {toolSelectionMode === 'disallow' ? 'bg-violet-600 text-white' : 'bg-muted text-foreground hover:bg-muted/80'}"
											>
												Disallow
											</button>
										</div>
										<p class="text-xs text-muted-foreground mt-1">
											{#if toolSelectionMode === 'all'}
												Agent can use all available tools.
											{:else if toolSelectionMode === 'allow'}
												Agent can ONLY use checked tools.
											{:else}
												Agent can use all tools EXCEPT checked ones.
											{/if}
										</p>
									</div>

									<!-- Tool categories and individual tools -->
									{#if toolSelectionMode !== 'all'}
										<div class="border-t border-border pt-3 space-y-2">
											{#each availableTools.categories as category}
												{#if category.tools.length > 0}
													{@const categoryToolNames = category.tools.map(t => t.name)}
													{@const selectedInCategory = toolSelectionMode === 'allow'
														? categoryToolNames.filter(name => profileForm.allowed_tools.includes(name)).length
														: categoryToolNames.filter(name => profileForm.disallowed_tools.includes(name)).length}
													{@const isFullySelected = selectedInCategory === categoryToolNames.length}
													{@const isPartiallySelected = selectedInCategory > 0 && selectedInCategory < categoryToolNames.length}
													<div class="border border-border rounded-lg overflow-hidden">
														<!-- Category header -->
														<div class="w-full px-3 py-2 bg-muted/50 flex items-center gap-2 text-sm text-foreground">
															<!-- Custom checkbox with indeterminate state -->
															<button
																type="button"
																on:click|stopPropagation={() => toggleCategory(category)}
																class="w-4 h-4 rounded flex items-center justify-center transition-colors {isFullySelected ? 'bg-violet-600' : isPartiallySelected ? 'bg-violet-600' : 'bg-muted border border-border'}"
															>
																{#if isFullySelected}
																	<!-- Checkmark icon -->
																	<svg class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																		<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
																	</svg>
																{:else if isPartiallySelected}
																	<!-- Minus/dash icon for partial selection -->
																	<svg class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																		<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 12h14" />
																	</svg>
																{/if}
															</button>
															<button
																type="button"
																on:click={() => toggleToolCategoryExpansion(category.id)}
																class="flex-1 flex items-center gap-2 text-left hover:text-foreground/80"
															>
																<span class="flex-1">{category.name}</span>
																<span class="text-xs text-muted-foreground">({category.tools.length} tools)</span>
																<svg class="w-4 h-4 transition-transform {toolsExpanded[category.id] ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																	<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
																</svg>
															</button>
														</div>
														<!-- Individual tools -->
														{#if toolsExpanded[category.id]}
															<div class="p-2 space-y-1 bg-card">
																{#each category.tools as tool}
																	{@const isSelected = toolSelectionMode === 'allow'
																		? profileForm.allowed_tools.includes(tool.name)
																		: profileForm.disallowed_tools.includes(tool.name)}
																	<label class="flex items-center gap-2 px-2 py-1 rounded hover:bg-muted/50 cursor-pointer">
																		<input
																			type="checkbox"
																			checked={isSelected}
																			on:change={() => toggleTool(tool.name)}
																			class="w-4 h-4 rounded bg-muted border-0 text-violet-600 focus:ring-ring"
																		/>
																		<span class="text-sm text-foreground">{tool.name}</span>
																		<span class="text-xs text-muted-foreground">- {tool.description}</span>
																	</label>
																{/each}
															</div>
														{/if}
													</div>
												{/if}
											{/each}
										</div>

										<!-- Summary of selected tools -->
										<div class="text-xs text-muted-foreground pt-2 border-t border-border">
											{#if toolSelectionMode === 'allow'}
												{profileForm.allowed_tools.length} tool{profileForm.allowed_tools.length !== 1 ? 's' : ''} allowed
												{#if profileForm.allowed_tools.length > 0}
													<span class="text-foreground">: {profileForm.allowed_tools.join(', ')}</span>
												{/if}
											{:else if toolSelectionMode === 'disallow'}
												{profileForm.disallowed_tools.length} tool{profileForm.disallowed_tools.length !== 1 ? 's' : ''} blocked
												{#if profileForm.disallowed_tools.length > 0}
													<span class="text-foreground">: {profileForm.disallowed_tools.join(', ')}</span>
												{/if}
											{/if}
										</div>
									{/if}

									<!-- AI Tools Section -->
									<div class="border-t border-border pt-3 mt-3">
										<label class="block text-xs text-muted-foreground mb-2">AI Tools</label>
										<p class="text-xs text-muted-foreground mb-2">Enable AI-powered tools. Configure API keys in Settings → Integrations.</p>
										<div class="space-y-2">
											<label class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-muted/50 cursor-pointer border border-border">
												<input
													type="checkbox"
													bind:checked={profileForm.ai_tools_image_generation}
													class="w-4 h-4 rounded bg-muted border-0 text-violet-600 focus:ring-ring"
												/>
												<div class="flex-1">
													<span class="text-sm text-foreground">Image Generation</span>
													<span class="text-xs text-muted-foreground ml-1">(Nano Banana)</span>
												</div>
											</label>
											<!-- Add more AI tools here as they become available -->
										</div>
									</div>
								</div>
							{/if}
						</div>

						<!-- Behavior Settings Accordion -->
						<div class="border border-border rounded-lg overflow-hidden">
							<button
								type="button"
								on:click={() => toggleSection('behavior')}
								class="w-full px-3 py-2 bg-accent flex items-center justify-between text-sm text-foreground hover:bg-muted"
							>
								<span>Behavior Settings</span>
								<svg class="w-4 h-4 transition-transform {expandedSections.behavior ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>
							{#if expandedSections.behavior}
								<div class="p-3 space-y-3 bg-card">
									<label class="flex items-center gap-2 cursor-pointer">
										<input type="checkbox" bind:checked={profileForm.include_partial_messages} class="w-4 h-4 rounded bg-muted border-0 text-violet-600 focus:ring-ring" />
										<div>
											<span class="text-sm text-foreground">Include Partial Messages</span>
											<p class="text-xs text-muted-foreground">Stream partial text as it's being generated</p>
										</div>
									</label>
									<label class="flex items-center gap-2 cursor-pointer">
										<input type="checkbox" bind:checked={profileForm.continue_conversation} class="w-4 h-4 rounded bg-muted border-0 text-violet-600 focus:ring-ring" />
										<div>
											<span class="text-sm text-foreground">Continue Conversation</span>
											<p class="text-xs text-muted-foreground">Automatically continue most recent conversation</p>
										</div>
									</label>
									<label class="flex items-center gap-2 cursor-pointer">
										<input type="checkbox" bind:checked={profileForm.fork_session} class="w-4 h-4 rounded bg-muted border-0 text-violet-600 focus:ring-ring" />
										<div>
											<span class="text-sm text-foreground">Fork Session</span>
											<p class="text-xs text-muted-foreground">Create new session ID when resuming</p>
										</div>
									</label>
								</div>
							{/if}
						</div>

						<!-- Enabled Subagents Accordion -->
						<div class="border border-border rounded-lg overflow-hidden">
							<button
								type="button"
								on:click={() => toggleSection('enabledAgents')}
								class="w-full px-3 py-2 bg-accent flex items-center justify-between text-sm text-foreground hover:bg-muted"
							>
								<div class="flex items-center gap-2">
									<span>Enabled Subagents</span>
									{#if profileForm.enabled_agents.length > 0}
										<span class="text-xs px-1.5 py-0.5 bg-primary/20 text-primary rounded">{profileForm.enabled_agents.length}</span>
									{/if}
								</div>
								<svg class="w-4 h-4 transition-transform {expandedSections.enabledAgents ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>
							{#if expandedSections.enabledAgents}
								<div class="p-3 bg-card">
									{#if allSubagents.length === 0}
										<p class="text-sm text-muted-foreground text-center py-2">No subagents configured. Create subagents from the Agents menu.</p>
									{:else}
										<p class="text-xs text-muted-foreground mb-3">Select which subagents this profile can use</p>
										<div class="space-y-2 max-h-48 overflow-y-auto">
											{#each allSubagents as agent (agent.id)}
												<label class="flex items-start gap-3 p-2 rounded-lg border border-border hover:border-primary/50 cursor-pointer transition-colors">
													<input
														type="checkbox"
														checked={profileForm.enabled_agents.includes(agent.id)}
														on:change={() => toggleAgent(agent.id)}
														class="mt-0.5 w-4 h-4 rounded bg-muted border-0 text-primary focus:ring-ring"
													/>
													<div class="flex-1 min-w-0">
														<div class="flex items-center gap-2">
															<span class="text-sm font-medium text-foreground">{agent.name}</span>
															<span class="text-xs px-1.5 py-0.5 rounded bg-primary/10 text-primary">{getModelDisplay(agent.model)}</span>
														</div>
														<p class="text-xs text-muted-foreground mt-0.5 line-clamp-1">{agent.description}</p>
													</div>
												</label>
											{/each}
										</div>
										<div class="flex items-center justify-between mt-3 pt-2 border-t border-border">
											<span class="text-xs text-muted-foreground">{profileForm.enabled_agents.length} of {allSubagents.length} enabled</span>
											<div class="flex gap-2">
												<button type="button" on:click={() => profileForm.enabled_agents = []} class="text-xs text-primary hover:underline">Clear</button>
												<button type="button" on:click={() => profileForm.enabled_agents = allSubagents.map(a => a.id)} class="text-xs text-primary hover:underline">All</button>
											</div>
										</div>
									{/if}
								</div>
							{/if}
						</div>

						<!-- System Prompt Accordion -->
						<div class="border border-border rounded-lg overflow-hidden">
							<button
								type="button"
								on:click={() => toggleSection('systemPrompt')}
								class="w-full px-3 py-2 bg-accent flex items-center justify-between text-sm text-foreground hover:bg-muted"
							>
								<span>System Prompt</span>
								<svg class="w-4 h-4 transition-transform {expandedSections.systemPrompt ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>
							{#if expandedSections.systemPrompt}
								<div class="p-3 space-y-3 bg-card">
									<div>
										<label class="block text-xs text-muted-foreground mb-1">Prompt Type</label>
										<select bind:value={profileForm.system_prompt_type} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground">
											<option value="preset">Claude Code + Appended Instructions</option>
											<option value="custom">Custom System Prompt</option>
										</select>
									</div>
									{#if profileForm.system_prompt_type === 'preset'}
										<div>
											<label class="block text-xs text-muted-foreground mb-1">Append Instructions</label>
											<textarea bind:value={profileForm.system_prompt_append} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground resize-y" rows="3" placeholder="Additional instructions to append to Claude Code's system prompt..."></textarea>
											<p class="text-xs text-muted-foreground mt-1">These instructions will be added after Claude Code's built-in system prompt.</p>
										</div>
									{:else}
										<div>
											<label class="block text-xs text-muted-foreground mb-1">Custom System Prompt</label>
											<textarea bind:value={profileForm.system_prompt_content} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground resize-y" rows="4" placeholder="Enter your custom system prompt (can be blank)..."></textarea>
										</div>
										<label class="flex items-center gap-2 cursor-pointer">
											<input type="checkbox" bind:checked={profileForm.system_prompt_inject_env} class="w-4 h-4 rounded bg-muted border-0 text-violet-600 focus:ring-ring" />
											<span class="text-sm text-foreground">Inject Environment Details</span>
										</label>
										<p class="text-xs text-muted-foreground">Adds working directory, platform, git status, and today's date to the system prompt.</p>
									{/if}
								</div>
							{/if}
						</div>

						<!-- Settings Sources Accordion -->
						<div class="border border-border rounded-lg overflow-hidden">
							<button
								type="button"
								on:click={() => toggleSection('settingSources')}
								class="w-full px-3 py-2 bg-accent flex items-center justify-between text-sm text-foreground hover:bg-muted"
							>
								<span>Settings Sources</span>
								<svg class="w-4 h-4 transition-transform {expandedSections.settingSources ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>
							{#if expandedSections.settingSources}
								<div class="p-3 bg-card">
									<p class="text-xs text-muted-foreground mb-2">Load settings from filesystem locations</p>
									<div class="flex flex-wrap gap-3">
										<label class="flex items-center gap-2 cursor-pointer">
											<input type="checkbox" checked={profileForm.setting_sources.includes('user')} on:change={() => toggleSettingSource('user')} class="w-4 h-4 rounded bg-muted border-0 text-violet-600 focus:ring-ring" />
											<span class="text-sm text-foreground">User (~/.claude)</span>
										</label>
										<label class="flex items-center gap-2 cursor-pointer">
											<input type="checkbox" checked={profileForm.setting_sources.includes('project')} on:change={() => toggleSettingSource('project')} class="w-4 h-4 rounded bg-muted border-0 text-violet-600 focus:ring-ring" />
											<span class="text-sm text-foreground">Project (.claude)</span>
										</label>
										<label class="flex items-center gap-2 cursor-pointer">
											<input type="checkbox" checked={profileForm.setting_sources.includes('local')} on:change={() => toggleSettingSource('local')} class="w-4 h-4 rounded bg-muted border-0 text-violet-600 focus:ring-ring" />
											<span class="text-sm text-foreground">Local</span>
										</label>
									</div>
								</div>
							{/if}
						</div>

						<!-- Advanced Settings Accordion -->
						<div class="border border-border rounded-lg overflow-hidden">
							<button
								type="button"
								on:click={() => toggleSection('advanced')}
								class="w-full px-3 py-2 bg-accent flex items-center justify-between text-sm text-foreground hover:bg-muted"
							>
								<span>Advanced Settings</span>
								<svg class="w-4 h-4 transition-transform {expandedSections.advanced ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>
							{#if expandedSections.advanced}
								<div class="p-3 space-y-3 bg-card">
									<div class="grid grid-cols-2 gap-3">
										<div>
											<label class="block text-xs text-muted-foreground mb-1">Working Directory</label>
											<input bind:value={profileForm.cwd} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground" placeholder="/workspace/my-project" />
										</div>
										<div>
											<label class="block text-xs text-muted-foreground mb-1">User Identifier</label>
											<input bind:value={profileForm.user} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground" placeholder="user@example.com" />
										</div>
									</div>
									<div>
										<label class="block text-xs text-muted-foreground mb-1">Additional Directories</label>
										<input bind:value={profileForm.add_dirs} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground" placeholder="/extra/dir1, /extra/dir2 (comma-separated)" />
									</div>
									<div>
										<label class="block text-xs text-muted-foreground mb-1">Max Buffer Size (bytes)</label>
										<input type="number" bind:value={profileForm.max_buffer_size} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground" placeholder="Default" />
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
								class="flex-1 px-4 py-2 bg-muted text-foreground rounded-lg hover:bg-accent"
							>
								Cancel
							</button>
							<button on:click={saveProfile} class="flex-1 px-4 py-2 bg-primary text-foreground rounded-lg hover:opacity-90"> Save </button>
						</div>
					</div>
				{:else}
					<!-- Hidden file input for import -->
					<input type="file" accept=".json" bind:this={profileImportInput} on:change={handleProfileImport} class="hidden" />

					<div class="space-y-2 mb-4">
						{#each $profiles as profile}
							<div class="flex items-center justify-between p-3 bg-accent rounded-lg">
								<div class="flex-1 min-w-0">
									<div class="flex items-center gap-2">
										<p class="text-sm text-foreground font-medium">{profile.name}</p>
										{#if $groups.profiles.assignments[profile.id]}
											<span class="text-[10px] px-1.5 py-0.5 bg-primary/10 text-primary rounded">{$groups.profiles.assignments[profile.id]}</span>
										{/if}
									</div>
									<p class="text-xs text-muted-foreground">{profile.id}</p>
								</div>
								<div class="flex gap-1">
									<!-- Group button -->
									<div class="relative group/dropdown">
										<button class="p-1.5 text-muted-foreground hover:text-foreground" title="Assign to group">
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
											</svg>
										</button>
										<div class="absolute right-0 top-full mt-1 w-40 bg-card border border-border rounded-lg shadow-lg opacity-0 invisible group-hover/dropdown:opacity-100 group-hover/dropdown:visible transition-all z-50">
											<div class="py-1 max-h-48 overflow-y-auto">
												{#each $groups.profiles.groups as group}
													<button
														on:click={() => groups.assignToGroup('profiles', profile.id, group.name)}
														class="w-full px-3 py-1.5 text-left text-sm hover:bg-accent transition-colors flex items-center gap-2"
													>
														{#if $groups.profiles.assignments[profile.id] === group.name}
															<svg class="w-3 h-3 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
															</svg>
														{:else}
															<span class="w-3"></span>
														{/if}
														{group.name}
													</button>
												{/each}
												{#if $groups.profiles.assignments[profile.id]}
													<button
														on:click={() => groups.removeFromGroup('profiles', profile.id)}
														class="w-full px-3 py-1.5 text-left text-sm hover:bg-accent transition-colors text-muted-foreground"
													>
														<span class="ml-5">Remove</span>
													</button>
												{/if}
												<div class="border-t border-border my-1"></div>
												<button
													on:click={() => { const name = prompt('New group name:'); if (name?.trim()) { groups.createGroup('profiles', name.trim()); groups.assignToGroup('profiles', profile.id, name.trim()); } }}
													class="w-full px-3 py-1.5 text-left text-sm hover:bg-accent transition-colors text-muted-foreground"
												>
													<span class="ml-5">+ New group</span>
												</button>
											</div>
										</div>
									</div>
									<!-- Export button (always visible) -->
									<button on:click={() => exportProfile(profile.id)} class="p-1.5 text-muted-foreground hover:text-foreground" title="Export profile">
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
										</svg>
									</button>
									{#if !profile.is_builtin}
										<button on:click={() => editProfile(profile)} class="p-1.5 text-muted-foreground hover:text-foreground" title="Edit profile">
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
											</svg>
										</button>
										<button on:click={() => deleteProfile(profile.id)} class="p-1.5 text-muted-foreground hover:text-destructive" title="Delete profile">
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
											</svg>
										</button>
									{/if}
								</div>
							</div>
						{/each}
					</div>
					<div class="flex gap-2">
						<button on:click={openNewProfileForm} class="flex-1 py-2 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-gray-500">
							+ New Profile
						</button>
						<button on:click={triggerProfileImport} disabled={profileImporting} class="px-4 py-2 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-gray-500 flex items-center gap-2" title="Import profile from file">
							{#if profileImporting}
								<span class="animate-spin inline-block w-4 h-4 border-2 border-current border-t-transparent rounded-full"></span>
							{:else}
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
								</svg>
							{/if}
							Import
						</button>
					</div>
				{/if}
			</div>
		</div>
	</div>
{/if}

<!-- Project Modal -->
{#if showProjectModal}
	<div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" on:click={() => (showProjectModal = false)}>
		<div class="bg-card rounded-xl w-full max-w-lg max-h-[80vh] overflow-y-auto" on:click|stopPropagation>
			<div class="p-4 border-b border-border flex items-center justify-between">
				<h2 class="text-lg font-semibold text-foreground">Projects</h2>
				<button
					class="text-muted-foreground hover:text-foreground"
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
							<label class="block text-xs text-muted-foreground mb-1">ID</label>
							<input bind:value={newProjectId} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground" placeholder="my-project" />
						</div>
						<div>
							<label class="block text-xs text-muted-foreground mb-1">Name</label>
							<input bind:value={newProjectName} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground" placeholder="My Project" />
						</div>
						<div>
							<label class="block text-xs text-muted-foreground mb-1">Description</label>
							<textarea bind:value={newProjectDescription} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground resize-none" rows="2" placeholder="Optional"></textarea>
						</div>
						<div class="flex gap-2">
							<button on:click={() => (showNewProjectForm = false)} class="flex-1 px-4 py-2 bg-muted text-foreground rounded-lg hover:bg-accent"> Cancel </button>
							<button on:click={createProject} class="flex-1 px-4 py-2 bg-primary text-foreground rounded-lg hover:opacity-90"> Create </button>
						</div>
					</div>
				{:else}
					<div class="space-y-2 mb-4">
						{#each $projects as project}
							<div class="flex items-center justify-between p-3 bg-accent rounded-lg">
								<div class="flex-1 min-w-0">
									<div class="flex items-center gap-2">
										<p class="text-sm text-foreground font-medium">{project.name}</p>
										{#if $groups.projects.assignments[project.id]}
											<span class="text-[10px] px-1.5 py-0.5 bg-primary/10 text-primary rounded">{$groups.projects.assignments[project.id]}</span>
										{/if}
									</div>
									<p class="text-xs text-muted-foreground mt-0.5">/workspace/{project.path}/</p>
								</div>
								<div class="flex gap-1">
									<!-- Group button -->
									<div class="relative group/dropdown">
										<button class="p-1.5 text-muted-foreground hover:text-foreground" title="Assign to group">
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
											</svg>
										</button>
										<div class="absolute right-0 top-full mt-1 w-40 bg-card border border-border rounded-lg shadow-lg opacity-0 invisible group-hover/dropdown:opacity-100 group-hover/dropdown:visible transition-all z-50">
											<div class="py-1 max-h-48 overflow-y-auto">
												{#each $groups.projects.groups as group}
													<button
														on:click={() => groups.assignToGroup('projects', project.id, group.name)}
														class="w-full px-3 py-1.5 text-left text-sm hover:bg-accent transition-colors flex items-center gap-2"
													>
														{#if $groups.projects.assignments[project.id] === group.name}
															<svg class="w-3 h-3 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
															</svg>
														{:else}
															<span class="w-3"></span>
														{/if}
														{group.name}
													</button>
												{/each}
												{#if $groups.projects.assignments[project.id]}
													<button
														on:click={() => groups.removeFromGroup('projects', project.id)}
														class="w-full px-3 py-1.5 text-left text-sm hover:bg-accent transition-colors text-muted-foreground"
													>
														<span class="ml-5">Remove</span>
													</button>
												{/if}
												<div class="border-t border-border my-1"></div>
												<button
													on:click={() => { const name = prompt('New group name:'); if (name?.trim()) { groups.createGroup('projects', name.trim()); groups.assignToGroup('projects', project.id, name.trim()); } }}
													class="w-full px-3 py-1.5 text-left text-sm hover:bg-accent transition-colors text-muted-foreground"
												>
													<span class="ml-5">+ New group</span>
												</button>
											</div>
										</div>
									</div>
									<button on:click={() => deleteProject(project.id)} class="p-1.5 text-muted-foreground hover:text-destructive" title="Delete project">
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
										</svg>
									</button>
								</div>
							</div>
						{/each}
					</div>
					<button on:click={() => (showNewProjectForm = true)} class="w-full py-2 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-gray-500">
						+ New Project
					</button>
				{/if}
			</div>
		</div>
	</div>
{/if}

<!-- Terminal Modal for interactive CLI commands (like /resume) -->
{#if showTerminalModal && terminalSessionId}
	<TerminalModal
		sessionId={terminalSessionId}
		command={terminalCommand}
		onClose={closeTerminalModal}
		onRewindComplete={handleRewindComplete}
	/>
{/if}

<!-- Rewind Modal V2 - Direct JSONL manipulation (bulletproof) -->
{#if showRewindModal && rewindSessionId}
	<RewindModal
		sessionId={rewindSessionId}
		onClose={closeRewindModal}
		onRewindComplete={handleRewindCompleteV2}
	/>
{/if}

<style>
	.overflow-wrap-anywhere {
		overflow-wrap: anywhere;
		word-break: break-word;
	}
	.scrollbar-hide::-webkit-scrollbar {
		display: none;
	}
	.scrollbar-hide {
		-ms-overflow-style: none;
		scrollbar-width: none;
	}
	.sidebar-slide-in {
		animation: slideInFromLeft 0.2s ease-out;
	}
	@keyframes slideInFromLeft {
		from {
			transform: translateX(-100%);
			opacity: 0;
		}
		to {
			transform: translateX(0);
			opacity: 1;
		}
	}
</style>
