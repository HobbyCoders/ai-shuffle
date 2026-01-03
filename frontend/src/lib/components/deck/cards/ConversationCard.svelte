<script lang="ts">
	/**
	 * ConversationCard - Real-time voice conversation card for The Deck
	 *
	 * Enables voice conversations with AI profiles using:
	 * - Voice Activity Detection (VAD) with mute control
	 * - STT for speech transcription
	 * - Claude responses via existing chat infrastructure
	 * - TTS for AI speech output
	 * - Interruption handling (user can interrupt AI at any time)
	 */

	import { onMount, onDestroy, tick } from 'svelte';
	import { Mic, MicOff, Square, Volume2, VolumeX, Settings, Loader2, Phone, PhoneOff, MessageSquare } from 'lucide-svelte';
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { conversation, getSessionStore, type ConversationStatus, type ConversationTurn } from '$lib/stores/conversation';
	import { profiles, projects, tabs, allTabs, type ChatMessage } from '$lib/stores/tabs';
	import { AudioService } from '$lib/services/audioService';
	import { api } from '$lib/api/client';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let { card, onClose, onMaximize, onFocus, onMove, onResize, onDragEnd, onResizeEnd, mobile = false }: Props = $props();

	// ========================================================================
	// State
	// ========================================================================

	// Session state (reactive from store) - use derived store for proper reactivity
	const sessionStore = getSessionStore(card.id);
	let session = $derived($sessionStore);

	// Local UI state
	let selectedProfileId = $state<string>('');
	let selectedProjectId = $state<string>('');
	let audioLevel = $state(0);
	let isUserSpeaking = $state(false);  // Track when VAD detects user speech
	let isInitializing = $state(false);
	let showSettings = $state(false);
	let transcriptEl: HTMLDivElement | undefined = $state();

	// Audio service instance
	let audioService: AudioService | null = $state(null);

	// TTS settings (loaded from user settings)
	let ttsVoice = $state('nova');
	let ttsModel = $state('gpt-4o-mini-tts');
	let ttsSpeed = $state(1.0);

	// Track the last processed message to avoid duplicate TTS
	let lastProcessedMessageId = $state<string | null>(null);

	// Track if we're currently generating/playing TTS
	let isGeneratingTts = $state(false);

	// Get derived session values
	const status = $derived(session?.status ?? 'idle');
	const isMuted = $derived(session?.isMuted ?? false);
	const turns = $derived(session?.turns ?? []);
	const currentUtterance = $derived(session?.currentUtterance ?? '');
	const errorMessage = $derived(session?.errorMessage ?? null);

	// Profile/project lists
	const profileList = $derived($profiles);
	const projectList = $derived($projects);

	// Check if data is still loading
	const isDataLoading = $derived(profileList.length === 0 || projectList.length === 0);

	// Get the linked chat tab for this conversation
	const linkedTab = $derived.by(() => {
		if (!session?.chatTabId) return null;
		return $allTabs.find((t) => t.id === session.chatTabId) ?? null;
	});

	// Track the last assistant message for TTS
	const lastAssistantMessage = $derived.by(() => {
		if (!linkedTab) return null;
		// Find the last non-streaming assistant text message
		const messages = linkedTab.messages;
		for (let i = messages.length - 1; i >= 0; i--) {
			const msg = messages[i];
			if (msg.role === 'assistant' && msg.type === 'text' && !msg.streaming) {
				return msg;
			}
		}
		return null;
	});

	// Check if the tab is currently streaming
	const isTabStreaming = $derived(linkedTab?.isStreaming ?? false);

	// Get selected profile/project names
	const selectedProfileName = $derived(
		profileList.find((p) => p.id === selectedProfileId)?.name ?? 'Select Profile'
	);
	const selectedProjectName = $derived(
		projectList.find((p) => p.id === selectedProjectId)?.name ?? 'Select Project'
	);

	// Status display text
	const statusText = $derived.by(() => {
		switch (status) {
			case 'idle': return 'Ready';
			case 'connecting': return 'Connecting...';
			case 'listening':
				if (isMuted) return 'Muted';
				if (isUserSpeaking) return 'You\'re speaking...';
				return 'Listening...';
			case 'processing': return 'Processing...';
			case 'speaking': return 'Speaking...';
			case 'error': return 'Error';
			default: return 'Ready';
		}
	});

	// Status indicator color class
	const statusColor = $derived.by(() => {
		switch (status) {
			case 'listening': return isMuted ? 'bg-yellow-500' : 'bg-green-500';
			case 'processing': return 'bg-blue-500';
			case 'speaking': return 'bg-purple-500';
			case 'error': return 'bg-red-500';
			default: return 'bg-gray-500';
		}
	});

	// ========================================================================
	// Lifecycle
	// ========================================================================

	onMount(async () => {
		// Initialize with defaults from card data if available
		// Only pre-select if explicitly saved in card data, otherwise leave empty for user to choose
		selectedProfileId = (card.data?.profileId as string) ?? '';
		selectedProjectId = (card.data?.projectId as string) ?? '';

		// Load TTS settings from user preferences
		await loadTtsSettings();

		// Don't auto-create session - wait for user to click Start
	});

	/**
	 * Load TTS settings from user preferences
	 */
	async function loadTtsSettings(): Promise<void> {
		try {
			const response = await api.get<{
				tts_model: string | null;
				tts_voice: string | null;
				tts_speed: number | null;
			}>('/settings/integrations');

			if (response.tts_model) ttsModel = response.tts_model;
			if (response.tts_voice) ttsVoice = response.tts_voice;
			if (response.tts_speed) ttsSpeed = response.tts_speed;

			console.log('[ConversationCard] Loaded TTS settings:', { ttsModel, ttsVoice, ttsSpeed });
		} catch (error) {
			console.warn('[ConversationCard] Failed to load TTS settings, using defaults:', error);
		}
	}

	onDestroy(() => {
		// Clean up audio service
		if (audioService) {
			audioService.stop();
			audioService = null;
		}

		// Remove session from store
		conversation.removeSession(card.id);
	});

	// Auto-scroll transcript when new turns arrive
	$effect(() => {
		if (turns.length > 0 && transcriptEl) {
			tick().then(() => {
				if (transcriptEl) {
					transcriptEl.scrollTop = transcriptEl.scrollHeight;
				}
			});
		}
	});

	// Watch for new assistant messages and generate TTS
	$effect(() => {
		// Only process when conversation is active (not idle)
		if (status === 'idle' || !session) return;

		// Check if there's a new completed assistant message
		if (lastAssistantMessage && lastAssistantMessage.id !== lastProcessedMessageId && !isTabStreaming) {
			// We have a new completed message - generate TTS
			handleNewAssistantMessage(lastAssistantMessage);
		}
	});

	// Watch for streaming state changes to update status
	$effect(() => {
		if (!session) return;

		if (isTabStreaming && status === 'listening') {
			// Claude is responding, update status
			conversation.setStatus(card.id, 'processing');
		} else if (!isTabStreaming && status === 'processing' && !isGeneratingTts) {
			// Claude finished, go back to listening (unless we're about to speak)
			conversation.setStatus(card.id, 'listening');
		}
	});

	// ========================================================================
	// Session Management
	// ========================================================================

	async function initializeSession(): Promise<void> {
		if (!selectedProfileId || !selectedProjectId) {
			return;
		}

		isInitializing = true;

		try {
			// Create a new chat tab for this conversation
			const tabId = tabs.createTab();
			tabs.setTabProfile(tabId, selectedProfileId);
			tabs.setTabProject(tabId, selectedProjectId);

			// Get the tab to get session ID
			const tab = $allTabs.find((t) => t.id === tabId);

			// Create conversation session
			const profile = profileList.find((p) => p.id === selectedProfileId);
			const project = projectList.find((p) => p.id === selectedProjectId);

			conversation.createSession(card.id, {
				profileId: selectedProfileId,
				profileName: profile?.name ?? 'Unknown',
				projectId: selectedProjectId,
				projectName: project?.name ?? 'Unknown',
				chatTabId: tabId,
				chatSessionId: tab?.sessionId ?? '',
			});

			console.log('[ConversationCard] Session initialized');
		} catch (error) {
			console.error('[ConversationCard] Failed to initialize session:', error);
			conversation.setStatus(card.id, 'error', 'Failed to initialize conversation');
		} finally {
			isInitializing = false;
		}
	}

	// ========================================================================
	// Conversation Controls
	// ========================================================================

	async function startConversation(): Promise<void> {
		// Prevent double-clicks while initializing
		if (isInitializing) {
			console.log('[ConversationCard] Already initializing, ignoring click');
			return;
		}

		// Prevent starting if already started
		if (audioService) {
			console.log('[ConversationCard] Already started, ignoring click');
			return;
		}

		// Initialize session if needed (check store directly, not derived)
		const existingSession = conversation.getSession(card.id);
		if (!existingSession) {
			await initializeSession();
			// Verify session was created
			const newSession = conversation.getSession(card.id);
			if (!newSession) {
				console.error('[ConversationCard] Failed to create session');
				return;
			}
		}

		try {
			conversation.setStatus(card.id, 'connecting');

			// Initialize audio service
			audioService = new AudioService({
				vadSensitivity: 0.02,
				silenceThresholdMs: 1200,
			});

			// Set up callbacks
			audioService.onAudioLevelChange((level) => {
				audioLevel = level;
			});

			// Track VAD state for UI feedback
			audioService.onVadStateChange((vadState) => {
				isUserSpeaking = vadState.isSpeaking;
			});

			audioService.onSpeechEnded(async (audioBlob) => {
				await handleSpeechEnd(audioBlob);
			});

			// Start capturing
			await audioService.start();

			conversation.setStatus(card.id, 'listening');
			console.log('[ConversationCard] Conversation started');
		} catch (error) {
			console.error('[ConversationCard] Failed to start:', error);
			conversation.setStatus(card.id, 'error', 'Failed to access microphone');
		}
	}

	function stopConversation(): void {
		if (audioService) {
			audioService.stop();
			audioService = null;
		}

		audioLevel = 0;
		isUserSpeaking = false;
		conversation.setStatus(card.id, 'idle');
		console.log('[ConversationCard] Conversation stopped');
	}

	function toggleMute(): void {
		if (!session) return;

		conversation.toggleMute(card.id);

		if (audioService) {
			audioService.setMuted(!isMuted);
		}
	}

	// ========================================================================
	// TTS Handling
	// ========================================================================

	/**
	 * Handle a new assistant message - add to conversation and generate TTS
	 */
	async function handleNewAssistantMessage(message: ChatMessage): Promise<void> {
		if (!session || !message.content) return;

		// Mark this message as processed
		lastProcessedMessageId = message.id;

		// Add the assistant turn to the conversation
		const turnId = conversation.addOrUpdateAssistantTurn(card.id, message.content, false);

		// Generate and play TTS
		await generateAndPlayTTS(message.content, turnId);
	}

	/**
	 * Generate TTS audio and play it
	 */
	async function generateAndPlayTTS(text: string, turnId: string): Promise<void> {
		if (!audioService || !session) return;

		// Limit text length for TTS (4096 chars max)
		const ttsText = text.length > 4000 ? text.substring(0, 4000) + '...' : text;

		if (!ttsText.trim()) {
			// Nothing to speak
			return;
		}

		isGeneratingTts = true;
		conversation.setStatus(card.id, 'speaking');

		try {
			console.log('[ConversationCard] Generating TTS for:', ttsText.substring(0, 100) + '...');

			// Call the TTS endpoint
			const response = await api.post<{
				audio_url: string;
				file_path: string;
				duration_seconds?: number;
			}>('/canvas/generate/tts', {
				text: ttsText,
				provider: 'openai-tts',
				model: ttsModel,
				voice: ttsVoice,
				speed: ttsSpeed,
				output_format: 'mp3',
			});

			if (response.audio_url) {
				// Update the turn with the audio URL
				conversation.completeAssistantTurn(card.id, turnId, response.audio_url);

				// Play the audio
				await audioService.playTts(response.audio_url);

				console.log('[ConversationCard] TTS playback complete');
			}
		} catch (error) {
			console.error('[ConversationCard] TTS generation failed:', error);
			// Don't show error to user for TTS failures - just continue listening
		} finally {
			isGeneratingTts = false;

			// Go back to listening if still in conversation
			if (session && status !== 'idle') {
				conversation.setStatus(card.id, 'listening');
			}
		}
	}

	// ========================================================================
	// Speech Handling
	// ========================================================================

	async function handleSpeechEnd(audioBlob: Blob): Promise<void> {
		if (!session) return;

		conversation.setStatus(card.id, 'processing');

		try {
			// Transcribe the audio
			const formData = new FormData();
			formData.append('file', audioBlob, 'speech.webm');

			const response = await fetch('/api/v1/settings/transcribe', {
				method: 'POST',
				credentials: 'include',
				body: formData,
			});

			if (!response.ok) {
				throw new Error('Transcription failed');
			}

			const result = await response.json();
			const transcribedText = result.text?.trim();

			if (!transcribedText) {
				// No speech detected, go back to listening
				conversation.setStatus(card.id, 'listening');
				return;
			}

			// Add user turn
			conversation.addUserTurn(card.id, transcribedText);

			// Send to chat via existing WebSocket infrastructure
			if (session.chatTabId) {
				tabs.sendMessage(session.chatTabId, transcribedText);
			}

			// Start listening again (AI response will be handled by chat subscription)
			conversation.setStatus(card.id, 'listening');

		} catch (error) {
			console.error('[ConversationCard] Speech processing failed:', error);
			conversation.setStatus(card.id, 'error', 'Failed to process speech');

			// Recover after delay
			setTimeout(() => {
				if (session?.status === 'error') {
					conversation.setStatus(card.id, 'listening');
				}
			}, 2000);
		}
	}

	// ========================================================================
	// Rendering Helpers
	// ========================================================================

	function formatTimestamp(date: Date): string {
		return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
	}
