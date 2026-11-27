<script lang="ts">
	import { cn } from '#lib/utils.js';

	type Tone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger';

	let {
		tone = 'neutral',
		label,
		class: className
	}: {
		tone?: Tone;
		label: string;
		class?: string;
	} = $props();

	const tones: Record<Tone, string> = {
		neutral: 'border-border bg-elevated text-text-secondary',
		accent: 'border-primary/30 bg-primary/10 text-primary',
		success: 'border-success/25 bg-success/10 text-success',
		warning: 'border-warning/25 bg-warning/10 text-warning',
		danger: 'border-destructive/25 bg-destructive/10 text-destructive'
	};
</script>

<span
	class={cn(
		'inline-flex h-5 items-center rounded-full border px-2 text-[10px] font-medium whitespace-nowrap',
		tones[tone],
		className
	)}
>
	{label}
</span>
