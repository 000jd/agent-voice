/**
 * Auth gate state.
 *
 * The key is validated against `/v1/auth/check`, kept in `sessionStorage` by the
 * client, and never rendered back to the user.
 */
import { checkAuth, isUnauthorized } from '#lib/api/auth.js';
import {
	errorText,
	loadSessionKey,
	setSessionKey,
	setUnauthorizedHandler
} from '#lib/api/client.js';

export type AuthState = 'checking' | 'required' | 'ready' | 'offline';

class AuthStore {
	state = $state<AuthState>('checking');
	/** True when the daemon itself says a key is configured. */
	keyRequired = $state(false);
	pending = $state(false);
	error = $state('');

	private started = false;

	async init(): Promise<void> {
		if (this.started) return;
		this.started = true;
		const stored = loadSessionKey();
		if (stored) setSessionKey(stored);
		setUnauthorizedHandler(() => {
			if (this.state === 'ready') {
				this.state = 'required';
				this.error = 'The daemon rejected this session key. Enter it again.';
			}
		});
		await this.verify();
	}

	async verify(): Promise<void> {
		this.state = 'checking';
		this.error = '';
		try {
			const result = await checkAuth();
			this.keyRequired = result.authRequired;
			this.state = 'ready';
		} catch (error) {
			if (isUnauthorized(error)) {
				this.keyRequired = true;
				this.state = 'required';
			} else {
				this.state = 'offline';
				this.error = errorText(error);
			}
		}
	}

	async submit(rawKey: string): Promise<boolean> {
		const key = rawKey.trim();
		if (!key) {
			this.error = 'Enter the AGENT_VOICE_API_KEY value set for this daemon.';
			return false;
		}
		this.pending = true;
		this.error = '';
		setSessionKey(key);
		try {
			const result = await checkAuth();
			this.keyRequired = result.authRequired;
			this.state = 'ready';
			return true;
		} catch (error) {
			setSessionKey(null);
			if (isUnauthorized(error)) {
				this.state = 'required';
				this.error = 'That key was rejected by the daemon.';
			} else {
				this.state = 'offline';
				this.error = errorText(error);
			}
			return false;
		} finally {
			this.pending = false;
		}
	}

	signOut(): void {
		setSessionKey(null);
		this.state = 'required';
		this.keyRequired = true;
		this.error = '';
	}
}

export const auth = new AuthStore();
