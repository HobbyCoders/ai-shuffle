/**
 * A worker whose update only the latest installer can finish: the desktop
 * cannot update itself, or its update needs a newer installer or launcher
 * (`remedy` and `data_kept` from app/core/update_workers.py remedy()). Shared
 * by System > Updates and the sidebar update button; no imports, so the
 * sidebar does not pull in settings state.
 */

/**
 * The downloads page, which serves the installers the hub offers. Not GitHub's
 * latest release: a release can skip a platform's installer, and the hub then
 * keeps serving the previous one (tests/test_public_release_files.py).
 */
export const INSTALLER_URL = 'https://hobbycoders.com/ai-shuffle/downloads';
export const INSTALLER_ACTION = 'Download the latest installer';
export const INSTALLER_LINK = { label: INSTALLER_ACTION, href: INSTALLER_URL };
/** Said only when the launcher kept the previous data (`data_kept`). */
export const DATA_KEPT = 'Your data is kept as it was before the update.';

/**
 * In-app updates keep working, but part of this version (the launcher that
 * starts the app) arrives only with the installer (`installer_recommended`,
 * desktop/src/main/installer-advice.ts).
 */
export const INSTALLER_RECOMMENDED =
	'Updates keep working inside the app, but some improvements in this version (to how the app starts and updates) arrive only with the installer. Run the latest installer when convenient; your chats and settings are kept.';

/** Plain words for a worker that needs or would benefit from the latest installer, or undefined. */
export function installerHint(worker: { remedy?: 'installer' | null; data_kept?: boolean; installer_recommended?: boolean; phase: string }): string | undefined {
	if (worker.remedy !== 'installer') return worker.installer_recommended === true ? INSTALLER_RECOMMENDED : undefined;
	const why = worker.phase === 'unsupported' ? 'This app cannot update itself.' : 'This update cannot finish inside the app.';
	return `${why} Run the latest installer to finish it.${worker.data_kept ? ` ${DATA_KEPT}` : ''}`;
}
