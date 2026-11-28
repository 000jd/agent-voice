<script lang="ts">
	import { onMount } from 'svelte';
	import Check from '@lucide/svelte/icons/check';
	import Copy from '@lucide/svelte/icons/copy';
	import RefreshCw from '@lucide/svelte/icons/refresh-cw';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import ThemeToggle from '#lib/components/common/theme-toggle.svelte';
	import ConfirmDialog from '#lib/components/common/confirm-dialog.svelte';
	import ErrorNotice from '#lib/components/common/error-notice.svelte';
	import SettingField from '#lib/components/settings/setting-field.svelte';
	import { describeApiError } from '#lib/api/client.js';
	import {
		clearCache,
		deleteLexiconEntry,
		getCacheStats,
		getDiagnostics,
		getLexicon,
		getMcpConfig,
		getServerInfo,
		getSettings,
		patchSettings,
		putLexicon,
		type McpClient
	} from '#lib/api/settings.js';
	import type {
		ApiErrorPayload,
		CacheStats,
		Diagnostics,
		LexiconSnapshot,
		ServerInfo,
		SettingsSnapshot,
		SettingEntry
	} from '#lib/api/types.js';
	import { formatBytes, formatDateTime, plural } from '#lib/format.js';
	import { theme } from '#lib/theme.svelte.js';
	import { toasts } from '#lib/stores/toasts.svelte.js';

	let snapshot = $state<SettingsSnapshot | null>(null);
	let serverInfo = $state<ServerInfo | null>(null);
	let diagnostics = $state<Diagnostics | null>(null);
	let cache = $state<CacheStats | null>(null);
	let lexicon = $state<LexiconSnapshot | null>(null);
	let mcpConfig = $state<Record<string, unknown> | null>(null);
	let mcpClient = $state<McpClient>('command-code');

	let loading = $state(true);
	let loadError = $state<ApiErrorPayload | null>(null);
	let saving = $state(false);
	let saveError = $state<ApiErrorPayload | null>(null);
	let restartRequired = $state(false);
	let dirty = $state<Record<string, unknown>>({});

	let diagnosticsLoading = $state(false);
	let diagnosticsError = $state<ApiErrorPayload | null>(null);
	let cacheLoading = $state(false);
	let cacheError = $state<ApiErrorPayload | null>(null);
	let cacheClearOpen = $state(false);
	let lexiconError = $state<ApiErrorPayload | null>(null);
	let lexiconBusy = $state(false);
	let mcpError = $state<ApiErrorPayload | null>(null);
	let copied = $state(false);

	let newWord = $state('');
	let newReplacement = $state('');

	const groups = $derived.by<{ name: string; entries: SettingEntry[] }[]>(() => {
		const result: { name: string; entries: SettingEntry[] }[] = [];
		for (const entry of snapshot?.settings ?? []) {
			const existing = result.find((group) => group.name === entry.group);
			if (existing) existing.entries.push(entry);
			else result.push({ name: entry.group, entries: [entry] });
		}
		return result;
	});

	const changedCount = $derived(Object.keys(dirty).length);
	const editableGroups = $derived(groups.filter((group) => group.entries.some((e) => e.editable)));
	const readOnlyGroups = $derived(groups.filter((group) => !group.entries.some((e) => e.editable)));

	function displayValue(value: unknown): string {
		if (value === null || value === undefined) return '—';
		if (typeof value === 'string') return value;
		if (typeof value === 'number' || typeof value === 'boolean') return String(value);
		if (Array.isArray(value)) return value.map((item: unknown) => String(item)).join(', ');
		return JSON.stringify(value);
	}

	function currentValue(entry: SettingEntry): unknown {
		return entry.key in dirty ? dirty[entry.key] : (snapshot?.values[entry.key] ?? entry.value);
	}

	function setValue(key: string, value: unknown): void {
		dirty = { ...dirty, [key]: value };
	}

	async function loadAll(): Promise<void> {
		loading = true;
		loadError = null;
		try {
			const [settings, info] = await Promise.all([getSettings(), getServerInfo()]);
			snapshot = settings;
			serverInfo = info;
			restartRequired = settings.restartRequired;
			dirty = {};
		} catch (error) {
			loadError = describeApiError(error);
		} finally {
			loading = false;
		}
		void runDiagnostics();
		void loadCache();
		void loadLexicon();
		void loadMcp();
	}

	async function save(): Promise<void> {
		if (changedCount === 0) return;
		saving = true;
		saveError = null;
		try {
			const updated = await patchSettings(dirty);
			snapshot = updated;
			restartRequired = true;
			dirty = {};
			toasts.success('Settings saved', 'Restart the daemon to apply them.');
		} catch (error) {
			saveError = describeApiError(error);
		} finally {
			saving = false;
		}
	}

	async function runDiagnostics(): Promise<void> {
		diagnosticsLoading = true;
		diagnosticsError = null;
		try {
			diagnostics = await getDiagnostics();
		} catch (error) {
			diagnosticsError = describeApiError(error);
		} finally {
			diagnosticsLoading = false;
		}
	}

	async function loadCache(): Promise<void> {
		cacheLoading = true;
		cacheError = null;
		try {
			cache = await getCacheStats();
		} catch (error) {
			cacheError = describeApiError(error);
		} finally {
			cacheLoading = false;
		}
	}

	async function doClearCache(): Promise<void> {
		cacheLoading = true;
		try {
			const result = await clearCache();
			toasts.success(
				'Cache cleared',
				`${plural(result.filesDeleted, 'file')} · ${formatBytes(result.bytesFreed)} freed`
			);
			await loadCache();
		} catch (error) {
			cacheError = describeApiError(error);
		} finally {
			cacheLoading = false;
		}
	}

	async function loadLexicon(): Promise<void> {
		lexiconError = null;
		try {
			lexicon = await getLexicon();
		} catch (error) {
			lexiconError = describeApiError(error);
		}
	}

	async function addLexiconEntry(): Promise<void> {
		if (!newWord.trim() || !newReplacement.trim()) return;
		lexiconBusy = true;
		lexiconError = null;
		try {
			lexicon = await putLexicon({
				word: newWord.trim(),
				replacement: newReplacement.trim(),
				scope: 'global'
			});
			newWord = '';
			newReplacement = '';
			toasts.success('Pronunciation saved');
		} catch (error) {
			lexiconError = describeApiError(error);
		} finally {
			lexiconBusy = false;
		}
	}

	async function removeLexiconEntry(word: string): Promise<void> {
		lexiconBusy = true;
		lexiconError = null;
		try {
			await deleteLexiconEntry({ word, scope: 'global' });
			await loadLexicon();
		} catch (error) {
			lexiconError = describeApiError(error);
		} finally {
			lexiconBusy = false;
		}
	}

	async function loadMcp(): Promise<void> {
		mcpError = null;
		try {
			mcpConfig = await getMcpConfig(mcpClient);
		} catch (error) {
			mcpError = describeApiError(error);
		}
	}

	async function selectMcpClient(client: McpClient): Promise<void> {
		mcpClient = client;
		await loadMcp();
	}

	async function copyMcp(): Promise<void> {
		if (!mcpConfig) return;
		try {
			await navigator.clipboard.writeText(JSON.stringify(mcpConfig, null, 2));
			copied = true;
			window.setTimeout(() => (copied = false), 2000);
		} catch (error) {
			mcpError = describeApiError(error);
		}
	}

	const apiKeyConfigured = $derived(snapshot?.values.AGENT_VOICE_API_KEY === true);

	onMount(() => {
		void loadAll();
	});
