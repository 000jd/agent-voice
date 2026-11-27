<script lang="ts">
	import History from '@lucide/svelte/icons/history';
	import EmptyState from '#lib/components/empty-state.svelte';
	import PageHeader from '#lib/components/page-header.svelte';
</script>

<PageHeader
	title="History"
	description="Recent renders, transcripts and dub jobs. Jobs are durable on disk; this timeline will read them in a later release."
/>

<EmptyState
	icon={History}
	title="No history to show yet"
	description="The daemon keeps job records under its data directory. The UI-boundary release will list them here without changing the headless CLI or MCP behaviour."
/>
