/** Durable generation history: list, favourite, rerender, delete. */
import { api, unwrap } from './client.js';
import { parseHistoryItem, parseHistoryPage, parseJobStarted } from './guards.js';
import type { HistoryItem, HistoryKind, HistoryPage, JobStarted } from './types.js';

export type HistoryQuery = {
	limit?: number;
	cursor?: string | null;
	kind?: HistoryKind | null;
	voice?: string | null;
	favorite?: boolean | null;
	q?: string | null;
};

export async function listHistory(query: HistoryQuery = {}): Promise<HistoryPage> {
	const params: Record<string, string | number | boolean> = {};
	if (query.limit) params.limit = query.limit;
	if (query.cursor) params.cursor = query.cursor;
	if (query.kind) params.kind = query.kind;
	if (query.voice) params.voice = query.voice;
	if (query.favorite) params.favorite = true;
	if (query.q?.trim()) params.q = query.q.trim();
	const result = (await api['/v1/history'].GET({
		params: { query: params }
	})) as Parameters<typeof unwrap>[0];
	return parseHistoryPage(unwrap(result, '/v1/history'));
}

export async function getHistoryItem(entryId: string): Promise<HistoryItem> {
	const result = (await api['/v1/history/{entry_id}'].GET({
		params: { path: { entry_id: entryId } }
	})) as Parameters<typeof unwrap>[0];
	return parseHistoryItem(unwrap(result, `/v1/history/${entryId}`));
}

export async function deleteHistoryItem(entryId: string): Promise<void> {
	const result = (await api['/v1/history/{entry_id}'].DELETE({
		params: { path: { entry_id: entryId } }
	})) as Parameters<typeof unwrap>[0];
	unwrap(result, `/v1/history/${entryId}`);
}

export async function favoriteHistoryItem(entryId: string): Promise<HistoryItem> {
	const result = (await api['/v1/history/{entry_id}/favorite'].POST({
		params: { path: { entry_id: entryId } }
	})) as Parameters<typeof unwrap>[0];
	return parseHistoryItem(unwrap(result, `/v1/history/${entryId}/favorite`));
}

export async function rerenderHistoryItem(
	entryId: string,
	options: { take?: number | null; seed?: number | null } = {}
): Promise<JobStarted> {
	const result = (await api['/v1/history/{entry_id}/rerender'].POST({
		params: { path: { entry_id: entryId } },
		body: { take: options.take ?? null, seed: options.seed ?? null }
	})) as Parameters<typeof unwrap>[0];
	return parseJobStarted(unwrap(result, `/v1/history/${entryId}/rerender`));
}
