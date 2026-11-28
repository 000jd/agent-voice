<script lang="ts">
	import ErrorNotice from '#lib/components/common/error-notice.svelte';
	import { auth } from '#lib/stores/auth.svelte.js';

	let key = $state('');
	let inputError = $state('');

	async function submit(event: SubmitEvent): Promise<void> {
		event.preventDefault();
		inputError = '';
		const ok = await auth.submit(key);
		if (ok) key = '';
		else if (auth.state === 'required') inputError = 'That key does not match this daemon.';
	}
</script>

<svelte:head>
	<title>Connect · agent-voice</title>
</svelte:head>

<div class="flex min-h-dvh items-center justify-center bg-canvas p-4">
	<div class="w-full max-w-[440px]">
		<div class="mb-5 flex items-center gap-2.5">
			<span class="h-4 w-1 rounded-[1px] bg-signal" aria-hidden="true"></span>
			<span class="text-[13px] font-semibold tracking-[-0.01em] text-ink">agent-voice</span>
			<span class="metric ml-auto text-[10px] text-ink-muted">studio</span>
		</div>

		<div class="card p-5">
			<h1 class="text-[18px] leading-6 font-semibold tracking-[-0.01em] text-ink">
				Connect to agent-voice
			</h1>
			<p class="mt-1 text-[12px] leading-5 text-ink-secondary">
				This daemon is protected with <code class="metric text-[11px]">AGENT_VOICE_API_KEY</code>.
				The key stays in this tab's session storage, is sent as a bearer header on every request,
				and is cleared as soon as the daemon rejects it.
			</p>

			{#if auth.state === 'offline'}
				<div class="mt-4">
					<ErrorNotice
						error={{
							status: 0,
							code: 'OFFLINE',
							message: auth.error || 'The daemon did not answer.',
							suggestion: 'Start `agent-voice serve`, check the host and port, then retry.',
							correlationId: ''
						}}
						onRetry={() => void auth.verify()}
					/>
				</div>
			{/if}

			<form class="mt-4 space-y-3" onsubmit={(event) => void submit(event)}>
				<div class="space-y-1">
					<label class="field-label" for="auth-key">API key</label>
					<input
						id="auth-key"
						class="input h-9"
						type="password"
						name="agent-voice-key"
						autocomplete="off"
						spellcheck="false"
						placeholder="Paste the key set for this daemon"
						bind:value={key}
						aria-invalid={inputError ? 'true' : undefined}
						aria-describedby={inputError ? 'auth-key-error' : undefined}
					/>
					{#if inputError}
						<p id="auth-key-error" class="text-[11px] text-danger" role="alert">{inputError}</p>
					{/if}
					{#if auth.error && auth.state !== 'offline'}
						<p class="text-[11px] text-danger" role="alert">{auth.error}</p>
					{/if}
				</div>
				<button type="submit" class="btn btn-primary w-full" disabled={auth.pending}>
					{auth.pending ? 'Checking…' : 'Connect'}
				</button>
			</form>

			<p class="mt-4 border-t border-rule pt-3 text-[11px] leading-4 text-ink-muted">
				No key configured? Start the daemon with
				<code class="metric">AGENT_VOICE_API_KEY= agent-voice serve</code> for a keyless local
				session, or set the key in <code class="metric">~/.agent-voice/.env</code> and reload.
			</p>
		</div>
	</div>
</div>
