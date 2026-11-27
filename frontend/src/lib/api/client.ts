import { createPathBasedClient } from 'openapi-fetch';
import type { paths } from './schema.js';

export const api = createPathBasedClient<paths>({ baseUrl: '' });
