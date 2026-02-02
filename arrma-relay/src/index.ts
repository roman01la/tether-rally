/**
 * ARRMA RC Car Relay - Cloudflare Workers
 *
 * Simplified after migration to WebRTC DataChannel:
 * - Serves static assets (control UI)
 * - Provides TURN credentials for WebRTC (requires valid token)
 * - Protects admin page with basic auth
 * - Generates access tokens for players
 * - Proxies admin API calls to Pi (keeps admin password server-side)
 *
 * Control flow is now:
 * Browser → WebRTC DataChannel → Pi → UDP → ESP32
 *
 * Environment variables (set in wrangler.toml or dashboard):
 * - TURN_KEY_ID: Cloudflare TURN key ID
 * - TURN_KEY_API_TOKEN: Cloudflare TURN API token
 * - ADMIN_PASSWORD: Password for admin page (basic auth) and Pi admin API
 * - TOKEN_SECRET: Secret for generating access tokens
 * - CONTROL_URL: Pi control relay URL (for admin API proxy)
 */

interface Env {
	ASSETS: Fetcher;
	TURN_KEY_ID: string;
	TURN_KEY_API_TOKEN: string;
	ADMIN_PASSWORD: string;
	TOKEN_SECRET: string;
	CONTROL_URL: string;
}

