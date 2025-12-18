<script lang="ts">
	/**
	 * ModeRail - Mode switcher for the sidebar
	 *
	 * Displays available workspace modes (Chat, Canvas, Tasks, etc.)
	 * and allows switching between them.
	 *
	 * This replaces the top part of the sidebar icon rail.
	 */

	import { workspace, activeMode, type WorkspaceMode } from '$lib/stores/workspace';

	interface ModeConfig {
		id: WorkspaceMode;
		name: string;
		icon: string;
		description: string;
	}

	// Available modes - will grow as we add more
	const modes: ModeConfig[] = [
		{
			id: 'chat',
			name: 'Chat',
			icon: 'M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z',
			description: 'AI chat conversations'
		}
		// Future modes will be added here:
		// { id: 'canvas', name: 'Canvas', icon: '...', description: 'Visual workspace' },
		// { id: 'tasks', name: 'Tasks', icon: '...', description: 'Task management' },
	];

	function selectMode(mode: WorkspaceMode) {
		workspace.setMode(mode);
	}
</script>

<div class="mode-rail">
	<div class="mode-label">Mode</div>
	<div class="modes">
		{#each modes as mode}
			<button
				class="mode-button"
				class:active={$activeMode === mode.id}
				onclick={() => selectMode(mode.id)}
				title={mode.description}
			>
				<svg
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.5"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d={mode.icon} />
				</svg>
				<span class="mode-name">{mode.name}</span>
			</button>
		{/each}
	</div>
</div>

<style>
	.mode-rail {
		padding: 12px;
		border-bottom: 1px solid rgba(255, 255, 255, 0.1);
	}

	.mode-label {
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.1em;
		color: rgba(255, 255, 255, 0.4);
		margin-bottom: 8px;
		padding-left: 4px;
	}

	.modes {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.mode-button {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 8px 10px;
		background: transparent;
		border: none;
		border-radius: 8px;
		color: rgba(255, 255, 255, 0.6);
		cursor: pointer;
		transition: all 0.15s ease;
		text-align: left;
	}

	.mode-button:hover {
		background: rgba(255, 255, 255, 0.05);
		color: rgba(255, 255, 255, 0.9);
	}

	.mode-button.active {
		background: rgba(255, 165, 0, 0.15);
		color: #fb923c;
	}

	.mode-button svg {
		width: 18px;
		height: 18px;
		flex-shrink: 0;
	}

	.mode-name {
		font-size: 0.875rem;
		font-weight: 500;
	}
</style>
