/**
 * Host half: exposes the local monitor summary on a same-origin route so the
 * browser half never depends on CORS or a fixed localhost port.
 *
 * Routes:
 *   GET /ustc-token/summary   -> proxy of http://127.0.0.1:8790/api/summary
 *   GET /ustc-token/status    -> diagnostic: did the browser half call us?
 */

const UPSTREAM = "http://127.0.0.1:8790/api/summary";

export const inject = ["webServer"];

export function apply(ctx) {
	const stats = {
		loadedAt: new Date().toISOString(),
		clientHits: 0,
		lastClientAt: null,
		lastUpstreamStatus: null,
		lastUpstreamAt: null,
		lastError: null,
	};

	const json = (res, status, value) => {
		res.writeHead(status, {
			"Content-Type": "application/json; charset=utf-8",
			"Cache-Control": "no-store",
		});
		res.end(JSON.stringify(value));
	};

	ctx.effect(
		() =>
			ctx.webServer.register({
				kind: "prefix",
				path: "/ustc-token",
				handler: async (req, res) => {
					const url = new URL(req.url ?? "/", "http://127.0.0.1");
					if (req.method !== "GET" && req.method !== "HEAD") {
						res.writeHead(405, { "Content-Type": "text/plain" });
						res.end("method not allowed");
						return;
					}
					if (url.pathname === "/ustc-token/status") {
						json(res, 200, { ...stats, upstream: UPSTREAM });
						return;
					}
					if (url.searchParams.has("probe")) {
						stats.clientHits += 1;
						stats.lastClientAt = new Date().toISOString();
					}
					try {
						const upstream = await fetch(UPSTREAM, { cache: "no-store" });
						const body = await upstream.text();
						stats.lastUpstreamStatus = upstream.status;
						stats.lastUpstreamAt = new Date().toISOString();
						res.writeHead(upstream.status, {
							"Content-Type": "application/json; charset=utf-8",
							"Cache-Control": "no-store",
						});
						res.end(req.method === "HEAD" ? undefined : body);
					} catch (error) {
						stats.lastError = String((error && error.message) || error);
						json(res, 503, { error: stats.lastError });
					}
				},
			}),
		"ustc-token: summary route",
	);
}
