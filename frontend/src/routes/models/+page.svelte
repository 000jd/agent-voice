<script lang="ts">
	import Boxes from '@lucide/svelte/icons/boxes';
	import EmptyState from '#lib/components/empty-state.svelte';
	import PageHeader from '#lib/components/page-header.svelte';
</script>

<PageHeader
	title="Models"
	description="Checkpoints, modes and device compatibility. The dashboard already lists every engine and its models; installing and switching devices is a later screen."
/>

<EmptyState
	icon={Boxes}
	title="Model management is read-only for now"
	description="Use the dashboard for the live inventory, or the CLI for installs: agent-voice engines install <id>. The daemon never downloads a model just because a page was opened."
/>
