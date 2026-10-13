<script lang="ts">
 /**
  * Shared, credential-free agent presentation. Each avatar is one file in
  * ./avatars, listed in ./avatars/avatars.json; the drawings are inlined at
  * build time, so showing one makes no network request.
  *
  * `color` is an avatar colour name (./avatar-colors), drawn from a theme
  * accent token. `neutral` is the built-in look: muted colour, dashed edge.
  */
 import type { AvatarId } from '$lib/api/assistant';
 import { avatarColorToken } from './avatar-colors';
 const files = import.meta.glob('./avatars/*.svg', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;
 const drawings: Record<string, string> = Object.fromEntries(
  Object.entries(files).map(([path, svg]) => [path.slice('./avatars/'.length, -'.svg'.length), svg])
 );
 let {
  avatar = 'robot',
  size = 40,
  color = null,
  neutral = false
 }: { avatar?: AvatarId; size?: number; color?: string | null; neutral?: boolean } = $props();
 // An id this build does not know (another worker's preset) shows the robot.
 const known = $derived(Object.hasOwn(drawings, avatar) ? avatar : 'robot');
 const tint = $derived(neutral ? 'var(--muted-foreground)' : avatarColorToken(color));
</script>

<!-- Static files from this folder, never user content. -->
<span
 class="agent-avatar"
 class:neutral
 data-avatar={known}
 data-color={neutral ? 'neutral' : (tint ? color : undefined)}
 style:width="{size}px"
 style:height="{size}px"
 style:--av={tint ?? undefined}
 aria-hidden="true">{@html drawings[known]}</span>

<style>
 .agent-avatar { --c:var(--av, var(--primary)); display:inline-flex; flex:none; align-items:center; justify-content:center; color:var(--c); background:color-mix(in oklch,var(--c) 9%,var(--background)); border:1px solid color-mix(in oklch,var(--c) 22%,var(--border)); border-radius:28%; overflow:hidden; }
 .agent-avatar.neutral { border-style:dashed; background:color-mix(in oklch,var(--c) 8%,var(--background)); }
 .agent-avatar :global(svg) { width:86%; height:86%; }
 .agent-avatar :global(.shape) { fill:color-mix(in oklch,var(--c) 26%,var(--background)); stroke:currentColor; stroke-width:2; stroke-linejoin:round; }
 .agent-avatar :global(.eyes) { stroke:var(--foreground); stroke-width:4; stroke-linecap:round; }
 .agent-avatar :global(.smile) { stroke:var(--foreground); stroke-width:2; stroke-linecap:round; }
 .agent-avatar :global(.ring) { fill:var(--background); stroke:currentColor; stroke-width:2; }
 .agent-avatar :global(.mark) { fill:currentColor; stroke:currentColor; stroke-width:1.5; stroke-linejoin:round; }
</style>
