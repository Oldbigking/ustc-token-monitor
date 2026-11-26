/**
 * Host half: exposes the local monitor on same-origin routes so the browser
 * half never depends on CORS or a fixed localhost port.
 *
 * Routes:
 *   GET  /ustc-token/summary      -> proxy of http://127.0.0.1:<port>/api/summary
 *   POST /ustc-token/widget/open  -> proxy of /api/widget/open (raise or spawn the floating widget)
 *   GET  /ustc-token/status       -> diagnostic: did the browser half call us?
 *
 * The monitor port defaults to 8790. If you start the dashboard on another port
 * (`python monitor.py --serve --port 9000`), set USTC_MONITOR_PORT=9000 in the
 * environment that launches DSH; the browser half talks to us, not to the port.
 */
const MONITOR_PORT = Number.parseInt(process.env.USTC_MONITOR_PORT || "8790", 10) || 8790;
const MONITOR_BASE = `http://127.0.0.1:${MONITOR_PORT}`;
const UPSTREAM = `${MONITOR_BASE}/api/summary`;
const UPSTREAM_OPEN = `${MONITOR_BASE}/api/widget/open`;

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
					if (url.pathname === "/ustc-token/widget/open") {
						if (req.method !== "POST") {
							res.writeHead(405, { "Content-Type": "text/plain" });
							res.end("method not allowed");
							return;
						}
						try {
							const upstream = await fetch(UPSTREAM_OPEN, { method: "POST", cache: "no-store" });
							const body = await upstream.text();
							res.writeHead(upstream.status, {
								"Content-Type": "application/json; charset=utf-8",
								"Cache-Control": "no-store",
							});
							res.end(body);
						} catch (error) {
							json(res, 503, { ok: false, error: String((error && error.message) || error) });
						}
						return;
					}
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
