<script lang="ts">
	import ErrorNotice from '#lib/components/common/error-notice.svelte';
	import { formatClock, formatElapsed } from '#lib/format.js';
	import { speak, type SpeakPhase } from '#lib/stores/speak.svelte.js';

	const labels: Record<SpeakPhase, string> = {
		idle: 'Ready',
		queued: 'Queued',
		loading: 'Loading',
		generating: 'Generating',
		done: 'Done',
		failed: 'Failed',
		cancelled: 'Cancelled'
	};

	const chipClass = $derived(
		speak.phase === 'failed'
			? 'chip-danger'
			: speak.phase === 'done'
				? 'chip-success'
				: speak.phase === 'idle'
					? ''
					: 'chip-signal'
	);
</script>

{#if speak.phase !== 'idle'}
	<div
		class="rounded-control border border-rule bg-surface px-3 py-2"
		aria-live="polite"
		aria-atomic="true"
	>
		<div class="flex flex-wrap items-center gap-2">
			<span class={`chip ${chipClass}`}>{labels[speak.phase]}</span>
			<span class="min-w-0 flex-1 truncate text-[12px] text-ink-secondary">
				{speak.jobMessage || (speak.busy ? 'Waiting for the daemon…' : '')}
			</span>
			{#if speak.busy}
				<span class="metric text-[11px] text-ink-muted">{formatElapsed(speak.elapsedMs)}</span>
				<button type="button" class="btn btn-sm btn-ghost" onclick={() => void speak.cancel()}>
					Cancel
				</button>
			{:else if speak.result}
				<span class="metric text-[11px] text-ink-muted">
					{formatClock(speak.result.durationSec)} · {speak.result.engineId}
					{#if speak.result.cached}
						· cache hit
					{/if}
				</span>
			{/if}
		</div>

		{#if speak.busy}
			<div class={`progress-track mt-2 ${speak.jobProgress > 0 ? '' : 'indeterminate'}`}>
				{#if speak.jobProgress > 0}
					<div class="progress-fill" style={`width: ${Math.round(speak.jobProgress * 100)}%`}></div>
				{/if}
			</div>
		{/if}

		{#if speak.error}
			<div class="mt-2">
				<ErrorNotice error={speak.error} compact />
			</div>
		{/if}
	</div>
{/if}
