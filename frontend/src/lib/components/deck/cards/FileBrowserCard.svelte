<script lang="ts">
	/**
	 * FileBrowserCard — workspace files in the shared view layout.
	 *
	 * Renders FilesStudio, whose tree claims the shared navigation panel and
	 * whose data pane owns all browse/preview/upload/download/rename/delete
	 * functionality via the `files` store.
	 */
	import FilesStudio from '../files/FilesStudio.svelte';
</script>

<FilesStudio />