// Validate HMAC-SHA256 signed token (same algorithm as Pi)
async function validateToken(token: string, env: Env): Promise<boolean> {
	if (!token || token.length !== 24 || !env.TOKEN_SECRET) {
		return false;
	}

	const expiryHex = token.substring(0, 8);
	const signature = token.substring(8);

	// Check expiry
	const expiry = parseInt(expiryHex, 16);
	if (isNaN(expiry) || Date.now() / 1000 > expiry) {
		return false;
	}

	// Verify HMAC signature
	const encoder = new TextEncoder();
	const keyData = encoder.encode(env.TOKEN_SECRET);
	const key = await crypto.subtle.importKey('raw', keyData, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
	const signatureBytes = await crypto.subtle.sign('HMAC', key, encoder.encode(expiryHex));
	const expectedSignature = Array.from(new Uint8Array(signatureBytes))
		.map((b) => b.toString(16).padStart(2, '0'))
		.join('')
		.substring(0, 16);

	// Constant-time comparison
	if (signature.length !== expectedSignature.length) {
		return false;
	}
	let diff = 0;
	for (let i = 0; i < signature.length; i++) {
		diff |= signature.charCodeAt(i) ^ expectedSignature.charCodeAt(i);
	}
	return diff === 0;
}

// Basic auth check for admin pages
function checkBasicAuth(request: Request, env: Env): Response | null {
	const authHeader = request.headers.get('Authorization');

	if (!authHeader || !authHeader.startsWith('Basic ')) {
		return new Response('Unauthorized', {
			status: 401,
			headers: { 'WWW-Authenticate': 'Basic realm="Admin Area"' },
		});
	}

	const base64Credentials = authHeader.slice(6);
	const credentials = atob(base64Credentials);
	const [username, password] = credentials.split(':');

	// Username can be anything, just check password
	if (password !== env.ADMIN_PASSWORD) {
		return new Response('Unauthorized', {
			status: 401,
			headers: { 'WWW-Authenticate': 'Basic realm="Admin Area"' },
		});
	}

	return null; // Auth passed
}

// Generate TURN credentials from Cloudflare
async function generateTurnCredentials(env: Env): Promise<Response> {
	if (!env.TURN_KEY_ID || !env.TURN_KEY_API_TOKEN) {
		return new Response('TURN credentials not configured', { status: 500 });
	}

	const response = await fetch(`https://rtc.live.cloudflare.com/v1/turn/keys/${env.TURN_KEY_ID}/credentials/generate-ice-servers`, {
		method: 'POST',
		headers: {
			Authorization: `Bearer ${env.TURN_KEY_API_TOKEN}`,
			'Content-Type': 'application/json',
		},
		body: JSON.stringify({ ttl: 86400 }), // 24 hour TTL
	});

	if (!response.ok) {
		return new Response('Failed to generate TURN credentials', { status: 500 });
	}

	const data = await response.json();
	return new Response(JSON.stringify(data), {
		headers: {
			'Content-Type': 'application/json',
			'Access-Control-Allow-Origin': '*',
		},
	});
}

// Generate access token for players (HMAC-SHA256 signed)
async function generateAccessToken(env: Env, durationMinutes: number): Promise<Response> {
	if (!env.TOKEN_SECRET) {
		return new Response(JSON.stringify({ error: 'TOKEN_SECRET not configured' }), {
			status: 500,
			headers: { 'Content-Type': 'application/json' },
		});
	}

	const expiryTime = Math.floor(Date.now() / 1000) + durationMinutes * 60;
	const expiryHex = expiryTime.toString(16).padStart(8, '0');

	// Use Web Crypto API for HMAC-SHA256
	const encoder = new TextEncoder();
	const keyData = encoder.encode(env.TOKEN_SECRET);
	const key = await crypto.subtle.importKey('raw', keyData, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);

	const signature = await crypto.subtle.sign('HMAC', key, encoder.encode(expiryHex));
	const signatureHex = Array.from(new Uint8Array(signature))
		.map((b) => b.toString(16).padStart(2, '0'))
		.join('')
		.substring(0, 16);

	const token = `${expiryHex}${signatureHex}`;
	const expiresAt = new Date(expiryTime * 1000).toISOString();

	return new Response(JSON.stringify({ token, expiresAt, durationMinutes }), {
		headers: { 'Content-Type': 'application/json' },
	});
}

export default {
	async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
		const url = new URL(request.url);
		const pathname = url.pathname;

		// Protect admin pages and API with basic auth
		if (pathname === '/admin.html' || pathname === '/admin' || pathname === '/admin/' || pathname === '/admin/generate-token') {
			// Require ADMIN_PASSWORD to be set
			if (!env.ADMIN_PASSWORD) {
				return new Response('Admin password not configured', { status: 500 });
			}
			const authError = checkBasicAuth(request, env);
			if (authError) return authError;

			// Token generation endpoint
			if (pathname === '/admin/generate-token' && request.method === 'POST') {
				const body = await request.json().catch(() => ({}));
				const durationMinutes = (body as { minutes?: number }).minutes || 60;
				return generateAccessToken(env, durationMinutes);
			}

			// After auth, pass through to assets (don't rewrite URL to avoid redirect loop)
			return env.ASSETS.fetch(request);
		}

		// Admin API proxy routes (authenticated via Basic Auth above)
		// These proxy to the Pi's admin endpoints with X-Admin-Password header
		if (pathname.startsWith('/admin/api/')) {
			// Require Basic Auth for all admin API calls
			if (!env.ADMIN_PASSWORD) {
				return new Response('Admin password not configured', { status: 500 });
			}
			const authError = checkBasicAuth(request, env);
			if (authError) return authError;

			if (!env.CONTROL_URL) {
				return new Response(JSON.stringify({ error: 'CONTROL_URL not configured' }), {
					status: 500,
					headers: { 'Content-Type': 'application/json' },
				});
			}

			// Map /admin/api/start-race -> Pi's /admin/start-race
			const piEndpoint = pathname.replace('/admin/api/', '/admin/');
			const piUrl = `${env.CONTROL_URL}${piEndpoint}`;

			try {
				const piResponse = await fetch(piUrl, {
					method: request.method,
					headers: {
						'Content-Type': 'application/json',
						'X-Admin-Password': env.ADMIN_PASSWORD,
					},
					body: request.method !== 'GET' ? await request.text() : undefined,
				});

				const responseBody = await piResponse.text();
				return new Response(responseBody, {
					status: piResponse.status,
					headers: {
						'Content-Type': 'application/json',
						'Access-Control-Allow-Origin': '*',
					},
				});
			} catch (err) {
				return new Response(JSON.stringify({ error: 'Failed to reach Pi' }), {
					status: 502,
					headers: { 'Content-Type': 'application/json' },
				});
			}
		}

		// TURN credentials endpoint (requires valid token to prevent abuse)
		if (pathname === '/turn-credentials') {
			const token = url.searchParams.get('token') || '';
			const isValid = await validateToken(token, env);
			if (!isValid) {
				return new Response(JSON.stringify({ error: 'Invalid or expired token' }), {
					status: 401,
					headers: {
						'Content-Type': 'application/json',
						'Access-Control-Allow-Origin': '*',
					},
				});
			}
			return generateTurnCredentials(env);
		}

		// Serve static assets
		return env.ASSETS.fetch(request);
	},
} satisfies ExportedHandler<Env>;
