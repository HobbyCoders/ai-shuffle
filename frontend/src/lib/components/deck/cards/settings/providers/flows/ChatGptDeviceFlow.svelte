<script lang="ts">
	/**
	 * ChatGptDeviceFlow — STUB (plan §1.3, Chunk 6 wires the real Codex device flow).
	 *
	 * Chunk 6 replaces this file's contents with the create → start → poll →
	 * done/error device-code enrollment. For now it only satisfies the Props
	 * interface so `AddProviderModal` can route the "OpenAI subscription" card to
	 * it. Keep this trivial.
	 */

	interface Props {
		onDone: () => void;
		onCancel: () => void;
		onBusyChange?: (busy: boolean) => void;
	}
	// `onDone` / `onBusyChange` are part of the contract Chunk 6 uses; only
	// `onCancel` is wired in the stub.
	let { onCancel }: Props = $props();
</script>

<div class="space-y-4">
	<p class="text-sm text-muted-foreground leading-relaxed">ChatGPT sign-in is being wired up.</p>

	<div class="flex items-center justify-end pt-1">
		<button
			type="button"
			onclick={onCancel}
			class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
		>
			Cancel
		</button>
	</div>
</div>
