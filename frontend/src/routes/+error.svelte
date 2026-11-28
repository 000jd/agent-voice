<script lang="ts">
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

	const status = $derived(page.status);
</script>

<svelte:head>
	<title>{status} · agent-voice</title>
</svelte:head>

<div class="flex min-h-dvh items-center justify-center bg-canvas p-6 text-ink">
	<div class="max-w-md text-center">
		<span
			class="mx-auto mb-5 flex size-11 items-center justify-center rounded-panel border border-rule bg-surface text-danger"
		>
			<TriangleAlert class="size-5" aria-hidden="true" />
		</span>
		<p class="metric text-[11px] text-ink-muted">Error {status}</p>
		<h1 class="mt-2 text-[20px] leading-7 font-semibold tracking-[-0.015em]">
			That page could not be loaded
		</h1>
		<p class="mt-2 text-[12px] leading-5 text-ink-secondary">
			The route may not exist in this build, or the static fallback could not resolve it.
		</p>
		<div class="mt-6">
			<a class="btn btn-primary" href={resolve('speak')}>Open Speak</a>
		</div>
	</div>
</div>
