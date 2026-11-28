/**
 * The single authenticated HTTP entry point of the Studio.
 *
 * The daemon key lives in `sessionStorage` only (never a URL, never
 * `localStorage`), is attached to every API, upload, audio and SSE request, and
 * is cleared the moment any request answers 401.
 */
import { createPathBasedClient } from 'openapi-fetch';
import type { paths } from './schema.js';
import { ApiShapeError, parseErrorPayload } from './guards.js';
import type { ApiErrorPayload } from './types.js';

export type RawResult = {
	data?: unknown;
	error?: unknown;
	response: Response;
};

const SESSION_STORAGE_KEY = 'agent-voice-session-key';

export class ApiError extends Error {
	readonly status: number;
	readonly code: string;
	readonly suggestion: string;
	readonly correlationId: string;

	constructor(payload: ApiErrorPayload) {
		super(payload.message);
		this.name = 'ApiError';
		this.status = payload.status;
		this.code = payload.code;
		this.suggestion = payload.suggestion;
		this.correlationId = payload.correlationId;
	}
}

let sessionKey: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

function handleUnauthorized(): void {
	unauthorizedHandler?.();
}

export function setUnauthorizedHandler(handler: (() => void) | null): void {
	unauthorizedHandler = handler;
}

export function setSessionKey(value: string | null): void {
	const next = value?.trim() ? value.trim() : null;
	sessionKey = next;
	if (typeof sessionStorage === 'undefined') return;
	try {
		if (next) sessionStorage.setItem(SESSION_STORAGE_KEY, next);
		else sessionStorage.removeItem(SESSION_STORAGE_KEY);
	} catch {
		// A blocked storage backend must not break the session; the in-memory key
		// still works for this page.
	}
}

export function loadSessionKey(): string | null {
	if (typeof sessionStorage === 'undefined') return null;
	try {
		const stored = sessionStorage.getItem(SESSION_STORAGE_KEY);
		return stored && stored.trim() ? stored.trim() : null;
	} catch {
		return null;
	}
}

export function hasSessionKey(): boolean {
	return sessionKey !== null;
}

export function currentSessionKey(): string | null {
	return sessionKey;
}

/** Called by transports that cannot use `apiFetch` (XHR uploads). */
export function reportUnauthorized(): void {
	handleUnauthorized();
}

export function authHeaders(): Record<string, string> {
	return sessionKey ? { Authorization: `Bearer ${sessionKey}` } : {};
}

/**
 * `openapi-fetch` accepts a custom fetch. It is the one place the bearer (and
 * the 401 side effect) is applied to generated-endpoint calls.
 */
async function authedFetch(input: Request): Promise<Response> {
	if (sessionKey && !input.headers.has('Authorization')) {
		try {
			input.headers.set('Authorization', `Bearer ${sessionKey}`);
		} catch {
			const retry = new Request(input, { headers: new Headers(input.headers) });
			retry.headers.set('Authorization', `Bearer ${sessionKey}`);
			const response = await fetch(retry);
			if (response.status === 401) handleUnauthorized();
			return response;
		}
	}
	const response = await fetch(input);
	if (response.status === 401) handleUnauthorized();
	return response;
}

export const api = createPathBasedClient<paths>({ baseUrl: '', fetch: authedFetch });

/** Authenticated fetch for streams, audio, uploads and anything not in the schema. */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
	const headers = new Headers(init.headers);
	headers.set('Accept', headers.get('Accept') ?? 'application/json');
	if (sessionKey) headers.set('Authorization', `Bearer ${sessionKey}`);
	const response = await fetch(path, {
		...init,
		headers,
		credentials: 'same-origin'
	});
	if (response.status === 401) handleUnauthorized();
	return response;
}

async function readJson(response: Response): Promise<unknown> {
	const text = await response.text();
	if (!text) return null;
	try {
		return JSON.parse(text) as unknown;
	} catch {
		return text;
	}
}

export async function readJsonBody(response: Response): Promise<unknown> {
	return readJson(response);
}

/** Authenticated JSON request that throws `ApiError` on any non-2xx response. */
export async function requestJson<T>(
	path: string,
	init: RequestInit,
	parse: (value: unknown) => T
): Promise<T> {
	const response = await apiFetch(path, init);
	const body = await readJson(response);
	if (!response.ok) throw new ApiError(parseErrorPayload(body, response.status));
	try {
		return parse(body);
	} catch (error) {
		if (error instanceof ApiShapeError) throw error;
		throw new ApiShapeError(path, 'could not parse the response body.');
	}
}

/** Authenticated GET that returns the raw response (exports, audio, text). */
export async function requestBlob(path: string): Promise<Blob> {
	const response = await apiFetch(path, { headers: { Accept: '*/*' } });
	if (!response.ok) {
		throw new ApiError(parseErrorPayload(await readJson(response), response.status));
	}
	return response.blob();
}

export async function requestText(path: string): Promise<string> {
	const response = await apiFetch(path, { headers: { Accept: 'text/plain, */*' } });
	if (!response.ok) {
		throw new ApiError(parseErrorPayload(await readJson(response), response.status));
	}
	return response.text();
}

export function unwrap(result: RawResult, _endpoint: string): unknown {
	void _endpoint;
	if (result.response.status === 401) handleUnauthorized();
	if (result.error !== undefined || result.data === undefined) {
		throw new ApiError(parseErrorPayload(result.error, result.response.status));
	}
	return result.data;
}

export function describeApiError(error: unknown): ApiErrorPayload {
	if (error instanceof ApiError) {
		return {
			status: error.status,
			code: error.code,
			message: error.message,
			suggestion: error.suggestion,
			correlationId: error.correlationId
		};
	}
	if (error instanceof ApiShapeError) {
		return {
			status: 0,
			code: 'BAD_RESPONSE',
			message: error.message,
			suggestion: '',
			correlationId: ''
		};
	}
	return {
		status: 0,
		code: 'CLIENT_ERROR',
		message: error instanceof Error ? error.message : 'The daemon returned an unknown error.',
		suggestion: '',
		correlationId: ''
	};
}

export function errorText(error: unknown): string {
	const payload = describeApiError(error);
	const parts = [payload.message];
	if (payload.suggestion) parts.push(payload.suggestion);
	if (payload.correlationId) parts.push(`(ref ${payload.correlationId})`);
	return parts.join(' ');
}
