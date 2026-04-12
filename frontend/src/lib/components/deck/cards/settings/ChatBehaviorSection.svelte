<script lang="ts">
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences';
	import './settings-shared.css';

	interface Props {
		expanded: boolean;
		onToggle: () => void;
		chatPrefs: ChatPreferences | null;
	}

	let {
		expanded,
		onToggle,
		chatPrefs,
	}: Props = $props();
</script>

<section class="section">
	<button class="section-header" onclick={onToggle}>
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
			{:else}
				<div class="loading-text">Loading preferences...</div>
			{/if}
		</div>
	{/if}
</section>
