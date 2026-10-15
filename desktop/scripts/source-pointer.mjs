// Shared activation boundary. No signing, network setup or upload on import.
// Direct stable publishing leaves expectedRevision undefined (legacy behavior).
// Whole-release promotion supplies a freshly verified stable revision or null
// for an empty lane; edge always requires a verified revision.
export function publishSourcePointer(wrangler, { revision, channel, version, assetId, size, signed,
 sequence, repository, repositoryId, expectedRevision }) {
 const quote = value => `'${value.replaceAll("'", "''")}'`;
 let forwardGuard = '';
 if (channel !== 'stable' || expectedRevision !== undefined) {
  if (!['edge', 'stable'].includes(channel) ||
      !(channel === 'stable' && expectedRevision === null) && !/^[a-f0-9]{40}$/.test(expectedRevision ?? '')) {
   throw Error('Missing verified publication baseline');
  }
  const latest = `SELECT revision,sequence FROM source_releases WHERE channel=${quote(channel)} ORDER BY sequence DESC LIMIT 1`;
  const pointer = JSON.parse(wrangler('--command', latest + ';', '--json')).flatMap(item => item.results ?? [])[0];
  if (expectedRevision === null) {
   if (pointer) throw Error('Published pointer changed or feed is stale; retry selection');
   forwardGuard = ` AND NOT EXISTS (${latest})`;
  } else {
   if (pointer?.revision !== expectedRevision || !Number.isSafeInteger(pointer?.sequence)) throw Error('Published pointer changed or feed is stale; retry selection');
   forwardGuard = ` AND (SELECT revision FROM (${latest}))=${quote(expectedRevision)} AND (SELECT sequence FROM (${latest}))=${pointer.sequence}`;
  }
 }
 const insert = `INSERT INTO source_releases(revision,channel,version,asset_id,size,manifest,sequence,release_repository,release_repository_id) SELECT ${quote(revision)},${quote(channel)},${quote(version)},${assetId},${size},${quote(signed)},${sequence},${quote(repository)},${repositoryId} WHERE ${sequence} > COALESCE((SELECT MAX(sequence) FROM source_releases),0)${forwardGuard} ON CONFLICT(revision,channel) DO NOTHING;`;
 // Use the query API: bulk --file imports emit non-JSON progress and pause D1.
 wrangler('--command', insert);
 const result = JSON.parse(wrangler('--command', `SELECT manifest,asset_id,release_repository,release_repository_id FROM source_releases WHERE revision=${quote(revision)} AND channel=${quote(channel)};`, '--json'));
 const row = result.flatMap(item => item.results ?? [])[0];
 if (row?.manifest !== signed || row?.asset_id !== assetId || row?.release_repository !== repository || row?.release_repository_id !== repositoryId) throw new Error('Source pointer read-back did not match');
}
