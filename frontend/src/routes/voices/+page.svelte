<script lang="ts">
	import Mic from '@lucide/svelte/icons/mic';
	import EmptyState from '#lib/components/empty-state.svelte';
	import PageHeader from '#lib/components/page-header.svelte';
	import { Input } from '#lib/components/ui/input/index.js';

	let query = $state('');

	const description = $derived(
		query
			? `No voices match “${query}”. Voice management is not wired in this scaffold.`
			: 'Voice cloning and VoiceDesign are available through the daemon and MCP today; this screen will list them in a later release.'
	);
</script>

<PageHeader
	title="Voices"
	description="Cloned and designed voices, with their reference clips and provenance. Nothing is loaded until you use one."
/>

<div class="mb-4 max-w-sm">
	<Input bind:value={query} placeholder="Search voices" aria-label="Search voices" />
</div>

<EmptyState icon={Mic} title="No voices to show yet" {description} />
