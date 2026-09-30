import type { MessageBoxOptions } from 'electron';
import type { Updater } from './updater';

type UpdateActions = Pick<Updater, 'current' | 'check' | 'download' | 'install'>;
type ShowDialog = (options: MessageBoxOptions) => Promise<{ response: number }>;

/** Native menu checks must report a result even when the web UI is hidden or stale. */
export class ManualUpdateCheck {
  active = false;
  constructor(private readonly updater: UpdateActions, private readonly show: ShowDialog) {}

  async run(): Promise<void> {
    if (this.active) return;
    this.active = true;
    try {
      await this.updater.check(true);
      const state = { ...this.updater.current };
      const label = state.release || state.version || 'the new version';
      let message: string;
      let detail = state.message || '';
      let action: 'download' | 'install' | null = null;
      if (state.error) {
        message = 'Could not complete the update check';
        detail = state.error;
      } else if (state.phase === 'available') {
        message = `AI Shuffle ${label} is available`;
        detail ||= 'Download and prepare this update? Nothing will restart until you choose to install it.';
        action = 'download';
      } else if (state.phase === 'ready') {
        message = 'An update is ready to install';
        detail = `AI Shuffle ${label} has been prepared. Restart to finish installing it.`;
        action = 'install';
      } else if (state.phase === 'checking') {
        message = 'An update check is already running';
      } else if (state.phase === 'downloading') {
        message = 'An update is being prepared';
      } else if (state.phase === 'unsupported') {
        message = 'Updates are not supported by this app configuration';
      } else if (detail || !state.checkedAt) {
        message = 'Update checking is unavailable';
        detail ||= 'Updates are not available in this app configuration.';
      } else {
        message = 'AI Shuffle is up to date';
        detail = 'No newer update is available on your selected update channel.';
      }
      const { response } = await this.show({
        type: state.error ? 'warning' : 'info', title: 'Check for updates', message, detail,
        buttons: action ? [action === 'download' ? 'Download update' : 'Restart and update', 'Later'] : ['OK'],
        defaultId: action ? 1 : 0, cancelId: action ? 1 : 0,
      });
      // A heartbeat may have changed the phase while the dialog was open.
      if (response === 0 && action && this.updater.current.phase === state.phase && this.updater.current.version === state.version) {
        await this.updater[action]();
      }
    } finally {
      this.active = false;
    }
  }
}
