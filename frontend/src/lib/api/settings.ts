/** Settings, diagnostics, lexicon, cache, MCP config — never the API key. */
import { api, unwrap } from './client.js';
import {
	parseCacheClearResult,
	parseCacheStats,
	parseDiagnostics,
	parseLexiconSnapshot,
	parseMcpConfig,
	parseServerInfo,
	parseSettingsSnapshot
} from './guards.js';
import type {
	CacheClearResult,
	CacheStats,
	Diagnostics,
	LexiconSnapshot,
	ServerInfo,
	SettingsSnapshot
} from './types.js';

export async function getSettings(): Promise<SettingsSnapshot> {
	const result = (await api['/v1/settings'].GET()) as Parameters<typeof unwrap>[0];
	return parseSettingsSnapshot(unwrap(result, '/v1/settings'));
}

export async function patchSettings(values: Record<string, unknown>): Promise<SettingsSnapshot> {
	const result = (await api['/v1/settings'].PATCH({
		body: { values }
	})) as Parameters<typeof unwrap>[0];
	return parseSettingsSnapshot(unwrap(result, '/v1/settings'));
}

export async function getDiagnostics(): Promise<Diagnostics> {
	const result = (await api['/v1/diagnostics'].GET()) as Parameters<typeof unwrap>[0];
	return parseDiagnostics(unwrap(result, '/v1/diagnostics'));
}

export async function getServerInfo(): Promise<ServerInfo> {
	const result = (await api['/v1/server_info'].GET()) as Parameters<typeof unwrap>[0];
	return parseServerInfo(unwrap(result, '/v1/server_info'));
}

export async function getLexicon(
	options: {
		project?: string | null;
		voiceId?: string | null;
	} = {}
): Promise<LexiconSnapshot> {
	const query: Record<string, string> = {};
	if (options.project) query.project = options.project;
	if (options.voiceId) query.voice_id = options.voiceId;
	const result = (await api['/v1/lexicon'].GET({
		params: { query }
	})) as Parameters<typeof unwrap>[0];
	return parseLexiconSnapshot(unwrap(result, '/v1/lexicon'));
}

export async function putLexicon(entry: {
	word: string;
	replacement: string;
	scope: 'global' | 'project' | 'voice';
	project?: string | null;
	voiceId?: string | null;
}): Promise<LexiconSnapshot> {
	const result = (await api['/v1/lexicon'].PUT({
		body: {
			word: entry.word,
			replacement: entry.replacement,
			scope: entry.scope,
			project: entry.project ?? null,
			voice_id: entry.voiceId ?? null
		}
	})) as Parameters<typeof unwrap>[0];
	return parseLexiconSnapshot(unwrap(result, '/v1/lexicon'));
}

export async function deleteLexiconEntry(entry: {
	word: string;
	scope?: 'global' | 'project' | 'voice';
	project?: string | null;
	voiceId?: string | null;
}): Promise<void> {
	const result = (await api['/v1/lexicon'].DELETE({
		params: {
			query: {
				word: entry.word,
				scope: entry.scope ?? 'global',
				...(entry.project ? { project: entry.project } : {}),
				...(entry.voiceId ? { voice_id: entry.voiceId } : {})
			}
		}
	})) as Parameters<typeof unwrap>[0];
	unwrap(result, '/v1/lexicon');
}

export async function getCacheStats(): Promise<CacheStats> {
	const result = (await api['/v1/cache'].GET()) as Parameters<typeof unwrap>[0];
	return parseCacheStats(unwrap(result, '/v1/cache'));
}

export async function clearCache(): Promise<CacheClearResult> {
	const result = (await api['/v1/cache'].DELETE()) as Parameters<typeof unwrap>[0];
	return parseCacheClearResult(unwrap(result, '/v1/cache'));
}

export type McpClient = 'claude-code' | 'claude-desktop' | 'command-code' | 'cursor' | 'vscode';

export async function getMcpConfig(client: McpClient): Promise<Record<string, unknown>> {
	const result = (await api['/v1/mcp-config'].GET({
		params: { query: { client } }
	})) as Parameters<typeof unwrap>[0];
	return parseMcpConfig(unwrap(result, '/v1/mcp-config'));
}
