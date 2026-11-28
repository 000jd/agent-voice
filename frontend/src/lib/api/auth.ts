/** `/v1/auth/check` — the only auth-aware endpoint. */
import { api, unwrap } from './client.js';
import { parseAuthCheck } from './guards.js';
import type { AuthCheck } from './types.js';

export async function checkAuth(): Promise<AuthCheck> {
	const result = await api['/v1/auth/check'].GET();
	return parseAuthCheck(unwrap(result, '/v1/auth/check'));
}

export function isUnauthorized(error: unknown): boolean {
	return typeof error === 'object' && error !== null && 'status' in error && error.status === 401;
}
