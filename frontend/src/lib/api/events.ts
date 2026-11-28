/**
 * Server-sent events over `fetch`.
 *
 * `EventSource` cannot send an `Authorization` header, so the Studio parses the
 * `/v1/events` stream itself. The parser is pure and unit-tested; the transport
 * reconnects with `Last-Event-ID` and a bounded backoff.
 */
import { apiFetch } from './client.js';
import type { EventConnection } from './types.js';

export type SseMessage = {
	id: string;
	event: string;
	data: string;
};

/** Incremental SSE parser: feed decoded text, get complete messages back. */
export class SseParser {
	private buffer = '';

	push(chunk: string): SseMessage[] {
		this.buffer += chunk.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
		const messages: SseMessage[] = [];
		let boundary = this.buffer.indexOf('\n\n');
		while (boundary !== -1) {
			const block = this.buffer.slice(0, boundary);
			this.buffer = this.buffer.slice(boundary + 2);
			const message = parseBlock(block);
			if (message) messages.push(message);
			boundary = this.buffer.indexOf('\n\n');
		}
		return messages;
	}

	reset(): void {
		this.buffer = '';
	}
}

function parseBlock(block: string): SseMessage | null {
	if (!block.trim()) return null;
	let id = '';
	let event = 'message';
	const data: string[] = [];
	for (const line of block.split('\n')) {
		if (!line || line.startsWith(':')) continue;
		const colon = line.indexOf(':');
		const field = colon === -1 ? line : line.slice(0, colon);
		const raw = colon === -1 ? '' : line.slice(colon + 1);
		const value = raw.startsWith(' ') ? raw.slice(1) : raw;
		switch (field) {
			case 'id':
				id = value;
				break;
			case 'event':
				event = value || 'message';
				break;
			case 'data':
				data.push(value);
				break;
			default:
				break;
		}
	}
	if (data.length === 0) return null;
	return { id, event, data: data.join('\n') };
}

export type EventStreamOptions = {
	onEvent: (message: SseMessage) => void;
	onState: (state: EventConnection) => void;
	lastEventId?: string | null;
	minRetryMs?: number;
	maxRetryMs?: number;
};

export type EventStreamHandle = {
	close: () => void;
	lastEventId: () => string | null;
};

export function openEventStream(options: EventStreamOptions): EventStreamHandle {
	const controller = new AbortController();
	let lastId = options.lastEventId ?? null;
	let closed = false;
	let attempt = 0;
	const minRetry = options.minRetryMs ?? 800;
	const maxRetry = options.maxRetryMs ?? 8000;

	function sleep(ms: number): Promise<void> {
		return new Promise((resolve) => {
			const timer = setTimeout(resolve, ms);
			controller.signal.addEventListener(
				'abort',
				() => {
					clearTimeout(timer);
					resolve();
				},
				{ once: true }
			);
		});
	}

	async function run(): Promise<void> {
		while (!closed) {
			options.onState('connecting');
			try {
				const headers: Record<string, string> = { Accept: 'text/event-stream' };
				if (lastId) headers['Last-Event-ID'] = lastId;
				const response = await apiFetch('/v1/events', {
					headers,
					signal: controller.signal,
					cache: 'no-store'
				});
				if (!response.ok) throw new Error(`events stream answered ${response.status}`);
				const body = response.body;
				if (!body) throw new Error('events stream had no body');
				options.onState('open');
				attempt = 0;
				const reader = body.getReader();
				const decoder = new TextDecoder();
				const parser = new SseParser();
				for (;;) {
					const { value, done } = await reader.read();
					if (done) break;
					for (const message of parser.push(decoder.decode(value, { stream: true }))) {
						if (message.id) lastId = message.id;
						options.onEvent(message);
					}
				}
			} catch {
				if (closed || controller.signal.aborted) return;
			}
			if (closed) return;
			options.onState('closed');
			attempt += 1;
			await sleep(Math.min(maxRetry, minRetry * 2 ** Math.min(attempt, 4)));
		}
	}

	void run();

	return {
		close: () => {
			closed = true;
			controller.abort();
			options.onState('idle');
		},
		lastEventId: () => lastId
	};
}
