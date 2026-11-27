<script lang="ts">
	let {
		id,
		label,
		value,
		min,
		max,
		step,
		display,
		oninput
	}: {
		id: string;
		label: string;
		value: number;
		min: number;
		max: number;
		step: number;
		display: string;
		oninput: (value: number) => void;
	} = $props();
</script>

<div>
	<div class="mb-1 flex items-center justify-between gap-2">
		<label for={id} class="text-[10px] font-medium tracking-[0.06em] text-text-muted uppercase">
			{label}
		</label>
		<span class="font-mono text-[10px] text-text-secondary tabular-nums">{display}</span>
	</div>
	<input
		{id}
		class="h-1.5 w-full cursor-pointer accent-primary"
		type="range"
		{min}
		{max}
		{step}
		{value}
		oninput={(event) => oninput(Number(event.currentTarget.value))}
	/>
</div>