</script>

<div class="h-full overflow-y-auto">
	<div class="mx-auto w-full max-w-[1000px] px-4 py-4 md:px-6">
		<div class="flex flex-wrap items-center gap-2">
			<p class="text-[12px] text-ink-secondary">
				<code class="metric">{snapshot?.configPath || 'config path unknown'}</code>
			</p>
			<div class="ml-auto flex items-center gap-2">
				<button type="button" class="btn btn-sm btn-ghost" onclick={() => void loadAll()}>
					<RefreshCw class="size-3.5" aria-hidden="true" />
					Reload
				</button>
				{#if changedCount > 0}
					<span class="metric text-[11px] text-ink-secondary">{changedCount} unsaved</span>
					<button type="button" class="btn btn-sm" onclick={() => (dirty = {})}>Discard</button>
					<button
						type="button"
						class="btn btn-sm btn-primary"
						disabled={saving}
						onclick={() => void save()}
					>
						{saving ? 'Saving…' : 'Save changes'}
					</button>
				{/if}
			</div>
		</div>

		{#if restartRequired}
			<div class="mt-3 rounded-control border border-rule bg-signal-soft px-3 py-2" role="status">
				<p class="text-[12px] leading-5 text-ink">
					<strong class="font-medium">Restart required.</strong>
					Stop the daemon (Ctrl-C) and start <code class="metric">agent-voice serve</code> again;
					these values are read at startup. <code class="metric">agent-voice doctor</code> verifies the
					result.
				</p>
			</div>
		{/if}

		{#if saveError}
			<div class="mt-3">
				<ErrorNotice error={saveError} compact />
			</div>
		{/if}

		{#if loading}
			<div class="mt-4 space-y-2">
				{#each Array.from({ length: 4 }, (_, index) => index) as index (index)}
					<div class="skeleton h-16 w-full"></div>
				{/each}
			</div>
		{:else if loadError}
			<div class="mt-4">
				<ErrorNotice error={loadError} onRetry={() => void loadAll()} />
			</div>
		{:else}
			{#each editableGroups as group (group.name)}
				<section class="mt-5" aria-labelledby={`group-${group.name}`}>
					<h2 id={`group-${group.name}`} class="mb-2 text-[13px] font-medium text-ink">
						{group.name}
					</h2>
					<div
						class="grid gap-3 rounded-panel border border-rule bg-surface p-3 sm:grid-cols-2 lg:grid-cols-3"
					>
						{#each group.entries as entry (entry.key)}
							<SettingField
								{entry}
								value={currentValue(entry)}
								onchange={(next) => setValue(entry.key, next)}
							/>
						{/each}
					</div>
				</section>
			{/each}

			{#each readOnlyGroups as group (group.name)}
				<section class="mt-5" aria-labelledby={`group-${group.name}`}>
					<h2 id={`group-${group.name}`} class="mb-2 text-[13px] font-medium text-ink">
						{group.name}
					</h2>
					<div class="rounded-panel border border-rule bg-surface p-3">
						<p class="mb-3 text-[11px] leading-4 text-ink-muted">
							Read-only in the Studio by design. Edit
							<code class="metric">{snapshot?.configPath || 'the daemon config'}</code> and restart
							the daemon; <code class="metric">agent-voice doctor</code> verifies the result.
						</p>
						<dl class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
							{#each group.entries as entry (entry.key)}
								<div class="min-w-0">
									<dt class="field-label">{entry.label}</dt>
									<dd
										class="metric truncate text-[12px] text-ink"
										title={String(currentValue(entry))}
									>
										{#if entry.key === 'AGENT_VOICE_API_KEY'}
											{apiKeyConfigured ? 'Configured' : 'Not set'}
										{:else}
											{displayValue(currentValue(entry))}
										{/if}
									</dd>
									<dd class="metric truncate text-[10px] text-ink-muted">{entry.key}</dd>
								</div>
							{/each}
						</dl>
					</div>
				</section>
			{/each}

			{#if serverInfo}
				<section class="mt-5" aria-labelledby="settings-paths">
					<h2 id="settings-paths" class="mb-2 text-[13px] font-medium text-ink">Paths</h2>
					<dl
						class="grid gap-2 rounded-panel border border-rule bg-surface p-3 text-[12px] sm:grid-cols-2"
					>
						<div>
							<dt class="text-[11px] text-ink-muted">Home</dt>
							<dd class="metric truncate">{serverInfo.home}</dd>
						</div>
						<div>
							<dt class="text-[11px] text-ink-muted">Data</dt>
							<dd class="metric truncate">{serverInfo.dataDir}</dd>
						</div>
						<div>
							<dt class="text-[11px] text-ink-muted">Artifacts</dt>
							<dd class="metric truncate">{serverInfo.artifactsDir}</dd>
						</div>
						<div>
							<dt class="text-[11px] text-ink-muted">Engines</dt>
							<dd class="metric truncate">{serverInfo.enginesDir}</dd>
						</div>
						<div class="sm:col-span-2">
							<dt class="text-[11px] text-ink-muted">Inbox convention</dt>
							<dd class="metric truncate">
								{serverInfo.inboxDir || `${serverInfo.dataDir}/inbox/`}
							</dd>
						</div>
					</dl>
					<p class="mt-1 text-[11px] text-ink-muted">
						Local-path cloning is confined to the inbox unless the daemon is configured otherwise.
						The Studio always uploads a draft instead of sending a server path.
					</p>
				</section>
			{/if}

			<section class="mt-5" aria-labelledby="settings-theme">
				<h2 id="settings-theme" class="mb-2 text-[13px] font-medium text-ink">Appearance</h2>
				<div
					class="flex items-center justify-between gap-3 rounded-panel border border-rule bg-surface p-3"
				>
					<div>
						<p class="text-[12px] font-medium text-ink">Theme</p>
						<p class="text-[11px] text-ink-muted">
							Dark is the default; the choice is stored in this browser only.
						</p>
					</div>
					<div class="flex items-center gap-2">
						<span class="metric text-[11px] text-ink-secondary">{theme.mode}</span>
						<ThemeToggle />
					</div>
				</div>
			</section>

			<section class="mt-5" aria-labelledby="settings-diagnostics">
				<div class="mb-2 flex items-center gap-2">
					<h2 id="settings-diagnostics" class="text-[13px] font-medium text-ink">Diagnostics</h2>
					<button
						type="button"
						class="btn btn-sm btn-ghost ml-auto"
						disabled={diagnosticsLoading}
						onclick={() => void runDiagnostics()}
					>
						{diagnosticsLoading ? 'Running…' : 'Run checks'}
					</button>
				</div>
				{#if diagnosticsError}
					<ErrorNotice error={diagnosticsError} onRetry={() => void runDiagnostics()} />
				{:else if diagnostics}
					<div class="rounded-panel border border-rule bg-surface">
						<div class="border-b border-rule px-3 py-1.5 text-[11px] text-ink-muted">
							Checked {formatDateTime(diagnostics.checkedAt)} · {diagnostics.checks.length} checks
						</div>
						<ul class="divide-y divide-rule">
							{#each diagnostics.checks as check (check.name)}
								<li class="flex flex-wrap items-start gap-2 px-3 py-1.5">
									<span
										class={`chip shrink-0 ${check.status === 'PASS' ? 'chip-success' : check.status === 'FAIL' ? 'chip-danger' : check.status === 'WARN' ? 'chip-signal' : ''}`}
									>
										{check.status}
									</span>
									<span class="metric w-[92px] shrink-0 text-[11px] text-ink">{check.name}</span>
									<span class="min-w-0 flex-1 text-[11px] text-ink-secondary">
										{check.detail}
										{#if check.fix}
											<span class="block text-ink-muted">{check.fix}</span>
										{/if}
									</span>
								</li>
							{/each}
						</ul>
					</div>
				{:else}
					<p class="text-[12px] text-ink-secondary">
						Run the checks to see the daemon's own verdict.
					</p>
				{/if}
			</section>

			<section class="mt-5" aria-labelledby="settings-mcp">
				<h2 id="settings-mcp" class="mb-2 text-[13px] font-medium text-ink">MCP configuration</h2>
				<div class="rounded-panel border border-rule bg-surface p-3">
					<div class="flex flex-wrap items-end gap-2">
						<div class="space-y-1">
							<label class="field-label" for="mcp-client">Client</label>
							<select
								id="mcp-client"
								class="select h-8 text-[12px]"
								value={mcpClient}
								onchange={(event) => void selectMcpClient(event.currentTarget.value as McpClient)}
							>
								<option value="command-code">Command Code</option>
								<option value="claude-code">Claude Code</option>
								<option value="claude-desktop">Claude Desktop</option>
								<option value="cursor">Cursor</option>
								<option value="vscode">VS Code</option>
							</select>
						</div>
						<button
							type="button"
							class="btn btn-sm"
							disabled={!mcpConfig}
							onclick={() => void copyMcp()}
						>
							{#if copied}
								<Check class="size-3.5" aria-hidden="true" />
								Copied
							{:else}
								<Copy class="size-3.5" aria-hidden="true" />
								Copy JSON
							{/if}
						</button>
					</div>
					{#if mcpError}
						<div class="mt-2"><ErrorNotice error={mcpError} compact /></div>
					{/if}
					<label class="sr-only" for="mcp-json">MCP configuration JSON</label>
					<textarea
						id="mcp-json"
						class="textarea metric mt-2 min-h-32 text-[11px]"
						readonly
						value={mcpConfig ? JSON.stringify(mcpConfig, null, 2) : ''}></textarea>
					<p class="mt-1 text-[11px] text-ink-muted">
						The daemon renders this from its own home path. Paste it into the client's MCP settings
						file, then restart that client.
					</p>
				</div>
			</section>

			<section class="mt-5" aria-labelledby="settings-lexicon">
				<h2 id="settings-lexicon" class="mb-2 text-[13px] font-medium text-ink">
					Pronunciation lexicon
					{#if lexicon}
						<span class="metric text-[11px] text-ink-muted">
							{lexicon.mergedEntries} entries · hash {lexicon.pronunciationHash.slice(0, 10)}
						</span>
					{/if}
				</h2>
				<div class="rounded-panel border border-rule bg-surface p-3">
					{#if lexiconError}
						<div class="mb-2"><ErrorNotice error={lexiconError} compact /></div>
					{/if}
					<form
						class="flex flex-wrap items-end gap-2"
						onsubmit={(event) => {
							event.preventDefault();
							void addLexiconEntry();
						}}
					>
						<div class="space-y-1">
							<label class="field-label" for="lexicon-word">Word</label>
							<input
								id="lexicon-word"
								class="input h-8 w-[160px] text-[12px]"
								type="text"
								bind:value={newWord}
							/>
						</div>
						<div class="space-y-1">
							<label class="field-label" for="lexicon-replacement">Say instead</label>
							<input
								id="lexicon-replacement"
								class="input h-8 w-[200px] text-[12px]"
								type="text"
								bind:value={newReplacement}
							/>
						</div>
						<button type="submit" class="btn btn-sm" disabled={lexiconBusy}>
							Add to global scope
						</button>
					</form>
					{#if lexicon}
						<ul class="mt-3 divide-y divide-rule border-t border-rule">
							{#each Object.entries(lexicon.effective) as [word, replacement] (word)}
								<li class="flex items-center gap-2 py-1.5">
									<span class="metric w-[180px] shrink-0 truncate text-[12px] text-ink">{word}</span
									>
									<span class="min-w-0 flex-1 truncate text-[12px] text-ink-secondary"
										>{replacement}</span
									>
									<button
										type="button"
										class="btn btn-ghost btn-icon text-ink-muted hover:text-danger"
										aria-label={`Delete pronunciation for ${word}`}
										disabled={lexiconBusy}
										onclick={() => void removeLexiconEntry(word)}
									>
										<Trash2 class="size-3.5" aria-hidden="true" />
									</button>
								</li>
							{/each}
							{#if Object.keys(lexicon.effective).length === 0}
								<li class="py-2 text-[12px] text-ink-secondary">
									No pronunciation overrides. Add one above; it applies to every render unless a
									project or voice scope overrides it.
								</li>
							{/if}
						</ul>
					{/if}
				</div>
			</section>

			<section class="mt-5 mb-4" aria-labelledby="settings-cache">
				<h2 id="settings-cache" class="mb-2 text-[13px] font-medium text-ink">Synthesis cache</h2>
				<div class="rounded-panel border border-rule bg-surface p-3">
					{#if cacheError}
						<div class="mb-2"><ErrorNotice error={cacheError} compact /></div>
					{/if}
					{#if cache}
						<dl class="metric grid gap-2 text-[12px] sm:grid-cols-3">
							<div>
								<dt class="text-[11px] text-ink-muted">Entries</dt>
								<dd>{cache.entries}</dd>
							</div>
							<div>
								<dt class="text-[11px] text-ink-muted">On disk</dt>
								<dd>{formatBytes(cache.totalBytes)}</dd>
							</div>
							<div>
								<dt class="text-[11px] text-ink-muted">Limit</dt>
								<dd>{formatBytes(cache.maxBytes)}</dd>
							</div>
						</dl>
						<p class="metric mt-1 truncate text-[11px] text-ink-muted" title={cache.cacheDir}>
							{cache.cacheDir}
						</p>
					{:else if cacheLoading}
						<div class="skeleton h-10 w-full"></div>
					{/if}
					<button
						type="button"
						class="btn btn-sm mt-2"
						disabled={cacheLoading}
						onclick={() => (cacheClearOpen = true)}
					>
						Clear the synthesis cache
					</button>
					<p class="mt-1 text-[11px] text-ink-muted">
						Clearing removes cached renders; history entries keep their own artifacts.
					</p>
				</div>
			</section>
		{/if}
	</div>
</div>

<ConfirmDialog
	bind:open={cacheClearOpen}
	title="Clear the synthesis cache"
	message="Delete every cached render?"
	detail="Identical lines will be re-synthesised. History artifacts are separate and stay intact."
	confirmLabel="Clear cache"
	danger
	onconfirm={() => void doClearCache()}
/>
