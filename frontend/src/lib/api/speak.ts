/** Synthesis and voice design. Speak is job-based so progress is observable. */
import { api, unwrap } from './client.js';
import { parseDesignResult, parseJobStarted, parseSynthesisResult } from './guards.js';
import type { components } from './schema.js';
import type { DesignResult, Job, JobStarted, SynthesisResult, SpeakRequest } from './types.js';

type WireSpeak = components['schemas']['SpeakRequest'];
type WireDesign = components['schemas']['DesignVoiceRequest'];

export function toWireSpeak(request: SpeakRequest): WireSpeak {
	return {
		text: request.text,
		voice: request.voice ?? null,
		speaker: request.speaker ?? null,
		instruct: request.instruct,
		engine: request.engine ?? null,
		model: request.model ?? null,
		quality: request.quality,
		language: request.language,
		format: request.format,
		seed: request.seed ?? null,
		speed: request.speed,
		take: request.take ?? null,
		markup: request.markup,
		spell_identifiers: request.spell_identifiers,
		paragraph_gap_ms: request.paragraph_gap_ms,
		project: request.project ?? null
	};
}

export async function startSpeakJob(request: SpeakRequest): Promise<JobStarted> {
	const result = (await api['/v1/agent/speak/jobs'].POST({
		body: toWireSpeak(request)
	})) as Parameters<typeof unwrap>[0];
	return parseJobStarted(unwrap(result, '/v1/agent/speak/jobs'));
}

export async function synthesize(request: SpeakRequest): Promise<SynthesisResult> {
	const result = (await api['/v1/agent/speak'].POST({
		body: toWireSpeak(request)
	})) as Parameters<typeof unwrap>[0];
	return parseSynthesisResult(unwrap(result, '/v1/agent/speak'));
}

export function synthesisFromJob(job: Job): SynthesisResult {
	const result = job.result;
	if (!result) throw new Error('The job finished without a result.');
	return parseSynthesisResult(result);
}

export type DesignInput = {
	name: string;
	instruct: string;
	sampleText: string;
	language: string;
	saveAsClone: boolean;
	candidates: number;
	take: number | null;
	seed: number | null;
	engine: string | null;
	model: string | null;
	quality: 'high' | 'fast';
	overwrite: boolean;
};

export function toWireDesign(input: DesignInput): WireDesign {
	return {
		name: input.name,
		instruct: input.instruct,
		sample_text: input.sampleText,
		language: input.language,
		save_as_clone: input.saveAsClone,
		candidates: input.candidates,
		take: input.take,
		seed: input.seed,
		overwrite: input.overwrite,
		engine: input.engine,
		model: input.model,
		quality: input.quality
	};
}

export async function designVoice(input: DesignInput): Promise<DesignResult> {
	const result = (await api['/v1/voices/design'].POST({
		body: toWireDesign(input)
	})) as Parameters<typeof unwrap>[0];
	return parseDesignResult(unwrap(result, '/v1/voices/design'));
}
