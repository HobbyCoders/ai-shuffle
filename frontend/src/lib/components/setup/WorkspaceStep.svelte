<script lang="ts">
	/**
	 * Wizard step: the workspace, where project checkouts live.
	 *
	 * On Docker and Unraid the path is inside the container (always
	 * `/workspace`), so the step also says which host folder backs it: the
	 * worker's own answer when it reports one (`host_path`), otherwise where to
	 * look (WORKSPACE_DIR in `.env`, or the Unraid template's Workspace path).
	 */
	import { FolderOpen, HardDrive, Loader2 } from 'lucide-svelte';
	import DirectoryBrowser from '$lib/components/DirectoryBrowser.svelte';

	interface Props {
		path: string;
		/** A Docker or Unraid install (the worker is not in local mode). */
		container: boolean;
		/** The host folder behind /workspace, when the worker knows it. */
		hostPath?: string | null;
		saving?: boolean;
		error?: string;
		onSave: () => void;
		onBack?: () => void;
	}
	let { path = $bindable(''), container, hostPath = null, saving = false, error = '', onSave, onBack }: Props = $props();

	const CONTAINER_ROOT = '/workspace';
	const outsideMount = $derived(container && !!path.trim() && path.trim() !== CONTAINER_ROOT && !path.trim().startsWith(CONTAINER_ROOT + '/'));
</script>

<div class="space-y-4" data-testid="setup-workspace">
	<div class="bg-primary/10 border border-primary/30 rounded-lg p-4">
		<div class="flex items-start gap-3">
			<FolderOpen size={20} strokeWidth={1.5} class="text-primary mt-0.5 shrink-0" />
			<div>
				<h2 class="text-foreground font-medium mb-1">Choose the workspace folder</h2>
				<p class="text-muted-foreground text-sm">Your projects live here. Agents can read and change files in the projects you give them, so keep anything you do not want an agent to touch outside it.</p>
			</div>
		</div>
	</div>

	{#if container}
		<div class="bg-muted/30 border border-border rounded-lg p-4 flex items-start gap-3" data-testid="setup-workspace-host">
			<HardDrive size={18} strokeWidth={1.5} class="text-muted-foreground mt-0.5 shrink-0" />
			<div class="text-sm text-muted-foreground space-y-1 min-w-0">
				<p>Inside the container the workspace is <code class="font-mono text-foreground">{CONTAINER_ROOT}</code>. Choose it or a folder inside it.</p>
				{#if hostPath}
					<p>On the host it is <code class="font-mono text-foreground break-all">{hostPath}</code>.</p>
				{:else}
					<p>On the host it is the folder you mapped to it:</p>
					<ul class="list-disc pl-5">
						<li>Docker Compose: <code class="font-mono text-foreground">WORKSPACE_DIR</code> in <code class="font-mono">.env</code> (<code class="font-mono">./workspace</code> next to <code class="font-mono">compose.yaml</code> by default).</li>
						<li>Unraid: the template's Workspace path (<code class="font-mono break-all">/mnt/user/appdata/ai-shuffle/workspace</code> by default).</li>
					</ul>
				{/if}
			</div>
		</div>
	{/if}

	<DirectoryBrowser onSelect={(p: string) => (path = p)} selectedPath={path} />
	{#if path}
		<div class="text-sm text-foreground font-mono bg-muted/50 px-3 py-2 rounded-lg border border-border flex items-center gap-2">
			<FolderOpen size={14} strokeWidth={1.5} class="text-primary shrink-0" />
			<span class="truncate">{path}</span>
		</div>
	{/if}
	<div>
		<label for="workspaceManual" class="block text-xs text-muted-foreground mb-1">Or type a path manually</label>
		<input type="text" id="workspaceManual" bind:value={path} class="input font-mono text-sm" placeholder="Enter workspace path" />
	</div>
	{#if outsideMount}
		<div role="status" class="bg-warning/10 border border-warning/30 text-foreground px-3 py-2 rounded-lg text-sm">
			This folder is not under {CONTAINER_ROOT}, so it is not on a host folder: its files are lost when the container is re-created.
		</div>
	{/if}
	{#if error}
		<div role="alert" class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">{error}</div>
	{/if}
	<div class="flex gap-3">
		{#if onBack}<button type="button" onclick={onBack} class="btn btn-secondary flex-1 py-3">Back</button>{/if}
		<button type="button" onclick={onSave} class="btn btn-primary flex-1 py-3" disabled={!path.trim() || saving}>
			{#if saving}<Loader2 size={16} strokeWidth={2} class="inline-block animate-spin mr-2" />{/if}
			Continue
		</button>
	</div>
</div>
