<script lang="ts">
	/**
	 * DragDropOverlay - Full-area drag overlay with file drop handling.
	 * Wraps children and overlays a "Drop files to attach" indicator when dragging.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import { api, type FileUploadResponse } from '$lib/api/client';
	import type { Snippet } from 'svelte';

	interface Props {
		onFilesUploaded: (results: FileUploadResponse[]) => void;
		disabled?: boolean;
		children: Snippet;
	}

	let { onFilesUploaded, disabled = false, children }: Props = $props();

	let isDragging = $state(false);

	function handleDragEnter(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
		isDragging = true;
	}

	function handleDragLeave(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		if (
			e.clientX < rect.left ||
			e.clientX > rect.right ||
			e.clientY < rect.top ||
			e.clientY > rect.bottom
		) {
			isDragging = false;
		}
	}

	function handleDragOver(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
	}

	async function handleDrop(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
		isDragging = false;

		if (!e.dataTransfer?.files?.length) return;

		if (!$activeSession.project) {
			alert('Please select a project first to upload files.');
			return;
		}

		if (disabled) return;

		const results: FileUploadResponse[] = [];
		try {
			for (const file of Array.from(e.dataTransfer.files)) {
				const result = await api.uploadFile(
					`/projects/${$activeSession.project}/upload`,
					file,
					$activeSession.sessionId ?? undefined,
					$activeSession.worktreeIntent === 'worktree' ? $activeSession.worktreeId ?? undefined : undefined
				);
				results.push(result);
			}
			onFilesUploaded(results);
		} catch (error: unknown) {
			console.error('Upload failed:', error);
			const err = error as { detail?: string };
			alert(`Upload failed: ${err.detail || 'Unknown error'}`);
		}
	}
</script>

<div
	class="drag-drop-wrapper"
	ondragenter={handleDragEnter}
	ondragleave={handleDragLeave}
	ondragover={handleDragOver}
	ondrop={handleDrop}
	role="region"
	aria-label="File drop zone"
>
	{#if isDragging}
		<div class="drag-overlay">
			<div class="drag-overlay-content">
				<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
				<span>Drop files to attach</span>
			</div>
		</div>
	{/if}
	{@render children()}
</div>

<style>
	.drag-drop-wrapper {
		position: relative;
	}

	/* Drag overlay */
	.drag-overlay {
		position: absolute;
		inset: 0;
		z-index: 50;
		background: color-mix(in oklch, var(--popover) 95%, transparent);
		backdrop-filter: blur(8px);
		display: flex;
		align-items: center;
		justify-content: center;
		border-radius: 0.75rem;
		animation: fadeIn 0.2s ease;
	}

	.drag-overlay-content {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 1rem 1.5rem;
		border: 2px dashed var(--primary);
		border-radius: 0.75rem;
		background: color-mix(in oklch, var(--primary) 8%, transparent);
		color: var(--primary);
		font-weight: 500;
		font-size: 0.875rem;
	}

	.drag-overlay-content svg {
		width: 1.25rem;
		height: 1.25rem;
		flex-shrink: 0;
	}

	@keyframes fadeIn {
		from { opacity: 0; }
		to { opacity: 1; }
	}
</style>
