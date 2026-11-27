import { afterEach, expect, test, vi } from 'vitest';
import { transcribeFile } from '#lib/api/studio.js';

const result = {
	text: 'A synthetic line.',
	language: 'English',
	duration_sec: 1,
	segments_count: 1,
	segments: [{ index: 0, start_sec: 0, end_sec: 1, text: 'A synthetic line.', words: [] }],
	engine_id: 'mock_asr',
	model_alias: 'mock-asr-parity',
	backend_name: 'mock-asr',
	backend_version: '1',
	elapsed_ms: 12,
	realtime_multiple: 80,
	truncated: false,
	segments_omitted: 0,
	transcript_json_path: null,
	srt_path: null,
	vtt_path: null
};

afterEach(() => {
	vi.unstubAllGlobals();
});

test('uploads the multipart fields the OpenAI endpoint accepts', async () => {
	const fetchMock = vi.fn().mockResolvedValue(
		new Response(JSON.stringify(result), {
			status: 200,
			headers: { 'Content-Type': 'application/json' }
		})
	);
	vi.stubGlobal('fetch', fetchMock);
	const file = new File(['wav'], 'clip.wav', { type: 'audio/wav' });

	const parsed = await transcribeFile(file, {
		model: 'mock_asr:mock-asr-parity',
		language: 'auto',
		hotwords: 'CUDA, Manim'
	});

	expect(parsed.text).toBe('A synthetic line.');
	expect(fetchMock).toHaveBeenCalledTimes(1);
	const call = fetchMock.mock.calls[0];
	expect(call?.[0]).toBe('/v1/audio/transcriptions');
	const init = call?.[1] as RequestInit;
	const form = init.body as FormData;
	expect(form.get('model')).toBe('mock_asr:mock-asr-parity');
	expect(form.get('response_format')).toBe('verbose_json');
	expect(form.get('prompt')).toBe('CUDA, Manim');
	expect(form.get('language')).toBeNull();
	expect(form.get('file')).toBeInstanceOf(File);
});

test('surfaces an error envelope as ApiError', async () => {
	const fetchMock = vi.fn().mockResolvedValue(
		new Response(
			JSON.stringify({
				error_code: 'UNAUTHORIZED',
				message: 'key required',
				suggestion: 'set AGENT_VOICE_API_KEY'
			}),
			{ status: 401, headers: { 'Content-Type': 'application/json' } }
		)
	);
	vi.stubGlobal('fetch', fetchMock);
	await expect(
		transcribeFile(new File(['wav'], 'clip.wav'), { model: 'mock_asr:mock-asr-parity' })
	).rejects.toMatchObject({ status: 401, code: 'UNAUTHORIZED' });
});
