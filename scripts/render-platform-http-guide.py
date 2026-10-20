"""Render the customer guide and route schemas as one offline HTML document."""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import markdown
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.api.platform_polarrag import router  # noqa: E402


def render() -> None:
    document = ROOT / "docs/zh-cn/knowledge/polarrag-platform-http-api.md"
    source = document.read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("[English]"))
    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc"], extension_configs={"toc": {"toc_depth": "2-3"}})
    body = md.convert(source)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    schema = app.openapi()
    cards = []
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            payload = {key: operation[key] for key in ("parameters", "requestBody", "responses") if key in operation}
            cards.append(
                '<details class="endpoint"><summary><span class="method">'
                + method.upper() + '</span><code>' + html.escape(path)
                + '</code></summary><pre><code>'
                + html.escape(json.dumps(payload, ensure_ascii=False, indent=2))
                + '</code></pre></details>'
            )
    components = html.escape(json.dumps(schema.get("components", {}).get("schemas", {}), ensure_ascii=False, indent=2))
    page = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PAS · PolarRAG HTTP 接口参考</title>
<style>
:root{--ink:#153834;--muted:#637873;--line:#dbe5df;--accent:#087669;--paper:#fffefa}*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:28px}body{margin:0;background:#f2f5f0;color:var(--ink);font:16px/1.85 "PingFang SC","Microsoft YaHei",sans-serif}aside{position:fixed;inset:0 auto 0 0;width:272px;padding:32px 24px;background:#123c36;color:#d9e8e1;overflow:auto}.brand{font:700 24px Georgia,serif;letter-spacing:2px;color:white}.edition{font-size:12px;color:#a3c2b5;margin:8px 0 28px;letter-spacing:1px}aside ul{list-style:none;padding:0}aside ul ul{padding-left:14px;font-size:12px}aside a{display:block;color:inherit;text-decoration:none;font-size:14px;line-height:1.6;padding:7px 0}aside a:hover{color:#fff;text-decoration:underline}main{margin-left:272px;padding:44px 5vw 80px;max-width:1440px}.toolbar{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:32px}.eyebrow{color:var(--accent);font:600 12px monospace;letter-spacing:2px}button{border:1px solid var(--line);border-radius:5px;padding:8px 14px;background:var(--paper);color:var(--ink);cursor:pointer}button:hover{border-color:var(--accent)}h1{font-size:36px;line-height:1.3;letter-spacing:-1px;margin:0 0 28px}h2{font-size:25px;margin:64px 0 20px;border-top:2px solid var(--accent);padding-top:22px}h3{font-size:20px;margin:32px 0 14px}p,li{max-width:88ch}a{color:var(--accent)}code{font:13px/1.6 "SFMono-Regular",Consolas,monospace;overflow-wrap:anywhere;background:#e8eeE6;padding:2px 5px;border-radius:3px}pre{position:relative;padding:25px 20px;background:#15342f;color:#edf5ef;overflow:auto;border-radius:7px;font-size:13px}pre code{background:none;color:inherit;padding:0;overflow-wrap:normal}pre .copy{position:absolute;right:8px;top:6px;font-size:11px;padding:2px 8px;background:#244b42;color:#e5f2e8;border-color:#48705f}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px;background:var(--paper)}th{text-align:left;background:#e8eee6;font-weight:600}td,th{border-bottom:1px solid var(--line);padding:13px 14px;vertical-align:top}td code{font-size:12px}.note{padding:18px 22px;border-left:3px solid var(--accent);background:#e6eee4;margin:25px 0;font-size:14px}.endpoint{margin:10px 0;border:1px solid var(--line);border-radius:6px;background:var(--paper)}summary{cursor:pointer;padding:13px 16px}.method{display:inline-block;min-width:64px;color:var(--accent);font:700 12px monospace}details pre{margin:0;border-radius:0 0 5px 5px}input{width:100%;padding:13px;border:1px solid var(--line);border-radius:6px;background:var(--paper);font:inherit;margin:10px 0 20px}footer{border-top:1px solid var(--line);padding-top:24px;margin-top:56px;color:var(--muted);font-size:13px}@media(max-width:850px){aside{position:static;width:auto;padding:24px}aside nav{max-height:220px;overflow:auto}main{margin:0;padding:30px 20px}h1{font-size:29px}}@media print{aside,.toolbar,button,input{display:none!important}main{margin:0;padding:0;max-width:none}body{background:white;font-size:11pt}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f3f3;color:black}h2{break-after:avoid}a{color:inherit}details{break-inside:avoid}}
</style></head><body><aside><div class="brand">PAS / API</div><div class="edition">POLARRAG · 接入参考</div><nav>TOC_PLACEHOLDER<a href="#contracts">10. 逐接口参数与响应结构</a><a href="#schemas">11. 数据模型</a></nav></aside>
<main><div class="toolbar"><span class="eyebrow">HTTP INTERFACE REFERENCE</span><button onclick="window.print()">打印 / 导出 PDF</button></div>
<div class="note">面向平台集成开发者。本文可离线阅读；示例使用占位符，不含真实凭据。请将基础地址替换为实际 PAS 地址，请使用对应 PAS 版本离线导出的 OpenAPI 契约；在线文档入口已禁用。</div>
BODY_PLACEHOLDER
<h2 id="contracts">10. 逐接口参数与响应结构</h2><p>以下结构由当前接口定义生成，展开查看必填参数、默认值、取值范围、请求类型和响应模型。<code>$ref</code> 对应下一节数据模型；自由对象响应的业务字段见前文示例。鉴权及业务错误以第 1、9 节为准，响应列表不穷举所有业务错误。</p>
<label for="filter">按路径或 HTTP 方法筛选</label><input id="filter" type="search" placeholder="例如 documents/rechunk 或 POST">
<div id="endpoints">ENDPOINTS_PLACEHOLDER</div><p id="empty" hidden>没有匹配的接口。</p>
<h2 id="schemas">11. 数据模型</h2><details><summary>展开 JSON Schema</summary><pre><code>SCHEMAS_PLACEHOLDER</code></pre></details>
<footer>PAS · PolarRAG HTTP 接口参考 · 中文 HTML 版<br>接口操作仍受用户、Agent、Space 范围及文档 ACL 共同约束。</footer></main>
<script>
document.querySelectorAll('table').forEach(t=>{const w=document.createElement('div');w.className='table-wrap';t.before(w);w.append(t)});
document.querySelectorAll('pre').forEach(p=>{const b=document.createElement('button');b.className='copy';b.textContent='复制';b.onclick=async()=>{try{await navigator.clipboard.writeText(p.querySelector('code').textContent);b.textContent='已复制'}catch{const s=getSelection(),r=document.createRange();r.selectNodeContents(p.querySelector('code'));s.removeAllRanges();s.addRange(r);b.textContent='已选中，请复制'}setTimeout(()=>b.textContent='复制',1800)};p.append(b)});
document.getElementById('filter').addEventListener('input',e=>{const q=e.target.value.toLowerCase();let count=0;document.querySelectorAll('.endpoint').forEach(d=>{d.hidden=!d.querySelector('summary').textContent.toLowerCase().includes(q);if(!d.hidden)count++});document.getElementById('empty').hidden=count!==0});
</script></body></html>'''
    page = page.replace("TOC_PLACEHOLDER", md.toc).replace("BODY_PLACEHOLDER", body).replace("ENDPOINTS_PLACEHOLDER", "\n".join(cards)).replace("SCHEMAS_PLACEHOLDER", components)
    target = document.with_suffix(".html")
    target.write_text(page)
    print(f"Rendered {len(cards)} operations: {target}")


if __name__ == "__main__":
    render()
