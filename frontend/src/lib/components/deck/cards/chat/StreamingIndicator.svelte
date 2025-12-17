<script lang="ts">
	/**
	 * StreamingIndicator - Animated typing indicator shown during AI response
	 */
</script>

<div class="flex items-center gap-3 px-4 py-3">
	<div class="w-8 h-8 rounded-full bg-purple-500/10 flex items-center justify-center flex-shrink-0">
		<svg class="w-4 h-4 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
		</svg>
	</div>
	<div class="bg-card border border-border/50 rounded-2xl rounded-bl-sm px-4 py-3">
		<div class="flex items-center gap-2">
			<div class="flex gap-1">
				<span class="w-2 h-2 rounded-full bg-primary animate-bounce" style="animation-delay: 0ms"></span>
				<span class="w-2 h-2 rounded-full bg-primary animate-bounce" style="animation-delay: 150ms"></span>
				<span class="w-2 h-2 rounded-full bg-primary animate-bounce" style="animation-delay: 300ms"></span>
			</div>
			<span class="text-sm text-muted-foreground">Claude is thinking...</span>
		</div>
	</div>
</div>
