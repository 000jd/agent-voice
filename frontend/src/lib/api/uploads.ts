/**
 * Multipart uploads with real progress and cancellation.
 *
 * XMLHttpRequest is used deliberately: `fetch` still cannot report upload
 * progress, and a 40 MB reference clip needs a determinate bar.
 */
import { ApiError, currentSessionKey, reportUnauthorized } from './client.js';
import { ApiShapeError, parseErrorPayload } from './guards.js';

export type UploadTask<T> = {
	promise: Promise<T>;
	abort: () => void;
};

export type UploadOptions<T> = {
	onProgress?: (ratio: number) => void;
	parse: (value: unknown) => T;
	signal?: AbortSignal;
};

function parseJsonBody(text: string): unknown {
	if (!text) return null;
	try {
		return JSON.parse(text) as unknown;
	} catch {
		return text;
	}
}

export function uploadForm<T>(
	path: string,
	form: FormData,
	options: UploadOptions<T>
): UploadTask<T> {
	const xhr = new XMLHttpRequest();
	const promise = new Promise<T>((resolve, reject) => {
		xhr.open('POST', path);
		xhr.responseType = 'text';
		const key = currentSessionKey();
		if (key) xhr.setRequestHeader('Authorization', `Bearer ${key}`);
		xhr.setRequestHeader('Accept', 'application/json');

		xhr.upload.onprogress = (event) => {
			if (event.lengthComputable && event.total > 0) {
				options.onProgress?.(Math.min(1, event.loaded / event.total));
			}
		};

		xhr.onload = () => {
			if (xhr.status === 401) reportUnauthorized();
			const body = parseJsonBody(xhr.responseText);
			if (xhr.status >= 200 && xhr.status < 300) {
				try {
					resolve(options.parse(body));
				} catch (error) {
					reject(
						error instanceof ApiShapeError
							? error
							: new ApiShapeError(path, 'could not parse the upload response.')
					);
				}
				return;
			}
			reject(new ApiError(parseErrorPayload(body, xhr.status)));
		};

		xhr.onerror = () => {
			reject(
				new ApiError({
					status: 0,
					code: 'NETWORK_ERROR',
					message: 'The upload could not reach the daemon.',
					suggestion: 'Check that `agent-voice serve` is running, then retry.',
					correlationId: ''
				})
			);
		};

		xhr.onabort = () => {
			reject(
				new ApiError({
					status: 0,
					code: 'ABORTED',
					message: 'Upload cancelled.',
					suggestion: '',
					correlationId: ''
				})
			);
		};

		options.signal?.addEventListener('abort', () => xhr.abort(), { once: true });
		xhr.send(form);
	});

	return { promise, abort: () => xhr.abort() };
}
