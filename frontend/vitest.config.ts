import { svelte } from '@sveltejs/vite-plugin-svelte';
import { svelteTesting } from '@testing-library/svelte/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	plugins: [svelte({ compilerOptions: { runes: true } }), svelteTesting()],
	test: {
		environment: 'happy-dom',
		setupFiles: ['./vitest-setup.ts'],
		include: ['src/**/*.test.ts'],
		restoreMocks: true
	}
});
