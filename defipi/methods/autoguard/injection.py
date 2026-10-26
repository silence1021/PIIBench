"""Browser-independent definitions shared by static and runtime injection."""
import html as html_module
import re
from bs4 import BeautifulSoup

INJECTION_ID = "autoguard-pii-hidden-payload"
INJECTION_CSS = (
    "position:absolute!important;width:1px!important;height:1px!important;"
    "padding:0!important;margin:-1px!important;overflow:hidden!important;"
    "clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important;"
)
INJECTION_JS = """({id, text, css}) => {
                      if (document.getElementById(id)) return false;
                      const node = document.createElement('div');
                      node.id = id;
                      node.textContent = text;
                      node.setAttribute('data-autoguard-payload', 'true');
                      node.style.cssText = css;
                      document.body.appendChild(node);
                      return true;
                    }"""

def inject_static(html, payload):
    if BeautifulSoup(html, "html.parser").find(id=INJECTION_ID) is not None:
        raise ValueError("AutoGuard payload is already present")
    closings = list(re.finditer(r"</body\s*>", html, re.I))
    if not closings:
        raise ValueError("AutoGuard static injection requires a closing body tag")
    node = ('<div id="' + INJECTION_ID + '" data-autoguard-payload="true" style="'
            + INJECTION_CSS + '">' + html_module.escape(payload, quote=False) + '</div>')
    index = closings[-1].start()
    return html[:index] + node + html[index:]
