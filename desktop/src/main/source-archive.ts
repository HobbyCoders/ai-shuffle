import { createHash } from 'node:crypto';
import { promises as fs, createWriteStream } from 'node:fs';
import { pipeline } from 'node:stream/promises';
import path from 'node:path';
import { Transform, type Readable } from 'node:stream';
const yauzl = require('yauzl');

/** A regular file written by extractSource: its archive path and SHA-256. */
export type ExtractedFile = [archivePath: string, sha256: string];

/** Python/code-point ordering, preserved by UTF-8 bytes. Shared by source and UI verification. */
export function treeDigest(files: ExtractedFile[]): string {
  const sorted = files.map(([file, digest]) => [Buffer.from(file, 'utf8'), digest] as const)
    .sort((a, b) => Buffer.compare(a[0], b[0]) || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
  const hash = createHash('sha256');
  for (const [file, digest] of sorted) hash.update(Buffer.concat([file, Buffer.from(`\0${digest}\n`, 'utf8')]));
  return hash.digest('hex');
}

/**
 * Extract ordinary files/directories only, before executing any source code.
 * Returns every regular file with the digest of the bytes actually written,
 * so the caller can check the signed tree digest without reading them again.
 */
export async function extractSource(archive: string, destination: string, limits = { files: 100_000, expanded: 2 * 1024 ** 3 }): Promise<ExtractedFile[]> {
  await fs.mkdir(destination, { recursive: false });
  const zip = await new Promise<any>((resolve, reject) => yauzl.open(archive, { lazyEntries: true, strictFileNames: true, validateEntrySizes: true }, (error: Error | null, zip: unknown) => error ? reject(error) : resolve(zip)));
  const names = new Set<string>();
  const files: ExtractedFile[] = [];
  let count = 0, expanded = 0;
  try {
    await new Promise<void>((resolve, reject) => {
      zip.on('error', reject); zip.on('end', resolve);
      zip.on('entry', (entry: { fileName: string; uncompressedSize: number; externalFileAttributes: number; generalPurposeBitFlag: number }) => {
        void (async () => {
          const name = entry.fileName;
          const parts = name.replace(/\/$/, '').split('/');
          const mode = (entry.externalFileAttributes >>> 16) & 0xf000;
          if (++count > limits.files || (expanded += entry.uncompressedSize) > limits.expanded) throw new Error('Source archive exceeds limits');
          if (!parts.length || parts.some(p => !p || p === '.' || p === '..' || /[\\:<>"|?*\x00-\x1f]/.test(p) || /[ .]$/.test(p) || /^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)/i.test(p))) throw new Error('Unsafe source archive path');
          const key = parts.join('/').toLowerCase();
          if (names.has(key) || ![0, 0x8000, 0x4000].includes(mode) || (entry.generalPurposeBitFlag & 1)) throw new Error('Duplicate, encrypted or special source archive entry');
          names.add(key);
          const target = path.join(destination, ...parts);
          if (name.endsWith('/')) await fs.mkdir(target, { recursive: true });
          else {
            await fs.mkdir(path.dirname(target), { recursive: true });
            const input = await new Promise<Readable>((resolve, reject) => zip.openReadStream(entry, (error: Error | null, stream: Readable) => error ? reject(error) : resolve(stream)));
            const hash = createHash('sha256');
            const digest = new Transform({ transform(chunk, _encoding, done) { hash.update(chunk); done(null, chunk); } });
            await pipeline(input, digest, createWriteStream(target, { flags: 'wx' }));
            files.push([parts.join('/'), hash.digest('hex')]);
          }
          zip.readEntry();
        })().catch(reject);
      });
      zip.readEntry();
    });
  } finally { zip.close(); }
  return files;
}
