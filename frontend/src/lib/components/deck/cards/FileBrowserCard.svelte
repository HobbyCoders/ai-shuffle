<script lang="ts">
	/**
	 * FileBrowserCard - File browser card for The Deck
	 *
	 * Provides interface for browsing and managing project files.
	 * Supports both desktop (with BaseCard wrapper) and mobile (standalone) modes.
	 */

	import { Files, Folder, FileText, FileCode, FileImage, Upload, Search } from 'lucide-svelte';
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';

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

	// Mock file structure for preview
	const mockFiles = [
		{ name: 'src', type: 'folder', icon: Folder },
		{ name: 'components', type: 'folder', icon: Folder },
		{ name: 'README.md', type: 'file', icon: FileText },
		{ name: 'app.ts', type: 'file', icon: FileCode },
		{ name: 'logo.png', type: 'file', icon: FileImage },
	];
</script>

{#if mobile}
	<!-- Mobile: Full-screen card with no BaseCard wrapper -->
	<div class="browser-content mobile">
		<div class="browser-header">
			<div class="search-bar">
				<Search size={16} />
				<span>Search files...</span>
			</div>
			<button class="upload-btn">
				<Upload size={18} />
			</button>
		</div>
		<div class="file-list">
			{#each mockFiles as file}
				<div class="file-item">
					<svelte:component this={file.icon} size={18} />
					<span class="file-name">{file.name}</span>
				</div>
			{/each}
		</div>
		<div class="browser-footer">
			<span class="file-count">5 items</span>
		</div>
	</div>
{:else}
	<!-- Desktop: Full BaseCard with all features -->
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="browser-content">
			<div class="browser-header">
				<div class="search-bar">
					<Search size={16} />
					<span>Search files...</span>
				</div>
				<button class="upload-btn">
					<Upload size={18} />
				</button>
			</div>
			<div class="file-list">
				{#each mockFiles as file}
					<div class="file-item">
						<svelte:component this={file.icon} size={18} />
						<span class="file-name">{file.name}</span>
					</div>
				{/each}
			</div>
			<div class="browser-footer">
				<span class="file-count">5 items</span>
			</div>
		</div>
	</BaseCard>
{/if}

<style>
	.browser-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		background: linear-gradient(180deg, oklch(0.11 0.015 85) 0%, oklch(0.08 0.01 85) 100%);
		color: var(--foreground);
		overflow: hidden;
		min-height: 100px;
		border-radius: 0 0 12px 12px;
	}

	.browser-content.mobile {
		height: 100%;
		border-radius: 0;
	}

	.browser-header {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 12px 16px;
		background: oklch(0.09 0.01 85);
		border-bottom: 1px solid oklch(0.15 0.015 85);
	}

	.search-bar {
		flex: 1;
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 14px;
		background: oklch(0.06 0.01 85);
		border-radius: 8px;
		color: oklch(0.5 0.01 85);
		font-size: 0.875rem;
		border: 1px solid oklch(0.15 0.01 85);
		cursor: pointer;
		transition: all 0.2s ease;
	}

	.search-bar:hover {
		border-color: oklch(0.25 0.02 85);
		background: oklch(0.08 0.01 85);
	}

	.upload-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		background: oklch(0.15 0.02 85);
		border: 1px solid oklch(0.2 0.02 85);
		border-radius: 8px;
		color: oklch(0.7 0.02 85);
		cursor: pointer;
		transition: all 0.2s ease;
	}

	.upload-btn:hover {
		background: oklch(0.2 0.03 85);
		color: var(--gold);
		border-color: var(--gold-dim);
	}

	.file-list {
		flex: 1;
		overflow-y: auto;
		padding: 8px;
	}

	.file-item {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 12px 14px;
		border-radius: 8px;
		color: oklch(0.75 0.01 85);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.file-item:hover {
		background: oklch(0.14 0.015 85);
		color: oklch(0.9 0.01 85);
	}

	.file-item :global(svg) {
		color: var(--gold);
		opacity: 0.7;
		flex-shrink: 0;
	}

	.file-item:hover :global(svg) {
		opacity: 1;
	}

	.file-name {
		flex: 1;
		font-size: 0.875rem;
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.browser-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 16px;
		background: oklch(0.09 0.01 85);
		border-top: 1px solid oklch(0.15 0.015 85);
		font-size: 0.75rem;
		color: oklch(0.5 0.01 85);
	}

	.file-count {
		font-weight: 500;
	}
</style>
