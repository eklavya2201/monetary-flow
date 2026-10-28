"""Optional bring-your-own LLM second opinion. It can only VETO a trade the rules already
approved; it never creates trades, sizes them, or loosens a limit. Any failure = veto."""
import json
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

import httpx

SYSTEM = (
    "You review one proposed long trade on an Indian stock for a small cash-only account. "
    "The technical setup and position size are already computed by fixed rules; do not redo them. "
    "Your only job: veto if recent news or events make the trade unsafe right now (results within 2 days, "
    "fraud/regulatory action, block deals, sudden rating cuts, corporate actions that distort price, "
    "a single unverified rumour driving price). If headlines are empty or routine, approve. "
    "Never approve because of a single bullish article. Reply with JSON only."
)
SCHEMA = {
    "type": "object",
    "properties": {"approve": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["approve", "reason"],
    "additionalProperties": False,
}


def headlines(sym: str, days: int = 4, limit: int = 8) -> list[str]:
    url = f"https://news.google.com/rss/search?q={sym}+NSE+share&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        root = ET.fromstring(httpx.get(url, timeout=10).text)
    except Exception:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for item in root.iter("item"):
        try:
            when = parsedate_to_datetime(item.findtext("pubDate"))
        except Exception:
            continue
        if when >= cutoff:
            out.append(f"{when:%d %b} - {item.findtext('title')}")
    return out[:limit]


def _prompt(plan, why: str, news: list[str]) -> str:
    return (f"Symbol: {plan.symbol}\nEntry {plan.entry}, stop {plan.stop}, target {plan.target}, qty {plan.qty}\n"
            f"Setup: {why}\nHeadlines (last 4 days):\n" + ("\n".join(news) or "(none found)"))


def review(provider: str, model: str, key: str, base_url: str, plan, why: str) -> tuple[bool, str]:
    if provider == "none":
        return True, "no LLM configured (rules only)"
    if not key:
        return False, "LLM provider set but no API key saved"
    news = headlines(plan.symbol)
    try:
        if provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=key, timeout=60)
            r = client.beta.messages.create(
                model=model, max_tokens=1024, system=SYSTEM,
                messages=[{"role": "user", "content": _prompt(plan, why, news)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
            if r.stop_reason == "refusal":
                return False, "LLM declined to review"
            text = next(b.text for b in r.content if b.type == "text")
        else:  # any OpenAI-compatible chat endpoint (OpenAI, OpenRouter, Groq, Gemini-compat...)
            r = httpx.post(f"{base_url.rstrip('/')}/chat/completions", timeout=60,
                           headers={"Authorization": f"Bearer {key}"},
                           json={"model": model, "response_format": {"type": "json_object"},
                                 "messages": [{"role": "system", "content": SYSTEM},
                                              {"role": "user", "content": _prompt(plan, why, news)}]})
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
        verdict = json.loads(text)
        return bool(verdict["approve"]), str(verdict["reason"])[:300]
    except Exception as e:
        return False, f"LLM review failed, vetoing: {type(e).__name__}"
