<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import Segmented from '#lib/components/common/segmented.svelte';
	import CloneDialog from '#lib/components/voices/clone-dialog.svelte';
	import ScriptEditor from '#lib/components/speak/script-editor.svelte';
	import SpeakInspector from '#lib/components/speak/speak-inspector.svelte';
	import TakeList from '#lib/components/speak/take-list.svelte';
	import VoicePicker from '#lib/components/speak/voice-picker.svelte';
	import { speak } from '#lib/stores/speak.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';

	let view = $state<'voice' | 'script' | 'inspector'>('script');

	const views = [
		{ id: 'voice', label: 'Voice' },
		{ id: 'script', label: 'Script' },
		{ id: 'inspector', label: 'Settings' }
	];

	onMount(() => {
		if (studio.engines.length === 0) void studio.refreshCatalog();
	});

	$effect(() => {
		if (studio.catalogLoading) return;
		const engines = studio.engines;
		const voices = studio.voices;
		const presets = studio.presets;
		if (engines.length === 0 && voices.length === 0 && presets.length === 0) return;
		speak.init();
		speak.syncCatalog();
	});

	onDestroy(() => {
		speak.dispose();
	});
</script>

<div class="flex h-full min-h-0 flex-col">
	<div class="shrink-0 border-b border-rule bg-panel px-3 py-1.5 xl:hidden">
		<Segmented
			items={views}
			value={view}
			label="Speak pane"
			onchange={(id) => (view = id as typeof view)}
		/>
	</div>

	<div class="flex min-h-0 flex-1">
		<aside
			id="speak-pane-voice"
			class={`${view === 'voice' ? 'flex' : 'hidden'} min-h-0 w-full shrink-0 flex-col border-rule bg-panel lg:flex lg:w-[280px] lg:border-r`}
			aria-label="Voice picker"
		>
			<VoicePicker />
		</aside>

		<section
			id="speak-pane-script"
			class={`${view === 'script' ? 'flex' : 'hidden'} min-h-0 min-w-0 flex-1 flex-col lg:flex`}
			aria-label="Script editor"
		>
			<ScriptEditor />
			<TakeList />
		</section>

		<aside
			id="speak-pane-inspector"
			class={`${view === 'inspector' ? 'flex' : 'hidden'} min-h-0 w-full shrink-0 flex-col border-rule bg-panel xl:flex xl:w-[300px] xl:border-l`}
			aria-label="Generation inspector"
		>
			<SpeakInspector />
		</aside>
	</div>
</div>

<CloneDialog />
