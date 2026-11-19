// Third part of queue-while-busy.cases.mjs, the queued error and banner probes.
import { runJourney } from './runner.mjs';
import { firstPart } from './lib/split.mjs';
import { THIRD_PART, journey } from './queue-while-busy.cases.mjs';

await runJourney(firstPart({ ...journey, name: 'queue-while-busy-3' }, THIRD_PART));
