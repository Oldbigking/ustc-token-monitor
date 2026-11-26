window.__ModuleLoader__.load({
	id: "dsh-plugin-ustc-token",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		const React = require("react");
		const { useEffect, useRef, useState } = React;

		const PROXY = "/ustc-token/summary?probe=client";
		/* 直连回退用的端口；看板换端口时改这里，或让宿主用 USTC_MONITOR_PORT 代理 */
		const DIRECT = "http://127.0.0.1:8790/api/summary";
		const DASHBOARD = "http://127.0.0.1:8790/";
		const REFRESH_MS = 5000;

		const readJson = async (url) => {
			const res = await fetch(url, { cache: "no-store" });
			if (!res.ok) throw new Error("HTTP " + res.status);
			return res.json();
		};

		/* 唤起桌面浮窗：先走宿主代理（同源、不用管 CORS），失败再直连监测器 */
		const postJson = async (url) => {
			const res = await fetch(url, { method: "POST", cache: "no-store" });
			if (!res.ok) throw new Error("HTTP " + res.status);
			return res.json();
		};
		const openDashboard = () => window.open(DASHBOARD, "_blank", "noopener");
		const openWidget = async () => {
			const urls = [
				"/ustc-token/widget/open",
				DIRECT.replace(/\/api\/summary$/, "/api/widget/open"),
			];
			for (const url of urls) {
				try {
					const r = await postJson(url);
					if (r && r.ok) return true;
				} catch (error) {
					/* 试下一个入口 */
				}
			}
			return false;
		};
		/* 点击 = 唤起浮窗；浮窗拉不起来就退回看板。Shift/Alt/Ctrl/中键/右键 = 直接看板 */
		const onBadgeClick = async (event) => {
			if (event && (event.shiftKey || event.altKey || event.ctrlKey || event.metaKey)) {
				openDashboard();
				return;
			}
			if (!(await openWidget())) openDashboard();
		};

		const fmt = (n) => {
			if (n === null || n === undefined) return "–";
			if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
			if (n >= 1e3) return (n / 1e3).toFixed(0) + "k";
			return String(Math.round(n));
		};

		function useSummary() {
			const [state, setState] = useState({ ok: false, data: null, stale: 0 });
			const alive = useRef(true);
			useEffect(() => {
				alive.current = true;
				let timer = 0;
				const tick = async () => {
					try {
						let data;
						try {
							data = await readJson(PROXY);
						} catch (proxyError) {
							data = await readJson(DIRECT);
						}
						if (alive.current) setState({ ok: true, data, stale: 0 });
					} catch (error) {
						if (alive.current) {
							setState((prev) => ({ ok: false, data: prev.data, stale: prev.stale + 1 }));
						}
					}
					if (alive.current) timer = setTimeout(tick, REFRESH_MS);
				};
				tick();
				return () => {
					alive.current = false;
					clearTimeout(timer);
				};
			}, []);
			return state;
		}

		const pill = {
			display: "inline-flex",
			alignItems: "center",
			gap: "6px",
			padding: "2px 8px",
			borderRadius: "6px",
			border: "1px solid var(--dsh-border, rgba(127,127,127,.22))",
			fontSize: "12px",
			lineHeight: "18px",
			color: "var(--dsh-text-secondary, rgba(127,127,127,1))",
			cursor: "pointer",
			userSelect: "none",
			whiteSpace: "nowrap",
			background: "transparent",
		};
		const value = { fontWeight: 600, fontVariantNumeric: "tabular-nums", color: "var(--dsh-text-primary, inherit)" };
		const dot = (color) => ({
			width: "6px",
			height: "6px",
			borderRadius: "50%",
			background: color,
			boxShadow: "0 0 6px " + color,
			display: "inline-block",
		});

		function UstcTokenBadge() {
			const { ok, data, stale } = useSummary();
			const h1 = (data && data.h1) || {};
			const m5 = (data && data.m5a) || {};
			const tps = m5.tps;
			const idle = m5.idle_s;
			const fresh = ok && idle !== null && idle !== undefined && idle <= 300;
			const budget = (data && data.budgets && data.budgets[0]) || null;
			const idleMin = idle !== null && idle !== undefined ? Math.floor(idle / 60) : null;

			const color = !ok ? "#ff6b6b" : stale > 0 ? "#f5b95c" : fresh ? "#3ddc97" : "#f5b95c";

			// 右半边：新鲜时给 5m 用量，空闲时给"多久之前"
			const tailLabel = fresh ? "5m " : "";
			const tailValue = fresh ? fmt(m5.total) : idleMin !== null ? idleMin + "m 前" : "无数据";

			const title = ok
				? [
						"点击唤起桌面浮窗（Shift/右键 打开看板）",
						"最近 5 分钟窗口 " + (m5.start_local || "–") + "–" + (m5.end_local || "–") +
							"（末次调用 " + (m5.last_call_local || "–") + "）",
						"平均 " + (tps ? tps.toFixed(1) : "–") + " tok/s（加权，" + (m5.req || 0) + " 次）",
						fresh ? "" : "最近 " + (idleMin === null ? "?" : idleMin) + " 分钟没有新调用，显示的是最后活跃窗口",
						"5m 用量 " + fmt(m5.total) + " token",
						"1h 用量 " + fmt(h1.total) + " token / " + (h1.req || 0) + " 次",
						"首 token " + (data.ttft_ms || "–") + " ms",
						"今日 " + fmt(data.day && data.day.total) + " token / 费用 " + ((data.day && data.day.spend) || 0).toFixed(2),
						budget ? "3h 限额 " + (budget.used || 0).toFixed(2) + " / " + (budget.max || 0) : "",
				  ]
						.filter(Boolean)
						.join("\n")
				: "监测服务未运行：点击尝试唤起浮窗，或运行 start.bat";

			return React.createElement(
				"button",
				{
					type: "button",
					style: pill,
					title,
					onClick: onBadgeClick,
					onContextMenu: (event) => {
						event.preventDefault();
						openDashboard();
					},
					onAuxClick: (event) => {
						if (event.button === 1) openDashboard();   // 中键
					},
					onMouseEnter: (e) => (e.currentTarget.style.background = "var(--dsh-surface-hover, rgba(127,127,127,.10))"),
					onMouseLeave: (e) => (e.currentTarget.style.background = "transparent"),
				},
				React.createElement("span", { style: dot(color) }),
				React.createElement(
					"span",
					{ style: value },
					ok && tps ? tps.toFixed(1) : "–",
				),
				React.createElement("span", null, "tok/s"),
				React.createElement("span", { style: { opacity: 0.35 } }, "·"),
				tailLabel ? React.createElement("span", null, tailLabel) : null,
				React.createElement("span", { style: value }, ok ? tailValue : "–"),
			);
		}

		const inject = ["slots"];

		function apply(ctx) {
			ctx.slots.inject("conversation.session.header.utilities", () =>
				ctx.slots.register(
					{
						name: "conversation.session.header.utilities",
						id: "ustc-token",
						order: 30,
						label: "USTC token",
					},
					UstcTokenBadge,
				),
			);
		}

		exports.UstcTokenBadge = UstcTokenBadge;
		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	},
});
