import type { Path } from '$app/types';
import type { Component } from 'svelte';
import AudioLines from '@lucide/svelte/icons/audio-lines';
import Boxes from '@lucide/svelte/icons/boxes';
import FileMusic from '@lucide/svelte/icons/file-music';
import History from '@lucide/svelte/icons/history';
import LayoutDashboard from '@lucide/svelte/icons/layout-dashboard';
import Mic from '@lucide/svelte/icons/mic';
import Settings from '@lucide/svelte/icons/settings';

export type NavItem = {
	label: string;
	href: Path;
	icon: Component;
};

export const NAV_ITEMS: readonly NavItem[] = [
	{ label: 'Dashboard', href: '', icon: LayoutDashboard },
	{ label: 'Speak', href: 'speak', icon: AudioLines },
	{ label: 'Voices', href: 'voices', icon: Mic },
	{ label: 'Transcribe', href: 'transcribe', icon: FileMusic },
	{ label: 'Models', href: 'models', icon: Boxes },
	{ label: 'History', href: 'history', icon: History },
	{ label: 'Settings', href: 'settings', icon: Settings }
];
