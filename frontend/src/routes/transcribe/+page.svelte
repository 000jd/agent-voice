<script lang="ts">
	import FileMusic from '@lucide/svelte/icons/file-music';
	import EmptyState from '#lib/components/empty-state.svelte';
	import PageHeader from '#lib/components/page-header.svelte';
</script>

<PageHeader
	title="Transcribe"
	description="Speech in, text out. The CLI, MCP and REST transcription paths already work; this drop zone is intentionally not implemented in the scaffold."
/>

<EmptyState
	icon={FileMusic}
	title="Transcription UI is not wired yet"
	description="Use `agent-voice transcribe <file>` or POST /v1/agent/transcribe. A later release will add drag-and-drop, language hints, hotwords and timed exports here."
/>
