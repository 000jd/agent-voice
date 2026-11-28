/** Health, GPU, engine and model discovery plus engine self-management. */
import { api, unwrap } from './client.js';
import {
	parseEnginesPayload,
	parseGpu,
	parseHealth,
	parseJobStarted,
	parseModelsPayload,
	parseUnloadResult
} from './guards.js';
import type {
	EngineSummary,
	GpuStatus,
	HealthSummary,
	JobStarted,
	ModelSummary,
	UnloadResult
} from './types.js';

export async function getHealth(): Promise<HealthSummary> {
	const result = (await api['/health'].GET()) as Parameters<typeof unwrap>[0];
	return parseHealth(unwrap(result, '/health'));
}

export async function getGpu(): Promise<GpuStatus> {
	const result = (await api['/v1/gpu'].GET()) as Parameters<typeof unwrap>[0];
	return parseGpu(unwrap(result, '/v1/gpu'));
}

export async function getEngines(): Promise<EngineSummary[]> {
	const result = (await api['/v1/engines'].GET()) as Parameters<typeof unwrap>[0];
	return parseEnginesPayload(unwrap(result, '/v1/engines'));
}

export async function getModels(engineId?: string): Promise<ModelSummary[]> {
	const result = (await api['/v1/models'].GET({
		params: { query: engineId ? { engine_id: engineId } : {} }
	})) as Parameters<typeof unwrap>[0];
	return parseModelsPayload(unwrap(result, '/v1/models'));
}

export async function unloadModels(): Promise<UnloadResult> {
	const result = (await api['/v1/models/unload'].POST()) as Parameters<typeof unwrap>[0];
	return parseUnloadResult(unwrap(result, '/v1/models/unload'));
}

export async function installEngine(engineId: string, device: string): Promise<JobStarted> {
	const result = (await api['/v1/engines/{engine_id}/install'].POST({
		params: { path: { engine_id: engineId }, query: { device } }
	})) as Parameters<typeof unwrap>[0];
	return parseJobStarted(unwrap(result, `/v1/engines/${engineId}/install`));
}

export async function downloadModel(engineId: string, modelAlias: string): Promise<JobStarted> {
	const result = (await api['/v1/models/{engine_id}/{model_alias}/download'].POST({
		params: { path: { engine_id: engineId, model_alias: modelAlias } }
	})) as Parameters<typeof unwrap>[0];
	return parseJobStarted(unwrap(result, `/v1/models/${engineId}/${modelAlias}/download`));
}
