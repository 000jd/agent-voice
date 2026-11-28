<script lang="ts">
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	import { cloneVoice, describeApiError } from '#lib/api/studio.js';
	import type { ApiErrorPayload } from '#lib/api/types.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import ErrorNotice from './error-notice.svelte';
	import SlideOver from './slide-over.svelte';
	import SwitchField from './switch-field.svelte';
	import { studio } from '#lib/stores/studio.svelte.js';

	let {
		open,
		onclose,
		onSaved
	}: { open: boolean; onclose: () => void; onSaved?: (voiceId: string) => void } = $props();

	let name = $state('');
	let path = $state('');
	let refText = $state('');
	let xVectorOnly = $state(false);
	let busy = $state(false);
	let error = $state<ApiErrorPayload | string>('');

	async function submit(): Promise<void> {
		if (!name.trim()) {
			error = 'Give the voice a name.';
			return;
		}
		if (!path.trim()) {
			error = 'Enter the reference clip path.';
			return;
		}
		busy = true;
		error = '';
		try {
			const voice = await cloneVoice({
				name: name.trim(),
				ref_audio_path: path.trim(),
				ref_text: refText.trim(),
				x_vector_only: xVectorOnly
			});
			await studio.refreshVoices();
			onSaved?.(voice.id);
			onclose();
		} catch (cause) {
			error = describeApiError(cause);
		} finally {
			busy = false;
		}
	}
</script>

<SlideOver
	{open}
	title="Clone a voice"
	description="Register a reference clip that already lives inside the daemon's inbox."
	{onclose}
>
	<div class="space-y-3.5">
		{#if error}
			<ErrorNotice {error} />
		{/if}

		<div>
			<label for="clone-name" class="mb-1 block text-[11px] text-text-secondary">
				Voice name
			</label>
			<input
				id="clone-name"
				class="h-8 w-full rounded-lg border border-border bg-elevated px-2.5 text-[12px] text-foreground placeholder:text-text-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
				placeholder="narrator"
				bind:value={name}
			/>
		</div>

		<div>
			<label for="clone-path" class="mb-1 block text-[11px] text-text-secondary">
				Reference clip path
			</label>
			<input
				id="clone-path"
				class="h-8 w-full rounded-lg border border-border bg-elevated px-2.5 font-mono text-[11px] text-foreground placeholder:text-text-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
				placeholder="<DATA_DIR>/inbox/clip.wav"
				bind:value={path}
			/>
			<p class="mt-1 text-[10px] leading-4 text-text-muted">
				The daemon confines agent/MCP clone reads to <code
					class="rounded bg-panel px-1 py-0.5 text-[10px]">&lt;DATA_DIR&gt;/inbox/</code
				>. Run <code class="rounded bg-panel px-1 py-0.5 text-[10px]">agent-voice doctor</code> for the
				exact path. This static UI does not guess it.
			</p>
		</div>

		<div>
			<label for="clone-ref-text" class="mb-1 block text-[11px] text-text-secondary">
				Reference transcript <span class="normal-case">(optional)</span>
			</label>
			<textarea
				id="clone-ref-text"
				class="min-h-[64px] w-full resize-y rounded-lg border border-border bg-elevated px-2.5 py-2 text-[12px] leading-5 text-foreground placeholder:text-text-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
				placeholder="What the speaker says in the clip"
				bind:value={refText}></textarea>
		</div>

		<SwitchField
			label="x-vector only"
			description="Use the speaker embedding without a transcript for this reference."
			checked={xVectorOnly}
			onchange={(checked: boolean) => (xVectorOnly = checked)}
		/>

		<Button class="h-9 w-full" disabled={busy} onclick={() => void submit()}>
			{#if busy}
				<LoaderCircle class="size-4 animate-spin" aria-hidden="true" />
				Registering
			{:else}
				Register voice
			{/if}
		</Button>
	</div>
</SlideOver>
