/**
 * Theme presets imported from tweakcn (https://tweakcn.com)
 * These themes provide complete styling including colors, fonts, shadows, and border radius.
 *
 * Static theme data lives in presets.json; this file provides types and helpers.
 */

import presetsData from './presets.json';

export interface ThemeStyles {
	background: string;
	foreground: string;
	card: string;
	'card-foreground': string;
	popover: string;
	'popover-foreground': string;
	primary: string;
	'primary-foreground': string;
	secondary: string;
	'secondary-foreground': string;
	muted: string;
	'muted-foreground': string;
	accent: string;
	'accent-foreground': string;
	destructive: string;
	'destructive-foreground': string;
	border: string;
	input: string;
	ring: string;
	sidebar?: string;
	'sidebar-foreground'?: string;
	'sidebar-primary'?: string;
	'sidebar-primary-foreground'?: string;
	'sidebar-accent'?: string;
	'sidebar-accent-foreground'?: string;
	'sidebar-border'?: string;
	'sidebar-ring'?: string;
	'font-sans'?: string;
	'font-serif'?: string;
	'font-mono'?: string;
	radius?: string;
	'shadow-color'?: string;
	'shadow-opacity'?: string;
	'shadow-blur'?: string;
	'shadow-spread'?: string;
	'shadow-offset-x'?: string;
	'shadow-offset-y'?: string;
	'letter-spacing'?: string;
	spacing?: string;
}

export interface ThemePreset {
	id: string;
	label: string;
	styles: {
		light: ThemeStyles;
		dark: ThemeStyles;
	};
}

export const themePresets: ThemePreset[] = presetsData as ThemePreset[];

// Helper to get a preset by ID
export function getPresetById(id: string): ThemePreset | undefined {
	return themePresets.find((preset) => preset.id === id);
}
