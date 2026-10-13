import {
	Compass,
	Database,
	FileCheck,
	FolderGit2,
	House,
	Lightbulb,
	Mail,
	MessageCircle,
	Music,
	Package,
	Webhook
} from 'lucide-svelte';
import type { SettingsIcon } from '$lib/settings/categories';

const CONNECTION_ICONS: Record<string, SettingsIcon> = {
	gmail: Mail,
	spotify: Music,
	hue: Lightbulb,
	homeassistant: House,
	webhook: Webhook
};

export function connectionIcon(type: string): SettingsIcon {
	return CONNECTION_ICONS[type] ?? MessageCircle;
}

export const PACKAGE_ICON: SettingsIcon = Package;
export const REVIEW_ICON: SettingsIcon = FileCheck;
export const EXPLORE_ICON: SettingsIcon = Compass;
export const SOURCES_ICON: SettingsIcon = Database;
export const PROJECTS_ICON: SettingsIcon = FolderGit2;
