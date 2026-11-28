/** Background jobs: poll, cancel, and wait for a terminal state. */
import { api, unwrap } from './client.js';
import { parseJob, parseJobsPayload } from './guards.js';
import type { Job, JobStatus } from './types.js';

const TERMINAL: readonly JobStatus[] = ['succeeded', 'failed', 'cancelled'];

export async function getJob(jobId: string): Promise<Job> {
	const result = (await api['/v1/jobs/{job_id}'].GET({
		params: { path: { job_id: jobId } }
	})) as Parameters<typeof unwrap>[0];
	return parseJob(unwrap(result, `/v1/jobs/${jobId}`));
}

export async function listJobs(query: { limit?: number; kind?: string } = {}): Promise<Job[]> {
	const params: Record<string, string | number> = {};
	if (query.limit) params.limit = query.limit;
	if (query.kind) params.kind = query.kind;
	const result = (await api['/v1/jobs'].GET({
		params: { query: params }
	})) as Parameters<typeof unwrap>[0];
	return parseJobsPayload(unwrap(result, '/v1/jobs'));
}

export async function cancelJob(jobId: string): Promise<Job> {
	const result = (await api['/v1/jobs/{job_id}/cancel'].POST({
		params: { path: { job_id: jobId } }
	})) as Parameters<typeof unwrap>[0];
	return parseJob(unwrap(result, `/v1/jobs/${jobId}/cancel`));
}

export function isTerminal(job: Job): boolean {
	return TERMINAL.includes(job.status);
}

/**
 * Poll fallback for callers that cannot hold an SSE connection open (a tab that
 * was suspended, or a job started before the stream connected).
 */
export async function waitForJob(
	jobId: string,
	options: {
		intervalMs?: number;
		timeoutMs?: number;
		signal?: AbortSignal;
		onUpdate?: (job: Job) => void;
	} = {}
): Promise<Job> {
	const interval = options.intervalMs ?? 700;
	const deadline = Date.now() + (options.timeoutMs ?? 10 * 60 * 1000);
	while (Date.now() < deadline) {
		if (options.signal?.aborted) break;
		const job = await getJob(jobId);
		options.onUpdate?.(job);
		if (isTerminal(job)) return job;
		await new Promise<void>((resolve) => {
			const timer = setTimeout(resolve, interval);
			options.signal?.addEventListener(
				'abort',
				() => {
					clearTimeout(timer);
					resolve();
				},
				{ once: true }
			);
		});
	}
	return getJob(jobId);
}
