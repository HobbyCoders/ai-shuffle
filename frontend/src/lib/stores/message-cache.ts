/**
 * IndexedDB cache for chat messages.
 * Provides instant message rendering on page refresh by caching the last
 * server-authoritative messages per session. Uses stale-while-revalidate:
 * cached messages render immediately, then get replaced by server data.
 */

import type { ChatMessage } from './types';

const DB_NAME = 'ai-shuffle-cache';
const DB_VERSION = 1;
const STORE_NAME = 'session-messages';
const MAX_ENTRIES = 30;

interface CacheEntry {
	sessionId: string;
	messages: ChatMessage[];
	timestamp: number;
}

let _cachedDB: IDBDatabase | null = null;

function openDB(): Promise<IDBDatabase> {
	// Return cached connection if it's still open
	if (_cachedDB) {
		try {
			// Verify the connection is still usable by checking its name
			// (accessing properties on a closed db throws)
			if (_cachedDB.name === DB_NAME) {
				return Promise.resolve(_cachedDB);
			}
		} catch {
			_cachedDB = null;
		}
	}

	return new Promise((resolve, reject) => {
		const request = indexedDB.open(DB_NAME, DB_VERSION);
		request.onupgradeneeded = () => {
			const db = request.result;
			if (!db.objectStoreNames.contains(STORE_NAME)) {
				db.createObjectStore(STORE_NAME, { keyPath: 'sessionId' });
			}
		};
		request.onsuccess = () => {
			_cachedDB = request.result;
			// Clear the cache if the browser closes the connection
			_cachedDB.onclose = () => { _cachedDB = null; };
			_cachedDB.onversionchange = () => {
				_cachedDB?.close();
				_cachedDB = null;
			};
			resolve(_cachedDB);
		};
		request.onerror = () => reject(request.error);
	});
}

async function withStore<T>(
	mode: IDBTransactionMode,
	fn: (store: IDBObjectStore) => IDBRequest<T>
): Promise<T> {
	const db = await openDB();
	return new Promise((resolve, reject) => {
		const tx = db.transaction(STORE_NAME, mode);
		const store = tx.objectStore(STORE_NAME);
		const request = fn(store);
		request.onsuccess = () => {
			resolve(request.result);
		};
		request.onerror = () => {
			reject(request.error);
		};
	});
}

export const messageCache = {
	async get(sessionId: string): Promise<ChatMessage[] | null> {
		try {
			const entry = await withStore<CacheEntry | undefined>('readonly', (store) =>
				store.get(sessionId)
			);
			return entry?.messages ?? null;
		} catch {
			return null;
		}
	},

	async set(sessionId: string, messages: ChatMessage[]): Promise<void> {
		try {
			const entry: CacheEntry = { sessionId, messages, timestamp: Date.now() };
			await withStore('readwrite', (store) => store.put(entry));
			// Evict old entries in background
			this.evict().catch(() => {});
		} catch {
			// Silently fail — cache is best-effort
		}
	},

	async delete(sessionId: string): Promise<void> {
		try {
			await withStore('readwrite', (store) => store.delete(sessionId));
		} catch {
			// Silently fail
		}
	},

	async evict(): Promise<void> {
		const db = await openDB();
		const tx = db.transaction(STORE_NAME, 'readwrite');
		const store = tx.objectStore(STORE_NAME);

		const entries: CacheEntry[] = await new Promise((resolve, reject) => {
			const request = store.getAll();
			request.onsuccess = () => resolve(request.result);
			request.onerror = () => reject(request.error);
		});

		if (entries.length > MAX_ENTRIES) {
			entries.sort((a, b) => a.timestamp - b.timestamp);
			const toRemove = entries.slice(0, entries.length - MAX_ENTRIES);
			for (const entry of toRemove) {
				store.delete(entry.sessionId);
			}
		}

		await new Promise<void>((resolve, reject) => {
			tx.oncomplete = () => resolve();
			tx.onerror = () => reject(tx.error);
			// NOTE: Don't close the DB here — it's cached in _cachedDB for
			// reuse. Closing it invalidates the cache, causing subsequent
			// operations to silently fail until a new connection is opened.
		});
	}
};
