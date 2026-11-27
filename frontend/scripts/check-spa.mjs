import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize, resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..', 'build');
const types = {
	'.html': 'text/html; charset=utf-8',
	'.js': 'text/javascript; charset=utf-8',
	'.css': 'text/css; charset=utf-8',
	'.json': 'application/json; charset=utf-8',
	'.svg': 'image/svg+xml',
	'.woff2': 'font/woff2'
};

async function exists(path) {
	try {
		return (await stat(path)).isFile();
	} catch {
		return false;
	}
}

const server = createServer(async (request, response) => {
	const url = new URL(request.url ?? '/', 'http://127.0.0.1');
	let pathname = url.pathname;
	if (pathname === '/ui') {
		response.writeHead(301, { Location: '/ui/' }).end();
		return;
	}
	if (!pathname.startsWith('/ui/')) {
		response.writeHead(404).end('not found');
		return;
	}
	pathname = pathname.slice(3);
	if (pathname.startsWith('/_app/') && !(await exists(join(root, normalize(pathname))))) {
		response.writeHead(404).end('not found');
		return;
	}
	const file = pathname === '/' ? join(root, '200.html') : join(root, normalize(pathname));
	if (await exists(file)) {
		response.writeHead(200, { 'Content-Type': types[extname(file)] ?? 'application/octet-stream' });
		response.end(await readFile(file));
		return;
	}
	response.writeHead(200, { 'Content-Type': types['.html'] });
	response.end(await readFile(join(root, '200.html')));
});

await new Promise((resolveListen) => server.listen(0, '127.0.0.1', resolveListen));
const address = server.address();
assert.ok(address && typeof address === 'object', 'server did not bind');
const origin = `http://127.0.0.1:${address.port}`;

try {
	const checks = [
		['/ui/', 200, 'text/html'],
		['/ui/speak', 200, 'text/html'],
		['/ui/voices', 200, 'text/html'],
		['/ui/_app/version.json', 200, 'application/json'],
		['/ui/_app/missing.js', 404, null]
	];
	for (const [path, status, contentType] of checks) {
		const response = await fetch(`${origin}${path}`);
		assert.equal(response.status, status, `${path} returned ${response.status}`);
		if (contentType) {
			assert.match(response.headers.get('content-type') ?? '', new RegExp(contentType));
		}
	}
	console.log(`SPA fallback contract OK (${checks.length} checks).`);
} finally {
	server.close();
}
