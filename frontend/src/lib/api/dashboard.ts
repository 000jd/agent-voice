import { api } from './client.js';

type JsonRecord = Record<string, unknown>;

export type HealthSummary = {
	status: string;
	version: string;
	protocolVersion: number;
	torchLoaded: boolean;
};

export type EngineModel = {
	alias: string;
	qualityTier: string;
	downloaded: boolean;
	loaded: boolean;
};

export type EngineSummary = {
	id: string;
	name: string;
	backend: string;
	backendVersion: string;
	installed: boolean;
	kind: 'tts' | 'asr';
	device: string;
	resolvedDevice: string | null;
	models: EngineModel[];
	whyUnavailable: string | null;
};

export type DashboardData = {
	health: HealthSummary;
	engines: EngineSummary[];
};

function asRecord(value: unknown): JsonRecord | null {
	return typeof value === 'object' && value !== null && !Array.isArray(value)
		? (value as JsonRecord)
		: null;
}

function stringField(record: JsonRecord, key: string): string | null {
	const value = record[key];
	return typeof value === 'string' ? value : null;
}

function booleanField(record: JsonRecord, key: string): boolean {
	return record[key] === true;
}

function numberField(record: JsonRecord, key: string): number | null {
	const value = record[key];
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function parseModel(value: unknown): EngineModel {
	const record = asRecord(value);
	const alias = record ? stringField(record, 'alias') : null;
	if (!record || !alias) {
		throw new Error('The daemon returned a model without an alias.');
	}
	return {
		alias,
		qualityTier: stringField(record, 'quality_tier') ?? 'unknown',
		downloaded: booleanField(record, 'downloaded_locally'),
		loaded: booleanField(record, 'loaded')
	};
}

function parseEngine(value: unknown): EngineSummary {
	const record = asRecord(value);
	const id = record ? stringField(record, 'engine_id') : null;
	if (!record || !id) {
		throw new Error('The daemon returned an engine without an engine_id.');
	}
	const kind = record.kind === 'asr' ? 'asr' : 'tts';
	const models = Array.isArray(record.models) ? record.models.map(parseModel) : [];
	return {
		id,
		name: stringField(record, 'display_name') ?? id,
		backend: stringField(record, 'backend_name') ?? 'unknown',
		backendVersion: stringField(record, 'backend_version') ?? '',
		installed: booleanField(record, 'installed'),
		kind,
		device: stringField(record, 'device') ?? 'unknown',
		resolvedDevice: stringField(record, 'resolved_device'),
		models,
		whyUnavailable: stringField(record, 'why_unavailable')
	};
}

function parseHealth(value: unknown): HealthSummary {
	const record = asRecord(value);
	if (!record) {
		throw new Error('The daemon returned an invalid /health response.');
	}
	const status = stringField(record, 'status');
	const version = stringField(record, 'version');
	const protocolVersion = numberField(record, 'protocol_version');
	if (!status || !version || protocolVersion === null) {
		throw new Error('The daemon returned an incomplete /health response.');
	}
	return {
		status,
		version,
		protocolVersion,
		torchLoaded: booleanField(record, 'torch_loaded')
	};
}

function parseEngines(value: unknown): EngineSummary[] {
	const record = asRecord(value);
	if (!record || !Array.isArray(record.engines)) {
		throw new Error('The daemon returned an invalid /v1/engines response.');
	}
	return record.engines.map(parseEngine);
}

function describeFailure(status: number, error: unknown): string {
	const record = asRecord(error);
	const message = record ? stringField(record, 'message') : null;
	const suggestion = record ? stringField(record, 'suggestion') : null;
	const base = message ?? `Request failed with HTTP ${status}.`;
	return suggestion ? `${base} ${suggestion}` : base;
}

type RawResult = {
	data?: unknown;
	error?: unknown;
	response: Response;
};

async function getHealth(): Promise<unknown> {
	const result = (await api['/health'].GET()) as RawResult;
	if (result.error || result.data === undefined) {
		throw new Error(describeFailure(result.response.status, result.error));
	}
	return result.data;
}

async function getEngines(): Promise<unknown> {
	const result = (await api['/v1/engines'].GET()) as RawResult;
	if (result.error || result.data === undefined) {
		throw new Error(describeFailure(result.response.status, result.error));
	}
	return result.data;
}

export async function loadDashboard(): Promise<DashboardData> {
	try {
		const [health, engines] = await Promise.all([getHealth(), getEngines()]);
		return {
			health: parseHealth(health),
			engines: parseEngines(engines)
		};
	} catch (error) {
		if (error instanceof TypeError) {
			throw new Error(
				'The daemon is not reachable. Start it with `agent-voice serve`, then refresh.',
				{ cause: error }
			);
		}
		throw error;
	}
}
