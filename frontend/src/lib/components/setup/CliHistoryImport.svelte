<script lang="ts">
	/**
	 * CliHistoryImport — bring recent Claude Code CLI conversations into AI
	 * Shuffle. Shown in setup's optional last step and in the Import dialog.
	 *
	 * The worker lists transcripts from the last 30 days that are not sessions
	 * yet; importing registers each transcript where it is (nothing is copied,
	 * and only transcripts are ever read — never credentials or settings).
	 * When there is nothing to import the section renders nothing at all.
	 */
	import { onMount } from 'svelte';
	import { History, Loader2, Check } from 'lucide-svelte';
	import { importCliConversation, listCliConversations, type CliConversation } from '$lib/api/cli-history';
	import { getErrorMessage } from '$lib/api/client';

	interface Props {
		onimported?: (count: number) => void;
	}

	let { onimported }: Props = $props();

	let conversations = $state<CliConversation[]>([]);
	let selected = $state<Set<string>>(new Set());
	let loaded = $state(false);
	let importing = $state(false);
	let importedCount = $state(0);
	let error = $state('');

	const allSelected = $derived(conversations.length > 0 && selected.size === conversations.length);

	function toggle(id: string) {
		const next = new Set(selected);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selected = next;
	}

	function toggleAll() {
		selected = allSelected ? new Set() : new Set(conversations.map((c) => c.sdk_session_id));
	}

	function when(seconds: number): string {
		return new Date(seconds * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}

	function folder(cwd: string | null): string {
		if (!cwd) return '';
		const parts = cwd.split(/[\\/]/).filter(Boolean);
		return parts[parts.length - 1] ?? cwd;
	}

	async function importSelected() {
		if (importing || selected.size === 0) return;
		importing = true;
		error = '';
		let done = 0;
		let lastError = '';
		const failed = new Set<string>();
		for (const conversation of conversations.filter((c) => selected.has(c.sdk_session_id))) {
			try {
				await importCliConversation(conversation);
				done++;
			} catch (e) {
				failed.add(conversation.sdk_session_id);
				lastError = getErrorMessage(e, 'Could not import a conversation.');
			}
		}
		// Imported rows leave the list; failed ones stay selected for a retry.
		conversations = conversations.filter((c) => !selected.has(c.sdk_session_id) || failed.has(c.sdk_session_id));
		selected = failed;
		importedCount += done;
		importing = false;
		if (failed.size) error = `${failed.size} could not be imported: ${lastError}`;
		if (done) onimported?.(done);
	}

	onMount(() => {
		listCliConversations()
			.then((list) => {
				conversations = list;
				selected = new Set(list.map((c) => c.sdk_session_id));
			})
			.catch(() => {
				// Not the owner, or the scan failed: the section simply stays hidden.
				conversations = [];
			})
			.finally(() => {
				loaded = true;
			});
	});
</script>

{#if loaded && (conversations.length > 0 || importedCount > 0)}
	<section class="cli-import" aria-labelledby="cli-import-title">
		<div class="cli-head">
			<History size={16} strokeWidth={2} aria-hidden="true" />
			<h3 id="cli-import-title">Claude Code conversations</h3>
		</div>
		{#if importedCount > 0}
			<p class="cli-done" role="status">
				<Check size={14} strokeWidth={2.4} aria-hidden="true" />
				Imported {importedCount} {importedCount === 1 ? 'conversation' : 'conversations'}. They are in your session list.
			</p>
		{/if}
		{#if conversations.length > 0}
			<p class="cli-sub">
				Found {conversations.length} recent {conversations.length === 1 ? 'conversation' : 'conversations'} from the Claude Code CLI on this worker. Import them to continue them here. Only the transcripts are read.
			</p>
			<label class="cli-all">
				<input type="checkbox" checked={allSelected} onchange={toggleAll} />
				<span>Select all</span>
			</label>
			<ul class="cli-list">
				{#each conversations as conversation (conversation.sdk_session_id)}
					<li>
						<label>
							<input
								type="checkbox"
								checked={selected.has(conversation.sdk_session_id)}
								onchange={() => toggle(conversation.sdk_session_id)}
							/>
							<span class="cli-title">{conversation.title}</span>
							<span class="cli-meta">
								{#if conversation.cwd}<span title={conversation.cwd}>{folder(conversation.cwd)}</span> · {/if}{conversation.message_count} messages · {when(conversation.modified_at)}
							</span>
						</label>
					</li>
				{/each}
			</ul>
			{#if error}<p class="cli-error" role="alert">{error}</p>{/if}
			<button type="button" class="btn btn-secondary w-full py-2.5 text-sm" onclick={importSelected} disabled={importing || selected.size === 0}>
				{#if importing}<Loader2 size={14} class="inline-block animate-spin mr-1" />{/if}
				Import {selected.size} {selected.size === 1 ? 'conversation' : 'conversations'}
			</button>
		{/if}
	</section>
{/if}

<style>
	.cli-import { display: flex; flex-direction: column; gap: 10px; }
	.cli-head { display: flex; align-items: center; gap: 8px; color: var(--foreground); }
	.cli-head h3 { margin: 0; font-size: 0.9375rem; font-weight: 600; }
	.cli-sub { margin: 0; font-size: 0.8125rem; color: var(--muted-foreground); }
	.cli-done { margin: 0; display: flex; align-items: center; gap: 6px; font-size: 0.8125rem; color: var(--success, var(--foreground)); }
	.cli-error { margin: 0; font-size: 0.8125rem; color: var(--destructive); }
	.cli-all { display: inline-flex; align-items: center; gap: 8px; font-size: 0.8125rem; color: var(--muted-foreground); cursor: pointer; }
	.cli-list {
		list-style: none; margin: 0; padding: 4px; max-height: 240px; overflow-y: auto;
		border: 1px solid var(--border); border-radius: var(--radius, 0.75rem);
	}
	.cli-list li label {
		display: grid; grid-template-columns: auto 1fr; column-gap: 8px; align-items: baseline;
		padding: 6px 8px; border-radius: 6px; cursor: pointer;
	}
	.cli-list li label:hover { background: var(--hover-overlay, var(--accent)); }
	.cli-list input { align-self: center; }
	.cli-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.875rem; color: var(--foreground); }
	.cli-meta { grid-column: 2; font-size: 0.75rem; color: var(--muted-foreground); }
</style>
