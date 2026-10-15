import assert from 'node:assert/strict';
import { test } from 'node:test';
import { publishSourcePointer } from '../scripts/source-pointer.mjs';

const row = { revision: 'b'.repeat(40), channel: 'stable', version: '1.2.3', assetId: 8,
 size: 100, signed: '{"original":"signed bytes"}', sequence: 1000, repository: 'example/app', repositoryId: 42 };
const saved = { manifest: row.signed, asset_id: row.assetId, release_repository: row.repository, release_repository_id: row.repositoryId };

for (const channel of ['edge', 'stable']) test(`${channel} activation CAS binds both revision and sequence`, () => {
 const sql = [];
 const wrangler = (...args) => {
  const statement = args[args.indexOf('--command') + 1]; sql.push(statement);
  if (statement.startsWith('SELECT revision,sequence')) return JSON.stringify([{ results: [{ revision: 'a'.repeat(40), sequence: 900 }] }]);
  if (statement.startsWith('SELECT manifest')) return JSON.stringify([{ results: [saved] }]);
  return '';
 };
 publishSourcePointer(wrangler, { ...row, channel, expectedRevision: 'a'.repeat(40) });
 const insert = sql.find(s => s.startsWith('INSERT'));
 assert(insert.includes(`channel='${channel}'`));
 assert(insert.includes('))=900'));
 assert(insert.includes(`))='${'a'.repeat(40)}'`));
 assert(insert.includes('1000 > COALESCE((SELECT MAX(sequence) FROM source_releases),0)'));
 assert(insert.includes('ON CONFLICT(revision,channel) DO NOTHING'));
 assert(insert.includes(row.signed));
});

test('promotion refuses a changed baseline without writing', () => {
 const wrangler = (...args) => {
  assert(args[args.indexOf('--command') + 1].startsWith('SELECT revision,sequence'));
  return JSON.stringify([{ results: [{ revision: 'c'.repeat(40), sequence: 901 }] }]);
 };
 assert.throws(() => publishSourcePointer(wrangler, { ...row, expectedRevision: 'a'.repeat(40) }), /pointer changed/);
});

test('empty stable lane uses an atomic NOT EXISTS guard', () => {
 let insert;
 const wrangler = (...args) => {
  const sql = args[args.indexOf('--command') + 1];
  if (sql.startsWith('INSERT')) { insert = sql; return ''; }
  return JSON.stringify([{ results: sql.startsWith('SELECT manifest') ? [saved] : [] }]);
 };
 publishSourcePointer(wrangler, { ...row, expectedRevision: null });
 assert(insert.includes("AND NOT EXISTS (SELECT revision,sequence FROM source_releases WHERE channel='stable'"));
});

test('activation refuses a lost CAS even when the write command succeeded', () => {
 const wrangler = (...args) => {
  const sql = args[args.indexOf('--command') + 1];
  if (sql.startsWith('INSERT')) return '';
  return JSON.stringify([{ results: sql.startsWith('SELECT manifest') ? [] : [{ revision: 'a'.repeat(40), sequence: 900 }] }]);
 };
 assert.throws(() => publishSourcePointer(wrangler, { ...row, expectedRevision: 'a'.repeat(40) }), /read-back/);
});

test('default stable publication preserves the pre-staging SQL behavior', () => {
 const queries = [];
 const wrangler = (...args) => {
  const sql = args[args.indexOf('--command') + 1]; queries.push(sql);
  return sql.startsWith('SELECT') ? JSON.stringify([{ results: [saved] }]) : '';
 };
 publishSourcePointer(wrangler, row);
 assert.equal(queries.length, 2);
 assert(queries[0].startsWith('INSERT'));
 assert(!queries[0].includes('NOT EXISTS'));
 assert(!queries[0].includes('SELECT revision,sequence'));
});
