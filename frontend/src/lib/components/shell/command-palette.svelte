<script lang="ts">
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import type { Path } from '$app/types';
	import { Dialog } from 'bits-ui';
	import Search from '@lucide/svelte/icons/search';
	import { NAV_ITEMS } from '#lib/nav.js';
	import { clone } from '#lib/stores/clone.svelte.js';
	import { palette } from '#lib/stores/palette.svelte.js';
	import { speak } from '#lib/stores/speak.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import { toasts } from '#lib/stores/toasts.svelte.js';
	import { theme } from '#lib/theme.svelte.js';
	import { uniquePresets, voiceLanguageLabel, voiceKindLabel } from '#lib/voices.js';

	let { onhelp }: { onhelp: () => void } = $props();

	type Item = {
		id: string;
		group: string;
		label: string;
		hint: string;
		keywords: string;
		run: () => void;
		destructive?: boolean;
	};

	let query = $state('');
	let activeIndex = $state(0);
	let listEl = $state<HTMLElement | null>(null);
	let confirmItem = $state<Item | null>(null);

	function navigate(href: Path): void {
		void goto(resolve(href));
	}

	const items = $derived.by<Item[]>(() => {
		const result: Item[] = NAV_ITEMS.map((item) => ({
			id: `nav-${item.href}`,
			group: 'Navigate',
			label: `Go to ${item.label}`,
			hint: '',
			keywords: `navigate ${item.label} ${item.href}`,
			run: () => navigate(item.href)
		}));
		for (const voice of studio.voices) {
			result.push({
				id: `voice-${voice.id}`,
				group: 'Your voices',
				label: voice.name,
				hint: `${voiceKindLabel(voice)} · ${voiceLanguageLabel(voice)} · ${voice.durationSec.toFixed(1)}s`,
				keywords: `${voice.name} ${voice.kind} ${voice.language} ${voice.refText}`,
				run: () => {
					speak.selectVoice(voice);
					navigate('speak');
				}
			});
		}
		for (const preset of uniquePresets(studio.presets)) {
			result.push({
				id: `preset-${preset.presetId}`,
				group: 'Presets',
				label: `Speak with ${preset.displayName}`,
				hint: `${preset.engineId} · ${preset.modelAlias}`,
				keywords: `${preset.displayName} ${preset.speaker} ${preset.engineId} ${preset.modelAlias} preset`,
				run: () => {
					speak.selectPreset(preset);
					navigate('speak');
				}
			});
		}
		result.push(
			{
				id: 'cmd-clone',
				group: 'Commands',
				label: 'Clone a voice',
				hint: 'Upload or record a reference',
				keywords: 'clone voice new record upload',
				run: () => {
					navigate('voices');
					clone.begin('create');
				}
			},
			{
				id: 'cmd-theme',
				group: 'Commands',
				label: theme.mode === 'dark' ? 'Switch to light theme' : 'Switch to dark theme',
				hint: '',
				keywords: 'theme light dark appearance',
				run: () => theme.toggle()
			},
			{
				id: 'cmd-refresh',
				group: 'Commands',
				label: 'Refresh engines and voices',
				hint: 'Re-read /v1/engines, /v1/voices, /v1/presets',
				keywords: 'refresh reload catalog engines voices',
				run: () => {
					void studio.refreshCatalog();
					toasts.info('Catalog refreshed');
				}
			},
			{
				id: 'cmd-help',
				group: 'Commands',
				label: 'Keyboard shortcuts and privacy',
				hint: '',
				keywords: 'help shortcuts privacy about',
				run: () => onhelp()
			},
			{
				id: 'cmd-unload',
				group: 'Commands',
				label: 'Unload all models',
				hint: 'Frees VRAM; cancels pending renders',
				keywords: 'unload models vram gpu free memory',
				destructive: true,
				run: () => {
					void (async () => {
						try {
							const result = await studio.unloadAll();
							toasts.success(
								'Models unloaded',
								`${result.unloaded.length} engines · ${result.cancelledJobs} jobs cancelled`
							);
						} catch (error) {
							toasts.error('Unload failed', error instanceof Error ? error.message : 'Unknown');
						}
					})();
				}
			}
		);
		return result;
	});

	const filtered = $derived.by<Item[]>(() => {
		const needle = query.trim().toLowerCase();
		if (!needle) return items;
		return items.filter((item) =>
			`${item.label} ${item.hint} ${item.keywords}`.toLowerCase().includes(needle)
		);
	});

	const grouped = $derived.by<{ name: string; entries: { item: Item; index: number }[] }[]>(() => {
		const groups: { name: string; entries: { item: Item; index: number }[] }[] = [];
		filtered.forEach((item, index) => {
			const existing = groups.find((group) => group.name === item.group);
			if (existing) existing.entries.push({ item, index });
			else groups.push({ name: item.group, entries: [{ item, index }] });
		});
		return groups;
	});

	const activeId = $derived(
		filtered[Math.min(activeIndex, Math.max(0, filtered.length - 1))]?.id ?? ''
	);

	$effect(() => {
		if (!palette.open) {
			query = '';
			activeIndex = 0;
			confirmItem = null;
		}
	});

	$effect(() => {
		const id = activeId;
		if (!id || !listEl) return;
		const option = listEl.querySelector(`[data-option-id="${id}"]`);
		option?.scrollIntoView({ block: 'nearest' });
	});

	function activate(item: Item): void {
		if (item.destructive) {
			confirmItem = item;
			return;
		}
		palette.hide();
		item.run();
	}

	function runConfirmed(): void {
		const item = confirmItem;
		confirmItem = null;
		palette.hide();
		item?.run();
	}

	function onKeyDown(event: KeyboardEvent): void {
		if (confirmItem) {
			if (event.key === 'Escape') {
				event.preventDefault();
				event.stopPropagation();
				confirmItem = null;
			}
			if (event.key === 'Enter') {
				event.preventDefault();
				runConfirmed();
			}
			return;
		}
		const count = filtered.length;
		if (event.key === 'ArrowDown') {
			event.preventDefault();
			activeIndex = count === 0 ? 0 : (activeIndex + 1) % count;
			return;
		}
		if (event.key === 'ArrowUp') {
			event.preventDefault();
			activeIndex = count === 0 ? 0 : (activeIndex - 1 + count) % count;
			return;
		}
		if (event.key === 'Home') {
			event.preventDefault();
			activeIndex = 0;
			return;
		}
		if (event.key === 'End') {
			event.preventDefault();
			activeIndex = Math.max(0, count - 1);
			return;
		}
		if (event.key === 'Enter') {
			event.preventDefault();
			const item = filtered[Math.min(activeIndex, count - 1)];
			if (item) activate(item);
		}
	}
