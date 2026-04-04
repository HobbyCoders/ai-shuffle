<script lang="ts">
	/**
	 * ChatBehaviorSettings - Toggle switches for chat display preferences
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences';
	import { onMount } from 'svelte';

	let expanded = $state(false);
	let chatPrefs = $state<ChatPreferences | null>(null);

	onMount(() => {
		chatPreferences.load();
		const unsubscribe = chatPreferences.subscribe((prefs: ChatPreferences) => {
			chatPrefs = prefs;
		});
		return () => { unsubscribe(); };
	});
</script>

<section class="section">
	<button class="section-header" onclick={() => expanded = !expanded}>
		<h3>Chat Behavior</h3>
		<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if expanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			{#if chatPrefs}
				<!-- Show Thinking Blocks -->
				<div class="setting-row">
					<div class="setting-info">
						<span>Show Thinking Blocks</span>
						<span class="setting-desc">Display Claude's reasoning process</span>
					</div>
					<button
						onclick={() => chatPreferences.setShowThinkingBlocks(!chatPrefs?.showThinkingBlocks)}
						class="toggle"
						class:active={chatPrefs.showThinkingBlocks}
						role="switch"
						aria-checked={chatPrefs.showThinkingBlocks}
						aria-label="Show Thinking Blocks"
					>
						<span class="toggle-knob"></span>
					</button>
				</div>

				<!-- Thinking Blocks Start Expanded -->
				<div class="setting-row">
					<div class="setting-info">
						<span>Expand Thinking by Default</span>
						<span class="setting-desc">Thinking blocks start expanded</span>
					</div>
					<button
						onclick={() => chatPreferences.setThinkingStartExpanded(!chatPrefs?.thinkingStartExpanded)}
						class="toggle"
						class:active={chatPrefs.thinkingStartExpanded}
						disabled={!chatPrefs.showThinkingBlocks}
						role="switch"
						aria-checked={chatPrefs.thinkingStartExpanded}
						aria-label="Expand Thinking by Default"
					>
						<span class="toggle-knob"></span>
					</button>
				</div>

				<!-- Thinking Summary on Collapse -->
				<div class="setting-row">
					<div class="setting-info">
						<span>Show Thinking Summary</span>
						<span class="setting-desc">Preview thinking content when collapsed</span>
					</div>
					<button
						onclick={() => chatPreferences.setShowThinkingSummary(!chatPrefs?.showThinkingSummary)}
						class="toggle"
						class:active={chatPrefs.showThinkingSummary}
						disabled={!chatPrefs.showThinkingBlocks}
						role="switch"
						aria-checked={chatPrefs.showThinkingSummary}
						aria-label="Show Thinking Summary"
					>
						<span class="toggle-knob"></span>
					</button>
				</div>

				<!-- Tools Start Expanded -->
				<div class="setting-row">
					<div class="setting-info">
						<span>Expand Tools by Default</span>
						<span class="setting-desc">Tool results start expanded</span>
					</div>
					<button
						onclick={() => chatPreferences.setToolsStartExpanded(!chatPrefs?.toolsStartExpanded)}
						class="toggle"
						class:active={chatPrefs.toolsStartExpanded}
						role="switch"
						aria-checked={chatPrefs.toolsStartExpanded}
						aria-label="Expand Tools by Default"
					>
						<span class="toggle-knob"></span>
					</button>
				</div>

				<!-- Todo List Start Expanded -->
				<div class="setting-row">
					<div class="setting-info">
						<span>Expand Todo List by Default</span>
						<span class="setting-desc">Task list starts expanded</span>
					</div>
					<button
						onclick={() => chatPreferences.setTodosStartExpanded(!chatPrefs?.todosStartExpanded)}
						class="toggle"
						class:active={chatPrefs.todosStartExpanded}
						role="switch"
						aria-checked={chatPrefs.todosStartExpanded}
						aria-label="Expand Todo List by Default"
					>
						<span class="toggle-knob"></span>
					</button>
				</div>

				<hr class="divider" />

				<!-- Auto-scroll on new messages -->
				<div class="setting-row">
					<div class="setting-info">
						<span>Auto-scroll on New Messages</span>
						<span class="setting-desc">Automatically scroll to new content</span>
					</div>
					<button
						onclick={() => chatPreferences.setAutoScrollOnNewMessage(!chatPrefs?.autoScrollOnNewMessage)}
						class="toggle"
						class:active={chatPrefs.autoScrollOnNewMessage}
						role="switch"
						aria-checked={chatPrefs.autoScrollOnNewMessage}
						aria-label="Auto-scroll on New Messages"
					>
						<span class="toggle-knob"></span>
					</button>
				</div>

				<!-- Show Message Timestamps -->
				<div class="setting-row">
					<div class="setting-info">
						<span>Show Message Timestamps</span>
						<span class="setting-desc">Display time on each message</span>
					</div>
					<button
						onclick={() => chatPreferences.setShowMessageTimestamps(!chatPrefs?.showMessageTimestamps)}
						class="toggle"
						class:active={chatPrefs.showMessageTimestamps}
						role="switch"
						aria-checked={chatPrefs.showMessageTimestamps}
						aria-label="Show Message Timestamps"
					>
						<span class="toggle-knob"></span>
					</button>
				</div>

				<!-- Compact Message View -->
				<div class="setting-row">
					<div class="setting-info">
						<span>Compact Message View</span>
						<span class="setting-desc">Reduce spacing between messages</span>
					</div>
					<button
						onclick={() => chatPreferences.setCompactMessageView(!chatPrefs?.compactMessageView)}
						class="toggle"
						class:active={chatPrefs.compactMessageView}
						role="switch"
						aria-checked={chatPrefs.compactMessageView}
						aria-label="Compact Message View"
					>
						<span class="toggle-knob"></span>
					</button>
				</div>

				<hr class="divider" />

				<!-- Fast Mode -->
				<div class="setting-row">
					<div class="setting-info">
						<span>Fast Mode ↯</span>
						<span class="setting-desc">Faster output with Opus 4.6 (requires Opus model)</span>
					</div>
					<button
						onclick={() => chatPreferences.setFastMode(!chatPrefs?.fastMode)}
						class="toggle"
						class:active={chatPrefs.fastMode}
						role="switch"
						aria-checked={chatPrefs.fastMode}
						aria-label="Fast Mode"
					>
						<span class="toggle-knob"></span>
					</button>
				</div>
			{:else}
				<div class="loading-text">Loading preferences...</div>
			{/if}
		</div>
	{/if}
</section>

<style>
	.section {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 12px 16px;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: background 150ms ease;
	}

	.section-header:hover { background: var(--hover-overlay); }
	.section-header:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.section-header h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
	}

	@media (min-width: 640px) {
		.section-header {
			justify-content: center;
			position: relative;
		}
		.section-header .chevron {
			position: absolute;
			right: 16px;
		}
	}

	.chevron {
		width: 20px;
		height: 20px;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.chevron.expanded { transform: rotate(180deg); }

	.section-content {
		padding: 8px 16px 16px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.setting-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 8px 0;
	}

	.setting-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.setting-desc {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.toggle {
		position: relative;
		width: 44px;
		height: 24px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		cursor: pointer;
		transition: all 200ms ease;
		flex-shrink: 0;
	}

	.toggle:hover { border-color: var(--ring); }
	.toggle:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.toggle.active {
		background: var(--primary);
		border-color: var(--primary);
	}

	.toggle:disabled { opacity: 0.5; cursor: not-allowed; }

	.toggle-knob {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 18px;
		height: 18px;
		background: var(--primary-foreground);
		border-radius: 50%;
		transition: transform 200ms ease;
		box-shadow: var(--shadow-s);
	}

	.toggle.active .toggle-knob {
		transform: translateX(20px);
	}

	.divider {
		border: none;
		border-top: 1px solid var(--border);
		margin: 12px 0;
	}

	.loading-text {
		text-align: center;
		padding: 20px;
		color: var(--muted-foreground);
	}
</style>
