"""Self-contained, escaped HTML; report content is never script source."""

import html
import json


def render_report(report):
    def escape(value):
        return html.escape(str(value), quote=True)

    def pretty(value):
        return escape(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False))

    summary = report["summary"]
    cards = "".join(f'<div class="metric"><strong>{summary[key]}</strong><span>{label}</span></div>'
                    for key, label in (("calls", "Calls / 调用"), ("ok", "Successes / 成功"),
                                       ("error", "Scripted errors / 预设失败"), ("rejected", "Rejected / 拒绝"),
                                       ("cancelled", "Cancelled / 取消")))
    rows = []
    for event in report["events"]:
        outcome = event.get("result") if event["status"] == "ok" else event.get("error")
        elapsed = "pending" if "elapsed_ms" not in event else f'{event["elapsed_ms"]:.2f} ms'
        rows.append(f'''<article class="call" data-status="{escape(event['status'])}">
<div class="call-head"><span class="id">{escape(event['id'])}</span>
<h2>{escape(event['tool'])}</h2><span class="status {escape(event['status'])}">{escape(event['status'])}</span></div>
<p class="timing">Step {escape(event['step'] if event['step'] is not None else '—')} · Requested delay {event['requested_delay_ms']} ms · Observed {escape(elapsed)}</p>
<div class="payload"><details open><summary>Arguments</summary><pre>{pretty(event['arguments'])}</pre></details>
<details><summary>Result / error</summary><pre>{pretty(outcome)}</pre></details></div></article>''')
    body = "\n".join(rows) or '<p class="empty">No calls were recorded.</p>'
    if report.get("stopped_early"):
        body = '<p role="alert"><strong>Partial run:</strong> ' + escape(report["stopped_early"]["message"]) + '</p>' + body
    return '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>tool-mockbox · Local fixture run</title><style>
:root{color-scheme:light;--ink:#17283b;--muted:#5d6d80;--line:#dbe3ed;--accent:#624bd1}*{box-sizing:border-box}
body{margin:0;background:#f3f6fb;color:var(--ink);font:15px/1.55 system-ui,sans-serif}main{max-width:1120px;margin:auto;padding:45px 24px}
.eyebrow{color:var(--accent);text-transform:uppercase;letter-spacing:.15em;font-size:12px;font-weight:750}h1{font-size:38px;letter-spacing:-.04em;margin:8px 0}header p{max-width:740px;color:var(--muted)}
.metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:25px 0}.metric{background:white;border:1px solid var(--line);border-radius:12px;padding:17px}.metric strong{display:block;font-size:29px}.metric span{color:var(--muted);font-size:13px}
.filters{display:flex;gap:12px;margin:25px 0}input,select{font:inherit;background:white;border:1px solid var(--line);border-radius:8px;padding:10px 12px}input{flex:1;min-width:100px}
.call{background:white;border:1px solid var(--line);border-radius:12px;padding:20px;margin:14px 0}.call-head{display:flex;gap:13px;align-items:center;flex-wrap:wrap}h2{font-size:20px;margin:0;overflow-wrap:anywhere}.id,.timing{font-size:13px;color:var(--muted)}.status{margin-left:auto;padding:3px 10px;border-radius:20px;background:#eeeafa;font-size:12px;font-weight:700}.ok{background:#ddf4e7;color:#165d36}.error,.rejected{background:#fff0e2;color:#904b0a}.cancelled{background:#f5e5ed;color:#8a315b}
.payload{display:grid;grid-template-columns:1fr 1fr;gap:15px}summary{cursor:pointer;font-size:13px;font-weight:650}pre{margin:10px 0 0;padding:13px;background:#f5f7fb;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 ui-monospace,monospace}.empty,footer{color:var(--muted)}footer{margin-top:30px;border-top:1px solid var(--line);padding-top:18px;font-size:12px}[hidden]{display:none!important}
@media(max-width:700px){main{padding:25px 14px}.metrics{grid-template-columns:repeat(2,1fr)}.payload{grid-template-columns:1fr}h1{font-size:30px}.status{margin-left:0}}@media print{.filters{display:none}.call[hidden]{display:block!important}body{background:white}.call{break-inside:avoid}details{display:block}pre{background:transparent}}
</style></head><body><main><header><div class="eyebrow">TOOL-MOCKBOX / LOCAL FIXTURES</div>
<h1>验证工具失败时，代码如何应对。</h1>
<p>See how your calling code handles failure.</p>
<p>A scripted tool run, with responses consumed in reservation order. This report does not evaluate a model or claim to reproduce real service behavior.</p>
<p>Wait mode: <strong>''' + escape(report["wait_mode"]) + '''</strong>. Requested delays are scenario settings; observed durations are measured wall-clock values.</p></header>
<section class="metrics" aria-label="Run summary">''' + cards + '''</section>
<div class="filters"><input id="search" type="search" placeholder="Search tools, arguments, or results" aria-label="Search calls">
<select id="status" aria-label="Filter status"><option value="all">All statuses</option><option>ok</option><option>error</option><option>rejected</option><option>cancelled</option><option>running</option></select></div>
<p id="match-count" role="status" aria-live="polite">Showing ''' + str(len(report["events"])) + ''' calls / 条调用</p>
<p id="no-match" class="empty" hidden>No matching calls / 无匹配调用</p>
<section id="calls" aria-label="Recorded calls">''' + body + '''</section>
<footer>Local report · No external resources or uploads. Arguments and fixture results may be sensitive; keep the file within its intended audience. With JavaScript disabled, every call remains readable.</footer>
</main><script>
const search=document.getElementById('search'),status=document.getElementById('status');
function filter(){const query=search.value.toLocaleLowerCase(),rows=Array.from(document.querySelectorAll('.call'));let visible=0;rows.forEach(row=>{row.hidden=(status.value!=='all'&&row.dataset.status!==status.value)||!row.textContent.toLocaleLowerCase().includes(query);if(!row.hidden)visible++;});document.getElementById('match-count').textContent=`Showing ${visible} of ${rows.length} calls / 显示 ${visible} 条，共 ${rows.length} 条`;document.getElementById('no-match').hidden=visible!==0||rows.length===0;}
search.addEventListener('input',filter);status.addEventListener('change',filter);
</script></body></html>
'''
