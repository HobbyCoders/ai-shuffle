// Second part of chat-position-stability.cases.mjs: every case not named by parts 1 and 3.
import { runJourney } from './runner.mjs';
import { secondPart } from './lib/split.mjs';
import { FIRST_PART, THIRD_PART, journey } from './chat-position-stability.cases.mjs';

await runJourney(secondPart(journey, [...FIRST_PART, ...THIRD_PART]));
