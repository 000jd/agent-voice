<script lang="ts">
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import { Button } from '#lib/components/ui/button/index.js';

	const status = $derived(page.status);
</script>

<svelte:head>
	<title>{status} · agent-voice</title>
</svelte:head>

<div class="flex min-h-dvh items-center justify-center bg-background p-6 text-foreground">
	<div class="max-w-md text-center">
		<span
			class="mx-auto mb-5 flex size-12 items-center justify-center rounded-full bg-destructive/10 text-destructive"
		>
			<TriangleAlert class="size-6" aria-hidden="true" />
		</span>
		<p class="text-xs font-medium tracking-widest text-muted-foreground uppercase">
			Error {status}
		</p>
		<h1 class="mt-2 text-2xl font-semibold tracking-tight">That page could not be loaded</h1>
		<p class="mt-2 text-sm text-muted-foreground">
			The route may not exist in this build, or the static fallback could not resolve it.
		</p>
		<div class="mt-6">
			<Button href={resolve('/')} variant="outline">Back to the dashboard</Button>
		</div>
	</div>
</div>
