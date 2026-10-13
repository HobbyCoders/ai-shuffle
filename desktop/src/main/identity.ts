/** Build-time identity, read only from the packaged application's own metadata. */
import path from 'node:path';
export interface DesktopIdentity {
	preview: boolean;
	appId: string;
	name: string;
	/** The URL scheme older builds registered; kept only to remove that registration (index.ts). */
	protocol: string;
	port: number;
	protectedPort: number;
	allowAttach: boolean;
	updates: boolean;
}

export function desktopIdentity(packaged: boolean, metadata: Record<string, unknown>): DesktopIdentity {
	const preview = packaged && metadata.shuffleDesktopChannel === 'preview';
	return {
		preview,
		appId: preview ? 'com.hobbycoders.aishuffle.preview' : 'com.hobbycoders.aishuffle.desktop',
		name: preview ? 'AI Shuffle Preview' : 'AI Shuffle',
		protocol: preview ? 'aishuffle-preview' : 'aishuffle',
		port: 8000,
		protectedPort: 8001,
		allowAttach: !preview,
		updates: !preview
	};
}

/** Mirrors the backend's local-mode defaults; preview never uses these paths. */
export function stableDataDir(platform: NodeJS.Platform, env: NodeJS.ProcessEnv, home: string): string {
	if (env.DATA_DIR) return env.DATA_DIR;
	if (platform === 'win32') return path.join(env.USERPROFILE || home, '.ai-shuffle');
	if (platform === 'darwin') return path.join(home, 'Library', 'Application Support', 'ai-shuffle');
	return path.join(env.XDG_DATA_HOME || path.join(home, '.local', 'share'), 'ai-shuffle');
}
