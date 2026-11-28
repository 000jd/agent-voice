/**
 * Library: persistent `/v1/history` and `/v1/captures` in one list with filters,
 * search, favourite, replay, rerender and delete.
 */
import { describeApiError } from '#lib/api/client.js';
import { deleteCapture, listCaptures } from '#lib/api/captures.js';
import {
	deleteHistoryItem,
	favoriteHistoryItem,
	listHistory,
	rerenderHistoryItem
} from '#lib/api/history.js';
import type { ApiErrorPayload, CaptureSummary, HistoryItem, HistoryKind } from '#lib/api/types.js';
import { clipFromCapture, clipFromHistory } from '#lib/clips.js';
import { getPlayer } from '#lib/stores/player.svelte.js';
import { studio } from '#lib/stores/studio.svelte.js';
import { toasts } from '#lib/stores/toasts.svelte.js';

export const LIBRARY_FILTERS = [
	{ id: 'all', label: 'All' },
	{ id: 'speak', label: 'Speech' },
	{ id: 'script', label: 'Scripts' },
	{ id: 'design', label: 'Designs' },
	{ id: 'transcribe', label: 'Transcriptions' },
	{ id: 'favorite', label: 'Favourites' }
] as const;

export type LibraryFilter = (typeof LIBRARY_FILTERS)[number]['id'];

export type LibraryRow =
	| { kind: 'history'; id: string; createdAt: number; item: HistoryItem }
	| { kind: 'capture'; id: string; createdAt: number; capture: CaptureSummary };

const PAGE_SIZE = 50;

class LibraryStore {
	filter = $state<LibraryFilter>('all');
	query = $state('');
	items = $state<HistoryItem[]>([]);
	captures = $state<CaptureSummary[]>([]);
	loading = $state(true);
	error = $state<ApiErrorPayload | null>(null);
	historyCursor = $state<string | null>(null);
	captureCursor = $state<string | null>(null);
	busyId = $state<string | null>(null);

	get rows(): LibraryRow[] {
		const showCaptures = this.filter === 'all' || this.filter === 'transcribe';
		const rows: LibraryRow[] = this.items.map((item) => ({
			kind: 'history' as const,
			id: `history:${item.id}`,
			createdAt: item.createdAt,
			item
		}));
		if (showCaptures) {
			for (const capture of this.captures) {
				rows.push({
					kind: 'capture',
					id: `capture:${capture.id}`,
					createdAt: capture.createdAt,
					capture
				});
			}
		}
		return rows.sort((left, right) => right.createdAt - left.createdAt);
	}

	get hasMore(): boolean {
		return this.historyCursor !== null || this.captureCursor !== null;
	}

	get isEmpty(): boolean {
		return !this.loading && this.rows.length === 0;
	}

	private queryParams(): { kind: HistoryKind | null; favorite: boolean } {
		if (this.filter === 'favorite') return { kind: null, favorite: true };
		if (this.filter === 'all') return { kind: null, favorite: false };
		return { kind: this.filter, favorite: false };
	}

	async refresh(): Promise<void> {
		this.loading = true;
		this.error = null;
		const { kind, favorite } = this.queryParams();
		try {
			const [history, captures] = await Promise.all([
				listHistory({
					limit: PAGE_SIZE,
					kind,
					favorite: favorite ? true : null,
					q: this.query
				}),
				listCaptures({ limit: PAGE_SIZE, q: this.query })
			]);
			this.items = history.items;
			this.historyCursor = history.nextCursor;
			this.captures = captures.items;
			this.captureCursor = captures.nextCursor;
		} catch (error) {
			this.error = describeApiError(error);
		} finally {
			this.loading = false;
		}
	}

	async loadMore(): Promise<void> {
		const { kind, favorite } = this.queryParams();
		try {
			if (this.historyCursor) {
				const page = await listHistory({
					limit: PAGE_SIZE,
					cursor: this.historyCursor,
					kind,
					favorite: favorite ? true : null,
					q: this.query
				});
				this.items = [...this.items, ...page.items];
				this.historyCursor = page.nextCursor;
			}
			if (this.captureCursor) {
				const page = await listCaptures({
					limit: PAGE_SIZE,
					cursor: this.captureCursor,
					q: this.query
				});
				this.captures = [...this.captures, ...page.items];
				this.captureCursor = page.nextCursor;
			}
		} catch (error) {
			this.error = describeApiError(error);
		}
	}

	setFilter(filter: LibraryFilter): void {
		this.filter = filter;
		void this.refresh();
	}

	async search(query: string): Promise<void> {
		this.query = query;
		await this.refresh();
	}

	async toggleFavorite(item: HistoryItem): Promise<void> {
		this.busyId = item.id;
		try {
			const updated = await favoriteHistoryItem(item.id);
			this.items = this.items.map((entry) => (entry.id === updated.id ? updated : entry));
		} catch (error) {
			toasts.error('Could not update the favourite', describeApiError(error).message);
		} finally {
			this.busyId = null;
		}
	}

	async removeHistory(item: HistoryItem): Promise<void> {
		this.busyId = item.id;
		try {
			await deleteHistoryItem(item.id);
			this.items = this.items.filter((entry) => entry.id !== item.id);
		} catch (error) {
			toasts.error('Could not delete the entry', describeApiError(error).message);
		} finally {
			this.busyId = null;
		}
	}

	async removeCapture(capture: CaptureSummary): Promise<void> {
		this.busyId = capture.id;
		try {
			await deleteCapture(capture.id);
			this.captures = this.captures.filter((entry) => entry.id !== capture.id);
		} catch (error) {
			toasts.error('Could not delete the capture', describeApiError(error).message);
		} finally {
			this.busyId = null;
		}
	}

	async rerender(item: HistoryItem): Promise<void> {
		this.busyId = item.id;
		try {
			const started = await rerenderHistoryItem(item.id, { take: (item.take ?? 1) + 1 });
			toasts.info('Rerender queued', `Job ${started.jobId} — the new take appears here.`);
			window.setTimeout(() => void this.refresh(), 2500);
		} catch (error) {
			toasts.error('Could not rerender', describeApiError(error).message);
		} finally {
			this.busyId = null;
		}
	}

	playHistory(item: HistoryItem): void {
		const clip = clipFromHistory(item);
		if (!clip) {
			toasts.info('No audio stored', 'This entry has no artifact to play.');
			return;
		}
		void getPlayer().play(clip);
	}

	playCapture(capture: CaptureSummary): void {
		void getPlayer().play(clipFromCapture(capture));
	}

	/** Called after a clone/transcribe mutation elsewhere. */
	invalidate(): void {
		void this.refresh();
		void studio.refreshCatalog();
	}
}

export const library = new LibraryStore();
