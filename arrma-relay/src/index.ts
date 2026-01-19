/**
 * ARRMA RC Car Relay - Cloudflare Workers
 *
 * Simplified after migration to WebRTC DataChannel:
 * - Serves static assets (control UI)
 * - Provides TURN credentials for WebRTC
 * - Protects admin page with basic auth
 *
 * Control flow is now:
 * Browser → WebRTC DataChannel → Pi → UDP → ESP32
 *
 * Environment variables (set in wrangler.toml or dashboard):
 * - TURN_KEY_ID: Cloudflare TURN key ID
 * - TURN_KEY_API_TOKEN: Cloudflare TURN API token
 * - ADMIN_PASSWORD: Password for admin page (basic auth)
 */

interface Env {
	ASSETS: Fetcher;
	TURN_KEY_ID: string;
	TURN_KEY_API_TOKEN: string;
	ADMIN_PASSWORD: string;
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

export default {
	async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
		const url = new URL(request.url);

		// Protect admin page with basic auth
		if (url.pathname === '/admin.html' || url.pathname === '/admin') {
			if (env.ADMIN_PASSWORD) {
				const authError = checkBasicAuth(request, env);
				if (authError) return authError;
			}
			// Serve admin.html for both /admin and /admin.html
			if (url.pathname === '/admin') {
				return env.ASSETS.fetch(new Request(new URL('/admin.html', request.url), request));
			}
		}

		// TURN credentials endpoint (for video WebRTC)
		if (url.pathname === '/turn-credentials') {
			return generateTurnCredentials(env);
		}

		// Serve static assets
		return env.ASSETS.fetch(request);
	},
} satisfies ExportedHandler<Env>;
