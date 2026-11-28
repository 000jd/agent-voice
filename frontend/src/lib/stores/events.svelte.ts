/**
 * `/v1/events` subscription.
 *
 * The stream is opened once by the shell and shared; pages subscribe to the
 * messages they care about. A dropped stream reconnects with `Last-Event-ID`
 * and pages keep polling as the fallback, so the UI never freezes on a lost
 * connection.
 */
import { openEventStream, type EventStreamHandle, type SseMessage } from '#lib/api/events.js';
import { parseHistoryItem, parseJob } from '#lib/api/guards.js';
import type { EventConnection, HistoryItem, Job } from '#lib/api/types.js';

export type StudioEvents = {
	onEvent: (message: SseMessage) => void;
};

class EventStore {
	state = $state<EventConnection>('idle');
	lastEventId = $state<string | null>(null);

	private handle: EventStreamHandle | null = null;
	private listeners = new Set<(message: SseMessage) => void>();

	start(): void {
		if (this.handle) return;
		this.handle = openEventStream({
			lastEventId: this.lastEventId,
			onState: (state) => {
				this.state = state;
			},
			onEvent: (message) => {
				if (message.id) this.lastEventId = message.id;
				for (const listener of this.listeners) listener(message);
			}
		});
	}

	stop(): void {
		this.handle?.close();
		this.handle = null;
	}

	subscribe(listener: (message: SseMessage) => void): () => void {
		this.listeners.add(listener);
		return () => {
			this.listeners.delete(listener);
		};
	}
}

export const events = new EventStore();

/** `{ "job": Job }` on `event: job`. Malformed events are ignored, never fatal. */
export function jobFromEvent(message: SseMessage): Job | null {
	if (message.event !== 'job') return null;
	try {
		const payload: unknown = JSON.parse(message.data);
		if (typeof payload !== 'object' || payload === null) return null;
		const job = (payload as Record<string, unknown>).job;
		return parseJob(job);
	} catch {
		return null;
	}
}

/** `{ "item": HistoryItem }` on `event: history`. */
export function historyFromEvent(message: SseMessage): HistoryItem | null {
	if (message.event !== 'history') return null;
	try {
		const payload: unknown = JSON.parse(message.data);
		if (typeof payload !== 'object' || payload === null) return null;
		const item = (payload as Record<string, unknown>).item;
		return parseHistoryItem(item);
	} catch {
		return null;
	}
}
