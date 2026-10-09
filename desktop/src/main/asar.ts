/**
 * The small part of Electron's asar format the launcher swap needs (#1821 D5):
 * write an archive from files on disk, and read one member back. Same layout
 * and integrity fields as @electron/asar 3.x (test/asar.test.mjs reads ours
 * with it), so Electron loads it whether or not an integrity fuse is on.
 *
 * Inside Electron, plain `fs` treats `*.asar` paths as directories; callers
 * pass Electron's `original-fs` (rawFs()) so these functions see the file.
 */
import { createHash } from 'node:crypto';
import type * as FS from 'node:fs';

const BLOCK_SIZE = 4 * 1024 * 1024;
type Directory = { files: Record<string, Entry> };
type Entry = Directory | { size: number; offset: string; integrity: { algorithm: 'SHA256'; hash: string; blockSize: number; blocks: string[] } };
export interface AsarMember { name: string; data: Buffer }

/** Electron's unpatched fs inside Electron, node's fs elsewhere (tests). */
export function rawFs(): typeof FS {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  return process.versions.electron ? require('original-fs') : require('node:fs');
}

const sha256 = (data: Buffer) => createHash('sha256').update(data).digest('hex');
function integrity(data: Buffer) {
  const blocks: string[] = [];
  for (let start = 0; ; start += BLOCK_SIZE) {
    blocks.push(sha256(data.subarray(start, Math.min(start + BLOCK_SIZE, data.length))));
    if (start + BLOCK_SIZE > data.length) break;
  }
  return { algorithm: 'SHA256' as const, hash: sha256(data), blockSize: BLOCK_SIZE, blocks };
}

/** Archive bytes for members named with forward slashes, in the given order. */
export function packAsar(members: AsarMember[]): Buffer {
  const root: Directory = { files: {} };
  let offset = 0;
  for (const { name, data } of members) {
    const parts = name.split('/');
    if (!name || parts.some(part => !part || part === '.' || part === '..' || part.includes('\\'))) throw new Error(`Invalid archive member ${name}`);
    let directory = root;
    for (const part of parts.slice(0, -1)) {
      const next = (directory.files[part] ??= { files: {} });
      if (!('files' in next)) throw new Error(`Archive member ${name} is inside a file`);
      directory = next;
    }
    const file = parts.at(-1)!;
    if (directory.files[file]) throw new Error(`Duplicate archive member ${name}`);
    directory.files[file] = { size: data.length, offset: String(offset), integrity: integrity(data) };
    offset += data.length;
  }
  const json = Buffer.from(JSON.stringify(root));
  const padded = Math.ceil(json.length / 4) * 4;
  // Chromium Pickle framing: header = [payload size][string length][json, padded to 4].
  const header = Buffer.alloc(8 + padded);
  header.writeUInt32LE(4 + padded, 0); header.writeInt32LE(json.length, 4); json.copy(header, 8);
  const size = Buffer.alloc(8);
  size.writeUInt32LE(4, 0); size.writeUInt32LE(header.length, 4);
  return Buffer.concat([size, header, ...members.map(member => member.data)]);
}

function header(fs: typeof FS, file: string): { root: Directory; base: number; fd: number } {
  const fd = fs.openSync(file, 'r');
  try {
    const size = Buffer.alloc(8);
    if (fs.readSync(fd, size, 0, 8, 0) !== 8 || size.readUInt32LE(0) !== 4) throw new Error('Not an asar archive');
    const length = size.readUInt32LE(4);
    if (length < 8 || length > 64 * 1024 * 1024) throw new Error('Not an asar archive');
    const buffer = Buffer.alloc(length);
    if (fs.readSync(fd, buffer, 0, length, 8) !== length) throw new Error('Truncated asar archive');
    const json = buffer.readInt32LE(4);
    if (json < 0 || json > length - 8) throw new Error('Not an asar archive');
    return { root: JSON.parse(buffer.subarray(8, 8 + json).toString('utf8')), base: 8 + length, fd };
  } catch (error) { fs.closeSync(fd); throw error; }
}

/** One member's bytes, checked against the integrity hash the archive records. */
export function readAsarMember(fs: typeof FS, file: string, name: string): Buffer {
  const { root, base, fd } = header(fs, file);
  try {
    let entry: Entry | undefined = root;
    for (const part of name.split('/')) entry = entry && 'files' in entry ? entry.files[part] : undefined;
    if (!entry || 'files' in entry || !Number.isSafeInteger(entry.size) || entry.size < 0) throw new Error(`${name} is not in ${file}`);
    const data = Buffer.alloc(entry.size);
    if (fs.readSync(fd, data, 0, entry.size, base + Number(entry.offset)) !== entry.size) throw new Error('Truncated asar archive');
    if (entry.integrity && entry.integrity.hash !== sha256(data)) throw new Error(`${name} does not match its archive integrity`);
    return data;
  } finally { fs.closeSync(fd); }
}
