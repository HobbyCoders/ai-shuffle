export interface SourceProfile {
  format: 1; isolated: true; port: number; protectedPort: number;
  debugPort?: number; feedDirectory?: string; publicKey?: string;
}
export const { readProfile, localFeed } = require('../../scripts/source-profile.cjs') as {
  readProfile(root: string): SourceProfile | null;
  localFeed(profile: SourceProfile | null): typeof fetch | undefined;
};
