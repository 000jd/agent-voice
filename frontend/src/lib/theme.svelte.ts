const STORAGE_KEY = 'agent-voice-theme';

export type ThemeMode = 'light' | 'dark';

function initialMode(): ThemeMode {
	if (typeof document === 'undefined') return 'light';
	return document.documentElement.classList.contains('dark') ? 'dark' : 'light';
}

class Theme {
	mode = $state<ThemeMode>(initialMode());

	init(): void {
		if (typeof document === 'undefined') return;
		this.mode = initialMode();
	}

	toggle(): void {
		this.set(this.mode === 'dark' ? 'light' : 'dark');
	}

	set(mode: ThemeMode): void {
		this.mode = mode;
		if (typeof document !== 'undefined') {
			document.documentElement.classList.toggle('dark', mode === 'dark');
		}
		try {
			localStorage.setItem(STORAGE_KEY, mode);
		} catch {
			return;
		}
	}
}

export const theme = new Theme();