</script>

{#if mobile}
	<!-- Mobile: Full-screen conversation view -->
	<div class="conversation-content mobile">
		{@render conversationUI()}
	</div>
{:else}
	<!-- Desktop: BaseCard wrapper -->
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="conversation-content">
			{@render conversationUI()}
		</div>
	</BaseCard>
{/if}

{#snippet conversationUI()}
	<!-- Loading State -->
	{#if isDataLoading}
		<div class="loading-state">
			<Loader2 size={32} class="animate-spin" />
			<p>Loading profiles and projects...</p>
		</div>
	{:else}
		<!-- Header: Profile & Status -->
		<div class="conversation-header">
			<div class="profile-section">
				{#if session && status !== 'idle'}
					<!-- Show profile info only when conversation is active -->
					<div class="profile-avatar">
						<MessageSquare size={24} />
					</div>
					<div class="profile-info">
						<div class="profile-name">{session.profileName}</div>
						<div class="profile-project">{session.projectName}</div>
					</div>
				{:else}
					<!-- Profile/Project Selection - always show when idle -->
					<div class="selection-row">
						<select
							class="profile-select"
							bind:value={selectedProfileId}
							disabled={status !== 'idle'}
						>
							<option value="">Select Profile</option>
							{#each profileList as profile}
								<option value={profile.id}>{profile.name}</option>
							{/each}
						</select>
						<select
							class="project-select"
							bind:value={selectedProjectId}
							disabled={status !== 'idle'}
						>
							<option value="">Select Project</option>
							{#each projectList as project}
								<option value={project.id}>{project.name}</option>
							{/each}
						</select>
					</div>
				{/if}
			</div>

			<div class="status-section">
				<div class="status-indicator {statusColor}"></div>
				<span class="status-text">{statusText}</span>
			</div>
		</div>

	<!-- Transcript Area -->
	<div class="transcript-area" bind:this={transcriptEl}>
		{#if turns.length === 0}
			<div class="empty-state">
				<div class="empty-icon">
					<Mic size={48} strokeWidth={1.5} />
				</div>
				<p class="empty-text">
					{#if status === 'idle'}
						Start a conversation to begin speaking with your AI assistant
					{:else if status === 'listening'}
						Listening for your voice...
					{:else}
						{statusText}
					{/if}
				</p>
			</div>
		{:else}
			{#each turns as turn (turn.id)}
				<div class="turn {turn.role}">
					<div class="turn-content">
						<span class="turn-text">{turn.text}</span>
						{#if turn.isStreaming}
							<span class="streaming-indicator">
								<Loader2 size={12} class="animate-spin" />
							</span>
						{/if}
					</div>
					<div class="turn-meta">
						<span class="turn-time">{formatTimestamp(turn.timestamp)}</span>
					</div>
				</div>
			{/each}
		{/if}

		<!-- Current utterance (live STT) -->
		{#if currentUtterance}
			<div class="turn user current">
				<div class="turn-content">
					<span class="turn-text">{currentUtterance}</span>
					<span class="streaming-indicator">
						<Loader2 size={12} class="animate-spin" />
					</span>
				</div>
			</div>
		{/if}
	</div>

	<!-- Audio Visualization -->
	<div class="audio-viz">
		<div class="viz-bar-container">
			{#each Array(12) as _, i}
				<div
					class="viz-bar"
					style="height: {status === 'listening' && !isMuted
						? Math.max(4, audioLevel * 100 * (1 + Math.sin(i * 0.5) * 0.3))
						: 4}px"
				></div>
			{/each}
		</div>
	</div>

	<!-- Error Message -->
	{#if errorMessage}
		<div class="error-banner">
			<span>{errorMessage}</span>
		</div>
	{/if}

	<!-- Control Bar (Pill) -->
	<div class="control-bar">
		{#if status === 'idle'}
			<!-- Start Button -->
			<button
				class="control-btn primary"
				onclick={startConversation}
				disabled={!selectedProfileId || !selectedProjectId || isInitializing}
				title="Start conversation"
			>
				{#if isInitializing}
					<Loader2 size={20} class="animate-spin" />
				{:else}
					<Phone size={20} />
				{/if}
				<span>Start</span>
			</button>
		{:else}
			<!-- Mute Button -->
			<button
				class="control-btn {isMuted ? 'muted' : ''}"
				onclick={toggleMute}
				title={isMuted ? 'Unmute' : 'Mute'}
			>
				{#if isMuted}
					<MicOff size={20} />
				{:else}
					<Mic size={20} />
				{/if}
			</button>

			<!-- Stop Button -->
			<button
				class="control-btn danger"
				onclick={stopConversation}
				title="End conversation"
			>
				<PhoneOff size={20} />
				<span>End</span>
			</button>

			<!-- Volume Button (placeholder for now) -->
			<button
				class="control-btn"
				onclick={() => showSettings = !showSettings}
				title="Settings"
			>
				<Settings size={18} />
			</button>
		{/if}
	</div>
	{/if}
{/snippet}

<style>
	.conversation-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: linear-gradient(180deg, rgba(15, 15, 18, 0.95) 0%, rgba(10, 10, 12, 0.98) 100%);
		overflow: hidden;
	}

	.conversation-content.mobile {
		padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
	}

	/* Loading State */
	.loading-state {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 16px;
		color: rgba(255, 255, 255, 0.5);
	}

	.loading-state p {
		font-size: 14px;
	}

	:global(.light) .loading-state {
		color: rgba(0, 0, 0, 0.5);
	}

	/* Header */
	.conversation-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 16px 20px;
		border-bottom: 1px solid rgba(255, 255, 255, 0.06);
		flex-shrink: 0;
	}

	.profile-section {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.profile-avatar {
		width: 44px;
		height: 44px;
		border-radius: 12px;
		background: linear-gradient(135deg, rgba(45, 212, 191, 0.2), rgba(45, 212, 191, 0.1));
		display: flex;
		align-items: center;
		justify-content: center;
		color: #2dd4bf;
	}

	.profile-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.profile-name {
		font-size: 15px;
		font-weight: 600;
		color: #f4f4f5;
	}

	.profile-project {
		font-size: 12px;
		color: rgba(255, 255, 255, 0.5);
	}

	.selection-row {
		display: flex;
		gap: 8px;
	}

	.profile-select,
	.project-select {
		padding: 8px 12px;
		background: rgba(255, 255, 255, 0.05);
		border: 1px solid rgba(255, 255, 255, 0.1);
		border-radius: 8px;
		color: #f4f4f5;
		font-size: 13px;
		min-width: 120px;
		cursor: pointer;
	}

	.profile-select:disabled,
	.project-select:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.status-section {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.status-indicator {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		animation: pulse 2s ease-in-out infinite;
	}

	.status-indicator.bg-green-500 {
		background: #22c55e;
		box-shadow: 0 0 8px rgba(34, 197, 94, 0.5);
	}

	.status-indicator.bg-yellow-500 {
		background: #eab308;
		box-shadow: 0 0 8px rgba(234, 179, 8, 0.5);
	}

	.status-indicator.bg-blue-500 {
		background: #3b82f6;
		box-shadow: 0 0 8px rgba(59, 130, 246, 0.5);
		animation: pulse-fast 1s ease-in-out infinite;
	}

	.status-indicator.bg-purple-500 {
		background: #a855f7;
		box-shadow: 0 0 8px rgba(168, 85, 247, 0.5);
	}

	.status-indicator.bg-red-500 {
		background: #ef4444;
		box-shadow: 0 0 8px rgba(239, 68, 68, 0.5);
	}

	.status-indicator.bg-gray-500 {
		background: #6b7280;
		animation: none;
	}

	.status-text {
		font-size: 12px;
		font-weight: 500;
		color: rgba(255, 255, 255, 0.6);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; transform: scale(1); }
		50% { opacity: 0.7; transform: scale(1.1); }
	}

	@keyframes pulse-fast {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.5; }
	}

	/* Transcript Area */
	.transcript-area {
		flex: 1;
		overflow-y: auto;
		padding: 16px 20px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.empty-state {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 16px;
		text-align: center;
		padding: 40px;
	}

	.empty-icon {
		color: rgba(255, 255, 255, 0.2);
	}

	.empty-text {
		font-size: 14px;
		color: rgba(255, 255, 255, 0.4);
		max-width: 240px;
		line-height: 1.5;
	}

	.turn {
		display: flex;
		flex-direction: column;
		gap: 4px;
		max-width: 85%;
	}

	.turn.user {
		align-self: flex-end;
	}

	.turn.assistant {
		align-self: flex-start;
	}

	.turn-content {
		display: flex;
		align-items: flex-end;
		gap: 8px;
		padding: 10px 14px;
		border-radius: 16px;
		font-size: 14px;
		line-height: 1.5;
	}

	.turn.user .turn-content {
		background: linear-gradient(135deg, #2dd4bf, #14b8a6);
		color: #0a0a0b;
		border-bottom-right-radius: 4px;
	}

	.turn.assistant .turn-content {
		background: rgba(255, 255, 255, 0.08);
		color: #f4f4f5;
		border-bottom-left-radius: 4px;
	}

	.turn.current .turn-content {
		opacity: 0.7;
	}

	.turn-text {
		flex: 1;
	}

	.streaming-indicator {
		color: inherit;
		opacity: 0.6;
	}

	.turn-meta {
		display: flex;
		justify-content: flex-end;
		padding: 0 4px;
	}

	.turn-time {
		font-size: 11px;
		color: rgba(255, 255, 255, 0.3);
	}

	/* Audio Visualization */
	.audio-viz {
		padding: 12px 20px;
		display: flex;
		justify-content: center;
	}

	.viz-bar-container {
		display: flex;
		align-items: flex-end;
		gap: 4px;
		height: 32px;
	}

	.viz-bar {
		width: 6px;
		background: linear-gradient(180deg, #2dd4bf, rgba(45, 212, 191, 0.3));
		border-radius: 3px;
		transition: height 0.1s ease-out;
	}

	/* Error Banner */
	.error-banner {
		padding: 10px 20px;
		background: rgba(239, 68, 68, 0.15);
		border-top: 1px solid rgba(239, 68, 68, 0.3);
		color: #ef4444;
		font-size: 13px;
		text-align: center;
	}

	/* Control Bar */
	.control-bar {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 16px 20px 20px;
		background: rgba(0, 0, 0, 0.3);
		border-top: 1px solid rgba(255, 255, 255, 0.06);
	}

	.control-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 12px 20px;
		background: rgba(255, 255, 255, 0.08);
		border: none;
		border-radius: 24px;
		color: #f4f4f5;
		font-size: 14px;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.2s ease;
	}

	.control-btn:hover:not(:disabled) {
		background: rgba(255, 255, 255, 0.12);
	}

	.control-btn:active:not(:disabled) {
		transform: scale(0.96);
	}

	.control-btn:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.control-btn.primary {
		background: linear-gradient(135deg, #2dd4bf, #14b8a6);
		color: #0a0a0b;
	}

	.control-btn.primary:hover:not(:disabled) {
		box-shadow: 0 4px 20px rgba(45, 212, 191, 0.3);
	}

	.control-btn.danger {
		background: rgba(239, 68, 68, 0.2);
		color: #ef4444;
	}

	.control-btn.danger:hover:not(:disabled) {
		background: rgba(239, 68, 68, 0.3);
	}

	.control-btn.muted {
		background: rgba(234, 179, 8, 0.2);
		color: #eab308;
	}

	/* Scrollbar */
	.transcript-area::-webkit-scrollbar {
		width: 6px;
	}

	.transcript-area::-webkit-scrollbar-track {
		background: transparent;
	}

	.transcript-area::-webkit-scrollbar-thumb {
		background: rgba(255, 255, 255, 0.1);
		border-radius: 3px;
	}

	.transcript-area::-webkit-scrollbar-thumb:hover {
		background: rgba(255, 255, 255, 0.2);
	}

	/* Light mode adjustments */
	:global(.light) .conversation-content {
		background: linear-gradient(180deg, rgba(255, 255, 255, 0.98) 0%, rgba(250, 250, 250, 0.98) 100%);
	}

	:global(.light) .conversation-header {
		border-bottom-color: rgba(0, 0, 0, 0.08);
	}

	:global(.light) .profile-name {
		color: #18181b;
	}

	:global(.light) .profile-project {
		color: rgba(0, 0, 0, 0.5);
	}

	:global(.light) .profile-select,
	:global(.light) .project-select {
		background: rgba(0, 0, 0, 0.03);
		border-color: rgba(0, 0, 0, 0.1);
		color: #18181b;
	}

	:global(.light) .turn.assistant .turn-content {
		background: rgba(0, 0, 0, 0.05);
		color: #18181b;
	}

	:global(.light) .control-bar {
		background: rgba(0, 0, 0, 0.02);
		border-top-color: rgba(0, 0, 0, 0.08);
	}

	:global(.light) .control-btn {
		background: rgba(0, 0, 0, 0.05);
		color: #18181b;
	}

	:global(.light) .control-btn:hover:not(:disabled) {
		background: rgba(0, 0, 0, 0.1);
	}

	:global(.light) .empty-icon {
		color: rgba(0, 0, 0, 0.2);
	}

	:global(.light) .empty-text {
		color: rgba(0, 0, 0, 0.4);
	}
</style>
