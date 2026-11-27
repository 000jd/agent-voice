<script lang="ts">
	import Lock from '@lucide/svelte/icons/lock';
	import PageHeader from '#lib/components/page-header.svelte';
	import * as Card from '#lib/components/ui/card/index.js';
</script>

<PageHeader
	title="Settings"
	description="Runtime configuration. Security-sensitive settings are read-only from the UI by design."
/>

<Card.Root class="max-w-2xl">
	<Card.Header>
		<Card.Title class="flex items-center gap-2">
			<Lock class="size-4 text-muted-foreground" aria-hidden="true" />
			Managed by the daemon
		</Card.Title>
		<Card.Description>
			The UI is one more REST client. It does not own the daemon's configuration and cannot widen
			the guard.
		</Card.Description>
	</Card.Header>
	<Card.Content>
		<dl class="divide-y text-sm">
			<div class="flex items-center justify-between gap-4 py-3">
				<dt class="text-muted-foreground">Bind address</dt>
				<dd class="font-mono text-xs">AGENT_VOICE_HOST</dd>
			</div>
			<div class="flex items-center justify-between gap-4 py-3">
				<dt class="text-muted-foreground">Authentication</dt>
				<dd class="font-mono text-xs">AGENT_VOICE_API_KEY</dd>
			</div>
			<div class="flex items-center justify-between gap-4 py-3">
				<dt class="text-muted-foreground">Engine mutation</dt>
				<dd class="font-mono text-xs">ALLOW_AGENT_ENGINE_MUTATION</dd>
			</div>
			<div class="flex items-center justify-between gap-4 py-3">
				<dt class="text-muted-foreground">Clone-path confinement</dt>
				<dd class="font-mono text-xs">AGENT_VOICE_ALLOW_ANY_PATH</dd>
			</div>
		</dl>
		<p class="mt-4 text-xs text-muted-foreground">
			Edit the daemon's <code>.env</code> or environment, then restart it. A future release may expose
			safe, non-security settings; the Tier-A list above will stay read-only.
		</p>
	</Card.Content>
</Card.Root>
