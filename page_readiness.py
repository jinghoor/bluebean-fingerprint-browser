"""Read-only page/target readiness probes; never stop loading or send input."""
from __future__ import annotations

import json
import math
import time


def readiness(ready: str) -> dict:
    return {"dom_ready": ready in ("interactive", "complete"),
            "load_complete": ready == "complete",
            "background_loading": ready != "complete"}


# A target's own geometry is sampled; animations elsewhere never extend a wait.
# This runs on demand without installing observers, attributes or page globals.
PROBE_JS = r"""
(condition => {
  const result = {url: location.href, document_token: String(performance.timeOrigin),
    ready: document.readyState, dom_ready: document.readyState !== 'loading',
    load_complete: document.readyState === 'complete', matched: false};
  if (condition.document_token && condition.document_token !== result.document_token)
    return {...result, stale_document: true, reason: 'document_changed'};
  if (condition.url && result.url !== condition.url) return {...result, reason: 'url_not_matched'};
  const state = condition.state;
  // DOM/URL/load waits do not need layout, hit-testing or the whole page's text.
  if (!condition.selector && !condition.text && !condition.identity && !condition.editable) {
    const matched = state === 'complete' ? result.load_complete : result.dom_ready;
    return {...result, matched, reason: matched ? 'condition_met' : 'document_loading'};
  }
  let el = null;
  try { el = document.querySelector(condition.selector || 'body'); }
  catch { return {...result, invalid_selector: true, reason: 'invalid_selector'}; }
  let visible = false, enabled = false, hit = false, box = null, point = null;
  if (el) {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    const clipped = {left: Math.max(0,r.left), top: Math.max(0,r.top),
      right: Math.min(innerWidth,r.right), bottom: Math.min(innerHeight,r.bottom)};
    visible = el.isConnected && r.width > 0 && r.height > 0 && cs.visibility === 'visible' &&
      cs.display !== 'none' && Number(cs.opacity) > 0 &&
      clipped.right > clipped.left && clipped.bottom > clipped.top &&
      (!el.checkVisibility || el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true}));
    enabled = !el.matches(':disabled') && !el.closest('[inert],[aria-disabled="true"]');
    if (condition.editable) enabled = enabled && !el.readOnly &&
      (el.matches('textarea') || el.isContentEditable || el.tagName === 'INPUT' &&
        ['text','search','url','tel','email','password','number'].includes(el.type));
    const x = (clipped.left+clipped.right)/2, y = (clipped.top+clipped.bottom)/2;
    const top = visible ? document.elementFromPoint(x,y) : null;
    hit = !!top && (top === el || el.contains(top)) && cs.pointerEvents !== 'none';
    point = [x,y];
    box = [r.x,r.y,r.width,r.height].map(v => Math.round(v*2)/2);
  }
  const textMatched = !condition.text || !!el && (el.innerText || '').includes(condition.text);
  if (condition.identity) {
    const name = el && (el.getAttribute('aria-label') || el.innerText || el.value ||
      el.getAttribute('title') || '').trim().replace(/\s+/g,' ').slice(0,60);
    if (!el || name !== condition.identity.name || el.tagName.toLowerCase() !== condition.identity.tag ||
        (el.getAttribute('href') || '').slice(0,80) !== condition.identity.href)
      return {...result, stale_target:true, reason:'target_changed'};
  }
  const matched = textMatched && (state === 'hidden' || !condition.selector || !!el) && (state === 'hidden' ? !visible :
    state === 'actionable' ? visible && enabled && hit :
    state === 'visible' ? visible : state === 'complete' ? result.load_complete : result.dom_ready);
  return {...result, matched, visible, enabled, hit, box, point,
    reason: matched ? 'condition_met' : !textMatched ? 'text_not_matched' :
      !el ? 'target_missing' : !visible ? 'target_not_visible' : !enabled ? 'target_disabled' :
      !hit ? 'target_covered' : 'document_loading'};
})
"""


def wait_for(cdp, condition: dict) -> dict:
    allowed = {'selector', 'text', 'url', 'state', 'timeout_ms', 'stable_ms',
               'document_token', 'identity', 'editable'}
    if not isinstance(condition, dict) or set(condition) - allowed:
        raise ValueError('unsupported page wait condition')
    condition = dict(condition)
    state = condition.setdefault('state', 'actionable' if condition.get('selector') else 'dom')
    if state not in ('dom', 'actionable', 'visible', 'hidden', 'complete'):
        raise ValueError('state must be dom, actionable, visible, hidden or complete')
    if state in ('actionable', 'visible', 'hidden') and not condition.get('selector'):
        raise ValueError('this wait state requires a selector')
    for key, limit in (('selector',2048), ('text',2000), ('url',4096), ('document_token',100)):
        if key in condition and (not isinstance(condition[key], str) or not condition[key] or len(condition[key]) > limit):
            raise ValueError(f'invalid {key} in page wait')
    for key, default, maximum in (('timeout_ms',10000,30000), ('stable_ms',100 if state == 'actionable' else 0,1000)):
        value = condition.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or not 0 <= value <= maximum:
            raise ValueError(f'{key} must be between 0 and {maximum}')
        condition[key] = value
    start = time.monotonic()
    deadline = start + condition['timeout_ms']/1000
    script = '(' + PROBE_JS + ')(' + json.dumps(condition,separators=(',',':')) + ')'
    stable_since, signature, last = None, None, {}
    while True:
        try:
            last = cdp.evaluate(script,
                                timeout=max(.01,min(1,deadline-time.monotonic())))
            if not isinstance(last,dict):
                raise RuntimeError('page readiness probe returned no state')
            if last.get('invalid_selector'):
                raise ValueError('invalid CSS selector in page wait')
            if last.get('stale_document') or last.get('stale_target'):
                return {'ok':False,'action_sent':False,'error_code':'STALE_OBSERVATION',
                        'error':'Page or target changed; observe again before acting.',
                        'next_action':'observe','state':last}
            if last.get('matched'):
                current = (last.get('document_token'),last.get('url'),last.get('box'),last.get('point'))
                if signature != current:
                    signature, stable_since = current, time.monotonic()
                if time.monotonic()-stable_since >= condition['stable_ms']/1000:
                    return {'ok':True,'condition_met':True,'action_sent':False,
                            'waited_ms':round((time.monotonic()-start)*1000,2),
                            'state':last, 'readiness':readiness(last.get('ready','loading'))}
            else:
                signature, stable_since = None, None
        except RuntimeError as exc:
            # Navigation can destroy a JS context. Retry observation only, never an action.
            if not any(word in str(exc).lower() for word in ('context', 'navigat')):
                raise
            signature, stable_since, last = None, None, {'reason':'navigation_in_progress'}
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            return {'ok':False,'action_sent':False,'error_code':'PAGE_CONDITION_TIMEOUT',
                    'error':'Requested page condition was not met; no click, typing or navigation was sent.',
                    'waited_ms':round((time.monotonic()-start)*1000,2),'state':last,
                    'next_action':'observe'}
        time.sleep(min(.05,remaining))
