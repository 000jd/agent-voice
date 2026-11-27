<script lang="ts">
	import AudioLines from '@lucide/svelte/icons/audio-lines';
	import PageHeader from '#lib/components/page-header.svelte';
	import { Button } from '#lib/components/ui/button/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import { Input } from '#lib/components/ui/input/index.js';

	let text = $state('');
</script>

<PageHeader
	title="Speak"
	description="The synthesis screen is not wired in this scaffold. The REST and MCP surfaces are unchanged and fully usable today."
/>

<Card.Root class="max-w-2xl">
	<Card.Header>
		<Card.Title>Text to speech</Card.Title>
		<Card.Description>
			This placeholder proves the form primitives and the typed client boundary. It does not call
			the daemon.
		</Card.Description>
	</Card.Header>
	<Card.Content class="space-y-4">
		<div class="space-y-2">
			<label class="text-sm font-medium" for="speak-text">Text</label>
			<Input id="speak-text" bind:value={text} placeholder="Type a sentence to synthesize" />
		</div>
		<Button disabled>
			<AudioLines class="size-4" aria-hidden="true" />
			Synthesize
		</Button>
		<p class="text-xs text-muted-foreground">
			The enabled version will call <code>POST /v1/agent/speak</code> and play the artifact from
			<code>/audio/&lt;id&gt;</code>.
		</p>
	</Card.Content>
</Card.Root>
