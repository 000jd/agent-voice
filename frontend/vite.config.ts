import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig, loadEnv } from 'vite';

const DEV_ORIGIN = 'http://127.0.0.1:8000';

export default defineConfig(({ mode }) => {
	const env = loadEnv(mode, process.cwd(), '');
	const devKey = (env.AGENT_VOICE_DEV_API_KEY ?? process.env.AGENT_VOICE_DEV_API_KEY ?? '').trim();
	const proxyHeaders: Record<string, string> = {
		Origin: DEV_ORIGIN
	};
	if (devKey) {
		proxyHeaders.Authorization = `Bearer ${devKey}`;
	}
	const proxy = {
		'/health': { target: DEV_ORIGIN, changeOrigin: true, headers: proxyHeaders },
		'/v1': { target: DEV_ORIGIN, changeOrigin: true, headers: proxyHeaders, ws: true },
		'/audio': { target: DEV_ORIGIN, changeOrigin: true, headers: proxyHeaders }
	};

	return {
		plugins: [
			tailwindcss(),
			sveltekit({
				compilerOptions: {
					runes: ({ filename }) =>
						filename.split(/[/\\]/).includes('node_modules') ? undefined : true
				},
				adapter: adapter({
					pages: 'build',
					assets: 'build',
					fallback: '200.html',
					precompress: false,
					strict: true
				}),
				paths: {
					base: '/ui',
					relative: false
				}
			})
		],
		server: {
			host: '127.0.0.1',
			port: 5173,
			strictPort: true,
			proxy
		},
		ssr: {
			noExternal: ['bits-ui']
		}
	};
});
