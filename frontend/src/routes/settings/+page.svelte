<script lang="ts">
	import Lock from '@lucide/svelte/icons/lock';
	import PageHeader from '#lib/components/page-header.svelte';
	import SegmentedControl from '#lib/components/studio/segmented-control.svelte';
	import StatusBadge from '#lib/components/studio/status-badge.svelte';
	import * as Card from '#lib/components/ui/card/index.js';
	import { formatGpuLabel, formatVram } from '#lib/format.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import { theme, type ThemeMode } from '#lib/theme.svelte.js';

	const settings = [
		['AGENT_VOICE_HOST', 'bind address'],
		['AGENT_VOICE_API_KEY', 'bearer key for /v1, /audio, /mcp'],
		['AGENT_VOICE_ALLOWED_HOSTS', 'extra Host names'],
		['AGENT_VOICE_ALLOW_ANY_PATH', 'clone/transcribe path confinement'],
		['ALLOW_AGENT_ENGINE_MUTATION', 'agent-authored plugin execution'],
		['REQUIRE_ENGINE_APPROVAL', 'human promotion gate'],
		['AGENT_VOICE_TOOLSET', 'MCP surface'],
		['AGENT_VOICE_HOME', 'install home'],
		['AGENT_VOICE_DATA_DIR', 'voices, cache, artifacts, inbox'],
		['AGENT_VOICE_ENGINES_DIR', 'plugin directory']
	];
</script>

<PageHeader
	title="Settings"
	description="Runtime state and the security settings the UI deliberately cannot edit."
/>

<div class="grid gap-3 xl:grid-cols-2">
	<Card.Root>
		<Card.Header>
			<Card.Title>Appearance</Card.Title>
			<Card.Description>Dark is the studio default. Light uses the same geometry.</Card.Description>
		</Card.Header>
		<Card.Content class="max-w-xs">
			<SegmentedControl
				options={[
					{ value: 'dark', label: 'Dark' },
					{ value: 'light', label: 'Light' }
				]}
				value={theme.mode}
				onchange={(next: ThemeMode) => theme.set(next)}
				label="Theme"
			/>
		</Card.Content>
	</Card.Root>

	<Card.Root>
		<Card.Header>
			<Card.Title>Connection</Card.Title>
			<Card.Description>The UI talks to this daemon origin only.</Card.Description>
		</Card.Header>
		<Card.Content class="space-y-2 font-mono text-[11px] text-text-secondary tabular-nums">
			<div class="flex items-center justify-between gap-3">
				<span class="text-text-muted">status</span>
				<StatusBadge
					tone={studio.connection === 'connected'
						? 'success'
						: studio.connection === 'offline'
							? 'danger'
							: 'neutral'}
					label={studio.connection === 'connected'
						? 'Connected'
						: studio.connection === 'offline'
							? 'Offline'
							: 'Checking'}
				/>
			</div>
			<div class="flex items-center justify-between gap-3">
				<span class="text-text-muted">version / protocol</span>
				<span>{studio.health?.version ?? '—'} / {studio.health?.protocolVersion ?? '—'}</span>
			</div>
			<div class="flex items-center justify-between gap-3">
				<span class="text-text-muted">gpu</span>
				<span>{formatGpuLabel(studio.gpu)}</span>
			</div>
			<div class="flex items-center justify-between gap-3">
				<span class="text-text-muted">workers</span>
				<span>{Object.keys(studio.gpu?.workers ?? {}).length}</span>
			</div>
			{#if studio.gpu?.vramFreeMb !== null && studio.gpu?.vramFreeMb !== undefined}
				<div class="flex items-center justify-between gap-3">
					<span class="text-text-muted">vram free</span>
					<span>{formatVram(studio.gpu.vramFreeMb)}</span>
				</div>
			{/if}
		</Card.Content>
	</Card.Root>
</div>

<Card.Root class="mt-3">
	<Card.Header>
		<Card.Title class="flex items-center gap-2">
			<Lock class="size-4 text-text-muted" aria-hidden="true" />
			Managed by the daemon
		</Card.Title>
		<Card.Description>
			Tier-A settings are read-only here. The UI is one more REST client and cannot widen the
			daemon's guard.
		</Card.Description>
	</Card.Header>
	<Card.Content>
		<dl class="divide-y divide-border text-[12px]">
			{#each settings as [key, description] (key)}
				<div
					class="flex flex-col gap-0.5 py-2.5 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
				>
					<dt class="font-mono text-[10px] text-text-muted">{key}</dt>
					<dd class="text-text-secondary">{description}</dd>
				</div>
			{/each}
		</dl>
	</Card.Content>
</Card.Root>

<Card.Root class="mt-3">
	<Card.Header>
		<Card.Title>Audio key</Card.Title>
		<Card.Description>
			The static UI does not store AGENT_VOICE_API_KEY and has no login. In development, Vite reads
			AGENT_VOICE_DEV_API_KEY and injects it server-side for /health, /v1 and /audio.
		</Card.Description>
	</Card.Header>
	<Card.Content>
		<p class="text-[11px] leading-5 text-text-secondary">
			On a keyed deployment, the docked player will report the 401 instead of pretending it can
			play. This is deliberate: a browser asset request cannot safely carry a secret.
		</p>
	</Card.Content>
</Card.Root>
