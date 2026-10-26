<script lang="ts">
 /**
  * Shared, credential-free agent presentation. Each avatar is one file in
  * ./avatars, listed in ./avatars/avatars.json; the drawings are inlined at
  * build time, so showing one makes no network request.
  *
  * `color` is an avatar colour name (./avatar-colors), drawn from a theme
  * accent token. `neutral` is the built-in look: muted colour, dashed edge.
  */
 import type { AvatarId } from '$lib/api/agent-identity';
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
 /* Clay style: solid characters in the agent colour on a round badge. Shading
    is layered translucent shapes, never gradients (the files hold shapes only).
    Mixed in oklab, not oklch: the light theme's background is oklch(0.995 0 0),
    whose hue 0 would swing every tint toward pink/beige in a hue blend. Face
    details use fixed ink and white, so the body keeps a lightness floor: dark
    accents (light themes, Vercel's black primary) would otherwise swallow the face. */
 .agent-avatar { --c:var(--av, var(--primary)); --body:oklch(from var(--c) max(l, 0.72) c h); display:inline-flex; flex:none; align-items:center; justify-content:center; color:var(--c); background:color-mix(in oklab,var(--c) 17%,var(--background)); border-radius:50%; overflow:hidden; }
 .agent-avatar.neutral { background:color-mix(in oklab,var(--c) 10%,var(--background)); outline:1px dashed color-mix(in oklab,var(--c) 55%,transparent); outline-offset:-1px; }
 .agent-avatar.neutral :global(svg) { filter:saturate(0); }
 .agent-avatar :global(svg) { width:90%; height:90%; }
 .agent-avatar :global(.shape) { fill:var(--body); }
 .agent-avatar :global(.shape.pale), .agent-avatar :global(.tint) { fill:color-mix(in oklab,var(--body) 42%,#fff); }
 .agent-avatar :global(.deep) { fill:color-mix(in oklab,var(--body) 68%,#000); }
 .agent-avatar :global(.ink) { fill:#16181d; }
 .agent-avatar :global(.white) { fill:#fff; }
 .agent-avatar :global(.shine) { fill:#fff; opacity:.5; }
 .agent-avatar :global(.shade) { fill:#000; opacity:.14; }
 .agent-avatar :global(.blush) { fill:oklch(0.72 0.16 12); opacity:.5; }
 .agent-avatar :global(.gold) { fill:oklch(0.83 0.15 85); }
 .agent-avatar :global(.jewel) { fill:oklch(0.62 0.2 22); }
 .agent-avatar :global(.leaf) { fill:oklch(0.74 0.17 145); }
 .agent-avatar :global(.stroke-ink) { stroke:#16181d; stroke-width:2.6; stroke-linecap:round; stroke-linejoin:round; }
 .agent-avatar :global(.stroke-deep) { stroke:color-mix(in oklab,var(--body) 68%,#000); stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
 .agent-avatar :global(.stroke-tint) { stroke:color-mix(in oklab,var(--body) 42%,#fff); stroke-width:3.5; stroke-linecap:round; }
</style>