</script>

<Dialog.Root
	bind:open={palette.open}
	onOpenChange={(next) => {
		if (!next) palette.hide();
	}}
>
	<Dialog.Portal>
		<Dialog.Overlay class="fixed inset-0 z-50 bg-scrim" />
		<Dialog.Content
			class="fixed inset-x-3 top-[12vh] z-50 flex max-h-[70vh] flex-col overflow-hidden rounded-panel border border-rule-strong bg-panel sm:left-1/2 sm:w-[560px] sm:-translate-x-1/2"
		>
			<Dialog.Title class="sr-only">Command palette</Dialog.Title>
			<Dialog.Description class="sr-only">
				Search screens, voices, presets and commands.
			</Dialog.Description>

			<div class="flex items-center gap-2 border-b border-rule px-3 py-2">
				<Search class="size-4 shrink-0 text-ink-muted" aria-hidden="true" />
				<input
					class="h-8 w-full bg-transparent text-[13px] text-ink outline-none"
					type="text"
					role="combobox"
					aria-expanded="true"
					aria-controls="palette-listbox"
					aria-autocomplete="list"
					aria-activedescendant={activeId ? `palette-option-${activeId}` : undefined}
					aria-label="Search commands and voices"
					placeholder="Search screens, voices, commands"
					autocomplete="off"
					spellcheck="false"
					value={query}
					oninput={(event) => {
						query = event.currentTarget.value;
						activeIndex = 0;
					}}
					onkeydown={onKeyDown}
				/>
				<kbd class="kbd">Esc</kbd>
			</div>

			{#if confirmItem}
				<div
					class="m-3 rounded-control border border-rule bg-surface p-3"
					role="alertdialog"
					aria-label="Confirm"
				>
					<p class="text-[13px] font-medium text-ink">{confirmItem.label}?</p>
					<p class="mt-1 text-[12px] leading-5 text-ink-secondary">
						{confirmItem.hint}. This cannot be undone.
					</p>
					<div class="mt-3 flex justify-end gap-2">
						<button type="button" class="btn btn-sm" onclick={() => (confirmItem = null)}>
							Cancel
						</button>
						<button type="button" class="btn btn-sm btn-danger" onclick={runConfirmed}>
							Unload all
						</button>
					</div>
				</div>
			{:else}
				<div class="min-h-0 flex-1 overflow-y-auto p-1.5" bind:this={listEl}>
					<div id="palette-listbox" role="listbox" aria-label="Results">
						{#each grouped as group (group.name)}
							<div role="group" aria-label={group.name} class="mb-1">
								<p class="px-2 py-1 text-[11px] font-medium text-ink-muted" aria-hidden="true">
									{group.name}
								</p>
								{#each group.entries as entry (entry.item.id)}
									<div
										id={`palette-option-${entry.item.id}`}
										data-option-id={entry.item.id}
										role="option"
										aria-selected={entry.index === activeIndex}
										tabindex="-1"
										class={`flex cursor-pointer items-center gap-3 rounded-control px-2 py-1.5 ${
											entry.index === activeIndex ? 'bg-signal-soft text-ink' : 'text-ink-secondary'
										}`}
										onclick={() => activate(entry.item)}
										onpointermove={() => (activeIndex = entry.index)}
										onkeydown={(event) => {
											if (event.key === 'Enter' || event.key === ' ') {
												event.preventDefault();
												activate(entry.item);
											}
										}}
									>
										<span class="min-w-0 flex-1 truncate text-[13px]">{entry.item.label}</span>
										{#if entry.item.hint}
											<span class="metric hidden shrink-0 text-[10px] text-ink-muted sm:block">
												{entry.item.hint}
											</span>
										{/if}
									</div>
								{/each}
							</div>
						{/each}
						{#if filtered.length === 0}
							<p class="px-3 py-6 text-center text-[12px] text-ink-secondary">
								Nothing matches “{query}”.
							</p>
						{/if}
					</div>
				</div>
			{/if}
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>
