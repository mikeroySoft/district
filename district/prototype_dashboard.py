"""Throwaway operator UI comparison. Run: python district/prototype_dashboard.py

Three variants at /?variant=A|B|C; illustrative data, no management API.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FILES = {"A": "fleet", "B": "flow", "C": "brief"}
SWITCHER = '''
<style>
#prototype-switcher{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;align-items:center;gap:12px;padding:10px 14px;border:1px solid #64748b;border-radius:12px;background:#101827;color:#f8fafc;box-shadow:0 6px 28px #0005;font:13px system-ui;white-space:nowrap}
#prototype-switcher button{background:#253249;color:white;border:1px solid #718096;border-radius:6px;min-width:44px;min-height:44px;cursor:pointer;font:inherit}
#prototype-switcher button:focus-visible{outline:3px solid #5eead4;outline-offset:3px}
#prototype-switcher small{display:block;color:#cbd5e1;font-size:11px}
</style>
<nav id="prototype-switcher" aria-label="Prototype variants"><button id="proto-prev" aria-label="Previous prototype">←</button><span><small>READ-ONLY PROTOTYPE · SAMPLE DATA</small><strong id="proto-label"></strong></span><button id="proto-next" aria-label="Next prototype">→</button></nav>
<script>
const variants=['A','B','C'], names=['Fleet console','Flow monitor','Operations brief'];
const selected=new URLSearchParams(location.search).get('variant')||'A';
const current=Math.max(0,variants.indexOf(selected));
document.getElementById('proto-label').textContent=variants[current]+' / '+names[current];
function cycle(delta){const u=new URL(location.href);u.searchParams.set('variant',variants[(current+delta+3)%3]);location.href=u;}
document.getElementById('proto-prev').onclick=()=>cycle(-1);
document.getElementById('proto-next').onclick=()=>cycle(1);
document.addEventListener('keydown',e=>{if(e.target.closest('input,textarea,select,[contenteditable],dialog')||e.altKey||e.ctrlKey||e.metaKey)return;if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();cycle(e.key==='ArrowLeft'?-1:1);}});
</script>
'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/":
            self.send_error(404)
            return
        variant = parse_qs(url.query).get("variant", ["A"])[0]
        if variant not in FILES:
            self.send_error(400, "Choose variant A, B, or C")
            return
        page = Path(__file__).with_name(f"prototype-{FILES[variant]}.html").read_text()
        body = page.replace("</body>", SWITCHER + "</body>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8761), Handler)
    print("District prototypes: http://0.0.0.0:8761/?variant=A", flush=True)
    server.serve_forever()
