<script lang="ts">
	/**
	 * The worktree chip's on/off mark: the app's switch at the size of the row
	 * under the composer. Decorative: the control around it carries the name and
	 * the state, and it sits inside a button or label so it cannot be a second
	 * button itself (settings/rows/Switch.svelte).
	 */
	interface Props {
		on: boolean;
	}

	let { on }: Props = $props();
</script>

<span class="worktree-switch" class:on aria-hidden="true"></span>

<style>
	.worktree-switch {
		position: relative;
		flex: none;
		display: block;
		width: 28px;
		height: 16px;
		/* The chip's gap is 6px; the label starts 8px after the track. */
		margin-right: 2px;
		border-radius: 999px;
		background: var(--switch-off); box-shadow: inset 0 0 0 1px var(--switch-off-edge);
		transition: background-color 0.15s;
	}
	.worktree-switch::after {
		content: '';
		position: absolute;
		top: 2px;
		left: 2px;
		width: 12px;
		height: 12px;
		border-radius: 50%;
		background: var(--switch-thumb);
		box-shadow: 0 0 0 0.5px var(--switch-thumb-edge), 0 1px 2px var(--switch-thumb-edge);
		transition: transform 0.15s;
	}
	.worktree-switch.on {
		background: var(--switch-on); box-shadow: none;
	}
	.worktree-switch.on::after {
		transform: translateX(12px);
	}
	@media (prefers-reduced-motion: reduce) {
		.worktree-switch, .worktree-switch::after { transition: none; }
	}
</style>
