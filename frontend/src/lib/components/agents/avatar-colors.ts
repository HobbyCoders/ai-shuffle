/**
 * Avatar colours. Each is a theme accent token, never a literal, so every
 * theme preset (light, dark, Vercel) recolours avatars with the rest of the UI.
 * The stored value is the name (`agent.avatar_color`); unknown or empty names
 * fall back to the app's primary colour, which is also the `primary` choice.
 */
/** Exactly the values the agent API accepts for `avatar_color`. */
export const AVATAR_COLORS = ['primary', 'emerald', 'blue', 'rose', 'warning'] as const;
export type AvatarColor = (typeof AVATAR_COLORS)[number];

const TOKENS: Record<AvatarColor, string> = {
	primary: 'var(--primary)',
	emerald: 'var(--emerald)',
	blue: 'var(--blue)',
	rose: 'var(--rose)',
	warning: 'var(--warning)'
};

export const AVATAR_COLOR_LABELS: Record<AvatarColor, string> = {
	primary: 'Accent',
	emerald: 'Green',
	blue: 'Blue',
	rose: 'Red',
	warning: 'Amber'
};

export function isAvatarColor(value: unknown): value is AvatarColor {
	return typeof value === 'string' && (AVATAR_COLORS as readonly string[]).includes(value);
}

/** CSS colour for a stored name, or null for "use the default". */
export function avatarColorToken(value: string | null | undefined): string | null {
	return isAvatarColor(value) ? TOKENS[value] : null;
}
