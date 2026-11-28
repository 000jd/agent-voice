<script lang="ts">
	import { initials } from '#lib/voices.js';

	let {
		name,
		size = 'md',
		kind = 'voice'
	}: {
		name: string;
		size?: 'sm' | 'md' | 'lg';
		kind?: 'voice' | 'preset';
	} = $props();

	const dimensions = $derived(
		size === 'lg'
			? 'size-12 text-[15px]'
			: size === 'sm'
				? 'size-7 text-[10px]'
				: 'size-9 text-[12px]'
	);
</script>

<span
	class={`monogram shrink-0 ${dimensions} ${kind === 'preset' ? 'border-dashed' : ''}`}
	aria-hidden="true">{initials(name)}</span
>
