/* 在 Node 里跑页面脚本，专抓浏览器端才会暴露的错误。
 *
 * 存在的理由：前端已经溜过两个 Python 测试碰不到的 bug ——
 *   ① 「执行步数」渲染成 [object Object]（取错字段）
 *   ② renderEval 里 nA 声明在 if 块内、块外引用 → ReferenceError，
 *      整个函数抛出，消融卡直接空白
 * 后者尤其隐蔽：页面不报错、不空白报警，只是少了一块内容。
 *
 * 这不是在模拟浏览器，而是让脚本跑到底，把 ReferenceError / TypeError 逼出来。
 * 用法：node tests/js/run_page.js <index.html> <fixtures.json>
 */
const fs = require("fs"), vm = require("vm"), path = require("path");

const [htmlPath, fixPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const fixtures = JSON.parse(fs.readFileSync(fixPath, "utf8"));

let code = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");
// boot() 会去点真实接口并操作 DOM，跑它只会淹没真正想抓的错误
code = code.replace(/^boot\(\);\s*$/m, "");

const store = new WeakMap();
function stub() {
  const self = function () { return stub(); };
  return new Proxy(self, {
    get(t, k) {
      if (k === Symbol.toPrimitive || k === "toString") return () => "";
      if (k === "length") return 0;
      if (k === Symbol.iterator) return function* () {};
      if (!store.has(t)) store.set(t, {});
      const own = store.get(t);
      return k in own ? own[k] : stub();
    },
    set(t, k, v) {
      if (!store.has(t)) store.set(t, {});
      store.get(t)[k] = v; return true;
    },
    apply: () => stub(),
  });
}

const ctx = {
  document: stub(), window: null, console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  IntersectionObserver: class { observe() {} },
  location: { href: "", reload() {} }, navigator: stub(),
  fetch: async (url) => {
    const key = Object.keys(fixtures).find(k => String(url).includes(k));
    if (!key) throw new Error(`桩件里没有这个接口的数据：${url}`);
    return { ok: true, json: async () => fixtures[key] };
  },
};
ctx.window = ctx;
vm.createContext(ctx);

try { vm.runInContext(code, ctx); }
catch (e) { console.error(`脚本加载失败: ${e.constructor.name} - ${e.message}`); process.exit(1); }

(async () => {
  // 需要先备好全局态的函数，按页面里的真实调用顺序来
  const seq = ["boot", "renderEval"];
  for (const fn of seq) {
    if (typeof ctx[fn] !== "function") continue;
    try { await ctx[fn](); }
    catch (e) {
      console.error(`${fn} 抛异常: ${e.constructor.name} - ${e.message}`);
      process.exit(2);
    }
  }
  console.log("ok");
})();
