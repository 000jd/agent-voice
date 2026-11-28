<script lang="ts">
	import ListMusic from '@lucide/svelte/icons/list-music';
	import Play from '@lucide/svelte/icons/play';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import type { CaptureSummary } from '#lib/api/types.js';
	import { formatClock, relativeTime, shortText } from '#lib/format.js';
	import { library } from '#lib/stores/library.svelte.js';

	let { capture, href }: { capture: CaptureSummary; href: string } = $props();

	const title = $derived(shortText(capture.title || capture.transcript || 'Untitled capture', 96));
</script>

<li class="group flex items-center gap-2 px-2 py-2 md:px-3">
	<button
		type="button"
		class="btn btn-ghost btn-icon shrink-0"
		aria-label={`Play ${capture.title || 'capture'}`}
		onclick={() => library.playCapture(capture)}
	>
		<Play class="size-3.5" aria-hidden="true" />
	</button>
	<a class="min-w-0 flex-1 rounded-[3px]" {href}>
		<p class="truncate text-[13px] text-ink hover:underline">{title}</p>
		<p class="metric truncate text-[11px] text-ink-muted">
			Transcription · {capture.engineId} · {formatClock(capture.durationSec)} ·
			{relativeTime(capture.createdAt)}
		</p>
	</a>
	<span class="chip hidden shrink-0 sm:inline-flex">
		<ListMusic class="size-3" aria-hidden="true" />
		Capture
	</span>
	<div class="flex shrink-0 items-center gap-0.5">
		<a
			class="btn btn-ghost btn-icon"
			{href}
			aria-label="Open capture in Transcribe"
			title="Open in Transcribe"
		>
			<ListMusic class="size-3.5" aria-hidden="true" />
		</a>
		<button
			type="button"
			class="btn btn-ghost btn-icon text-ink-muted hover:text-danger"
			aria-label="Delete capture"
			onclick={() => void library.removeCapture(capture)}
		>
			<Trash2 class="size-3.5" aria-hidden="true" />
		</button>
	</div>
</li>
