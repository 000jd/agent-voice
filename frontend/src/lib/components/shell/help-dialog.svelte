<script lang="ts">
	import Dialog from '#lib/components/common/dialog.svelte';

	let { open = $bindable(false) }: { open?: boolean } = $props();

	const shortcuts = [
		{ keys: 'Space', action: 'Play or pause the docked player' },
		{ keys: 'Ctrl / ⌘ + Enter', action: 'Generate speech in Speak' },
		{ keys: 'Ctrl / ⌘ + K', action: 'Open the command palette' },
		{ keys: 'Esc', action: 'Close a dialog or the palette' },
		{ keys: '?', action: 'Open this help' }
	];
</script>

<Dialog bind:open title="Shortcuts and privacy" size="md">
	<div class="space-y-5">
		<section>
			<h2 class="text-[12px] font-medium text-ink-secondary">Keyboard</h2>
			<dl class="mt-2 divide-y divide-rule">
				{#each shortcuts as shortcut (shortcut.keys)}
					<div class="flex items-baseline justify-between gap-4 py-2">
						<dt><kbd class="kbd">{shortcut.keys}</kbd></dt>
						<dd class="text-right text-[12px] text-ink-secondary">{shortcut.action}</dd>
					</div>
				{/each}
			</dl>
		</section>

		<section class="border-t border-rule pt-4">
			<h2 class="text-[12px] font-medium text-ink-secondary">Privacy</h2>
			<ul class="mt-2 space-y-1.5 text-[12px] leading-5 text-ink-secondary">
				<li>Everything you see comes from your own daemon on this machine.</li>
				<li>
					The API key, when one is configured, lives in this tab's session storage only. It is never
					written to a URL, localStorage or a log.
				</li>
				<li>
					Recorded audio is uploaded to the daemon you are connected to and discarded when a draft
					is abandoned.
				</li>
				<li>No telemetry, no analytics, no external fonts, CDNs or service worker.</li>
			</ul>
		</section>

		<section class="border-t border-rule pt-4">
			<h2 class="text-[12px] font-medium text-ink-secondary">Where things live</h2>
			<p class="mt-2 text-[12px] leading-5 text-ink-secondary">
				Voices, takes, captures and models are written under the daemon's data directory. The exact
				paths are on the Settings screen, read from <code class="metric">/v1/server_info</code>.
			</p>
		</section>
	</div>
</Dialog>
