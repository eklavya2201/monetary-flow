"""FastAPI backend + the mobile web UI. Run: uvicorn monetary_flow.app:app --host 0.0.0.0 --port 8080"""
import hashlib
import hmac
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import Settings
from .engine import Engine, now_ist, session_open
from .store import Store

DATA = Path(os.environ.get("MF_DATA_DIR", "data"))
DATA.mkdir(exist_ok=True)
MASTER = os.environ.get("MF_MASTER_KEY", "")
PASSCODE = os.environ.get("MF_PASSCODE", "")
if len(PASSCODE) < 6:
    raise SystemExit("MF_PASSCODE must be set (6+ characters)")
ARM_PHRASE = "I ACCEPT REAL MONEY RISK"

store = Store(str(DATA / "monetary_flow.db"), MASTER)
engine = Engine(store, DATA)
app = FastAPI(title="Monetary Flow")
TOKEN = hmac.new(MASTER.encode(), PASSCODE.encode(), hashlib.sha256).hexdigest()


def auth(authorization: str = Header("")):
    if not hmac.compare_digest(authorization.removeprefix("Bearer "), TOKEN):
        raise HTTPException(401, "bad passcode")


def loop():
    while True:
        s = store.settings()
        if store.get("running", False):
            engine.cycle()
        time.sleep(s.cycle_minutes * 60)


@app.on_event("startup")
def start():
    threading.Thread(target=loop, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "web" / "index.html")


class Login(BaseModel):
    passcode: str


@app.post("/api/login")
def login(body: Login):
    if not hmac.compare_digest(body.passcode, PASSCODE):
        raise HTTPException(401, "bad passcode")
    return {"token": TOKEN}


@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    s = store.settings()
    return {"settings": asdict(s), "secrets": store.secrets_present(), "running": store.get("running", False),
            "market_open": session_open(now_ist()), "halted": store.get("halted", []),
            "report": engine.report(), "logs": store.logs(40), "curve": store.equity_curve()[-300:]}


class SettingsIn(BaseModel):
    settings: dict
    arm_phrase: str = ""


@app.post("/api/settings", dependencies=[Depends(auth)])
def save_settings(body: SettingsIn):
    current = store.settings()
    merged = Settings(**{**asdict(current), **{k: v for k, v in body.settings.items() if k in asdict(current)}}).clamped()
    if merged.mode == "live" and current.mode != "live":
        have = store.secrets_present()
        if not (have["groww_api_key"] and have["groww_totp_secret"]):
            raise HTTPException(400, "save your Groww API key and TOTP secret first")
        if body.arm_phrase.strip() != ARM_PHRASE:
            raise HTTPException(400, f'type "{ARM_PHRASE}" to switch to live trading')
        store.log("LIVE MODE ARMED", "warn")
    if merged.mode != current.mode and store.trades("mode=? and status in ('PENDING','OPEN','EXITING')", (current.mode,)):
        raise HTTPException(400, f"close the open {current.mode} positions before switching mode")
    store.save_settings(merged)
    return asdict(merged)


class SecretsIn(BaseModel):
    groww_api_key: str | None = None
    groww_totp_secret: str | None = None
    llm_api_key: str | None = None


@app.post("/api/secrets", dependencies=[Depends(auth)])
def save_secrets(body: SecretsIn):
    for name, value in body.model_dump().items():
        if value is not None:
            store.set_secret(name, value.strip())
    return store.secrets_present()


@app.post("/api/test-broker", dependencies=[Depends(auth)])
def test_broker():
    """Read-only check: log in to Groww and read profile, funds, holdings. Places nothing."""
    from .brokers.groww import GrowwBroker
    key, totp = store.secret("groww_api_key"), store.secret("groww_totp_secret")
    if not (key and totp):
        raise HTTPException(400, "save the Groww API key and TOTP secret first")
    g = GrowwBroker(key, totp, store.settings().product)
    try:
        prof = g.profile()
        return {"ok": True, "ucc": prof.get("ucc"), "segments": prof.get("active_segments"),
                "funds": g.funds(), "holdings": len(g.holdings()), "nifty": g.quote("NIFTY")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/engine/{action}", dependencies=[Depends(auth)])
def control(action: str):
    if action == "start":
        store.put("running", True)
        store.log(f"engine started ({store.settings().mode})")
        return {"result": engine.cycle()}
    if action == "stop":
        store.put("running", False)
        store.log("engine stopped (open positions keep their broker stop-loss)")
        return {"result": "stopped"}
    if action == "cycle":
        return {"result": engine.cycle(force=True)}
    if action == "kill":
        return {"result": engine.kill()}
    raise HTTPException(404)
