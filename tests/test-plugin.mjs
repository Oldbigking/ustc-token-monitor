// 静态校验：包解析 + 客户端 bundle 工厂可执行
import { readFileSync } from "node:fs";
import vm from "node:vm";

const pkg = JSON.parse(readFileSync(new URL("../plugin/package.json", import.meta.url), "utf8"));
console.log("package:", pkg.name, "client decl:", JSON.stringify(pkg.dsh.client));
console.log("exports[./client]:", pkg.exports["./client"]);

const src = readFileSync(new URL("../plugin/lib/client.js", import.meta.url), "utf8");

let def;
const sandbox = {
  window: { __ModuleLoader__: { load(d) { def = d; } } },
  console,
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
console.log("loaded id:", def && def.id, "factory:", typeof (def && def.factory));

const fakeReact = {
  createElement: (...args) => ({ type: args[0], props: args[1] }),
  useEffect: () => {},
  useRef: (v) => ({ current: v }),
  useState: (v) => [v, () => {}],
};
const require = (name) => {
  if (name === "react") return fakeReact;
  throw new Error("unexpected require: " + name);
};
const mod = def.factory(require);
console.log("exports keys:", Object.keys(mod).join(","));
console.log("apply:", typeof mod.apply, "inject:", JSON.stringify(mod.inject));
console.log("component:", typeof mod.UstcTokenBadge);

// 模拟 apply 注册
const registrations = [];
const ctx = {
  slots: {
    inject: (name, cb) => cb(),
    register: (opts, comp) => registrations.push({ opts, comp }),
  },
};
mod.apply(ctx);
console.log("registered:", JSON.stringify(registrations[0].opts), "component ok:", typeof registrations[0].comp === "function");

// 渲染一次（假 React 只验证组件函数不抛错；hooks 为桩）
try {
  const el = registrations[0].comp({});
  console.log("render ok:", el && el.type);
} catch (e) {
  console.log("render threw (expected with stub hooks):", e.message);
}
