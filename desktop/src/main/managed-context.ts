import type { SourceApplication, SourceUpdates } from './source-updates';
import type { SourceProfile } from './source-profile';
import type { InstalledHost } from './installed-host';
export interface ManagedContext { updates: SourceUpdates; application: SourceApplication; root: string; profile?: SourceProfile | null; host?: InstalledHost | null }
let current: ManagedContext | null = null;
export function setManagedContext(context: ManagedContext): void { current = context; }
export function managedContext(): ManagedContext | null { return current; }
