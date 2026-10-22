// Observe the real test execution, never a filtered or skip-only collection.
import { tap } from 'node:test/reporters';
import { writeFileSync } from 'node:fs';
import path from 'node:path';

export default async function* reporter(source) {
  async function* inventory() {
    const files = new Set();
    for await (const event of source) {
      if (['test:pass', 'test:fail'].includes(event.type) && event.data.file) {
        files.add(path.relative(process.cwd(), event.data.file).split(path.sep).join('/'));
      }
      yield event;
    }
    writeFileSync(process.env.DESKTOP_INVENTORY, JSON.stringify([...files].sort()) + '\n');
  }
  yield* tap(inventory());
}
