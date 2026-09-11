import { promises as fs, createWriteStream } from 'node:fs';
import { pipeline } from 'node:stream/promises';
import path from 'node:path';
import type { Readable } from 'node:stream';
const yauzl = require('yauzl');

/** Extract ordinary files/directories only, before executing any source code. */
export async function extractSource(archive: string, destination: string): Promise<void> {
  await fs.mkdir(destination, { recursive: false });
  const zip = await new Promise<any>((resolve, reject) => yauzl.open(archive, { lazyEntries: true, strictFileNames: true, validateEntrySizes: true }, (error: Error | null, zip: unknown) => error ? reject(error) : resolve(zip)));
  const names = new Set<string>();
  let count = 0, expanded = 0;
  try {
    await new Promise<void>((resolve, reject) => {
      zip.on('error', reject); zip.on('end', resolve);
      zip.on('entry', (entry: { fileName: string; uncompressedSize: number; externalFileAttributes: number; generalPurposeBitFlag: number }) => {
        void (async () => {
          const name = entry.fileName;
          const parts = name.replace(/\/$/, '').split('/');
          const mode = (entry.externalFileAttributes >>> 16) & 0xf000;
          if (++count > 100_000 || (expanded += entry.uncompressedSize) > 2 * 1024 ** 3) throw new Error('Source archive exceeds limits');
          if (!parts.length || parts.some(p => !p || p === '.' || p === '..' || /[\\:<>"|?*\x00-\x1f]/.test(p) || /[ .]$/.test(p) || /^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)/i.test(p))) throw new Error('Unsafe source archive path');
          const key = parts.join('/').toLowerCase();
          if (names.has(key) || ![0, 0x8000, 0x4000].includes(mode) || (entry.generalPurposeBitFlag & 1)) throw new Error('Duplicate, encrypted or special source archive entry');
          names.add(key);
          const target = path.join(destination, ...parts);
          if (name.endsWith('/')) await fs.mkdir(target, { recursive: true });
          else {
            await fs.mkdir(path.dirname(target), { recursive: true });
            const input = await new Promise<Readable>((resolve, reject) => zip.openReadStream(entry, (error: Error | null, stream: Readable) => error ? reject(error) : resolve(stream)));
            await pipeline(input, createWriteStream(target, { flags: 'wx' }));
          }
          zip.readEntry();
        })().catch(reject);
      });
      zip.readEntry();
    });
  } finally { zip.close(); }
}
