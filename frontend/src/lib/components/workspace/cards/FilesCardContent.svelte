<script lang="ts">
	/**
	 * FilesCardContent - File browser card content
	 *
	 * Placeholder for file browser functionality.
	 * Will eventually allow browsing project files and folders.
	 */

	interface Props {
		cardId: string;
		folderId: string | null;
	}

	let { cardId, folderId }: Props = $props();
</script>

<div class="files-card">
	<div class="placeholder">
		<svg
			class="icon"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="1.5"
		>
			<path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
		</svg>
		<h3 class="title">File Browser</h3>
		<p class="description">Browse and manage project files</p>
		<p class="coming-soon">Coming soon</p>
	</div>
</div>

<style>
	.files-card {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: #1a1a1a;
	}

	.placeholder {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 2rem;
		text-align: center;
		color: rgba(255, 255, 255, 0.5);
	}

	.icon {
		width: 64px;
		height: 64px;
		margin-bottom: 1rem;
		opacity: 0.4;
	}

	.title {
		font-size: 1.125rem;
		font-weight: 600;
		color: rgba(255, 255, 255, 0.7);
		margin-bottom: 0.5rem;
	}

	.description {
		font-size: 0.875rem;
		margin-bottom: 1rem;
	}

	.coming-soon {
		font-size: 0.75rem;
		padding: 0.25rem 0.75rem;
		background: rgba(255, 255, 255, 0.1);
		border-radius: 9999px;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}
</style>
