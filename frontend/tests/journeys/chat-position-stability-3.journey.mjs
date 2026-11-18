// Third part of chat-position-stability.cases.mjs.
import { runJourney } from './runner.mjs';
import { firstPart } from './lib/split.mjs';
import { THIRD_PART, journey } from './chat-position-stability.cases.mjs';

await runJourney(firstPart({ ...journey, name: 'chat-position-stability-3' }, THIRD_PART));
