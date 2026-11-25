// 宿主半测试：注册路由并用假 req/res 调一次，确认能代理到本地监测器
import { apply } from "../plugin/lib/index.js";

let route;
const ctx = {
  effect: (fn) => {
    const dispose = fn();
    return dispose;
  },
  webServer: {
    register: (r) => {
      route = r;
      return () => {};
    },
  },
};
apply(ctx);
console.log("route:", route.kind, route.path);

const chunks = [];
const res = {
  statusCode: 0,
  headers: {},
  writeHead(code, headers) {
    this.statusCode = code;
    this.headers = headers;
  },
  end(body) {
    chunks.push(body);
  },
};
await route.handler({ method: "GET", url: "/ustc-token/summary" }, res);
console.log("status:", res.statusCode, "content-type:", res.headers["Content-Type"]);
const text = chunks.join("");
console.log("bytes:", text.length);
try {
  const j = JSON.parse(text);
  console.log("keys:", Object.keys(j).slice(0, 8).join(","));
  console.log("h1.tps:", j.h1 && j.h1.tps, "day.total:", j.day && j.day.total, "history:", (j.history || []).length);
} catch {
  console.log("body:", text.slice(0, 200));
}
