"""
Hyperliquid Orderbook Viewer — Backend
- L2 Book WebSocket for real-time orderbook
- Trades WebSocket to track active wallets
- Whale tracking: overlay known wallets' open orders on the book
"""

import asyncio
import json
import time
from pathlib import Path
from collections import defaultdict

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import websockets

app = FastAPI()

HL_API = "https://api.hyperliquid.xyz"
HL_WS = "wss://api.hyperliquid.xyz/ws"

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

current_coin: str = "PUMP"
l2_book: dict = {"bids": [], "asks": []}       # L2 aggregated levels
tracked_wallets: set[str] = set()               # wallets to monitor
banned_wallets: set[str] = set()                # wallets flagged red
wallet_orders: dict[str, list] = {}             # wallet -> their open orders
trade_log: list[dict] = []                      # recent trades with wallet info
wallet_stats: dict[str, dict] = defaultdict(    # wallet -> {volume, count, last_seen}
    lambda: {"volume": 0.0, "count": 0, "last_seen": 0, "side_bias": {"buy": 0, "sell": 0}}
)
browser_clients: set[WebSocket] = set()
hl_task = None
trade_task = None
whale_poll_task = None

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

import os
_data_dir = os.environ.get("DATA_DIR", str(Path(__file__).parent / "data"))
DATA_FILE = Path(_data_dir) / "state.json"


def load_state():
    """Load tracked/banned wallets from disk."""
    global tracked_wallets, banned_wallets
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
            tracked_wallets = set(data.get("tracked_wallets", []))
            banned_wallets = set(data.get("banned_wallets", []))
            print(f"[STATE] Loaded {len(tracked_wallets)} tracked, {len(banned_wallets)} banned")
        except Exception as e:
            print(f"[STATE] Failed to load: {e}")


def save_state():
    """Persist tracked/banned wallets to disk."""
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(json.dumps({
            "tracked_wallets": list(tracked_wallets),
            "banned_wallets": list(banned_wallets),
        }, indent=2))
    except Exception as e:
        print(f"[STATE] Failed to save: {e}")


# ---------------------------------------------------------------------------
# L2 Book aggregation + whale overlay
# ---------------------------------------------------------------------------

def build_book_payload(max_levels: int = 30) -> dict:
    """Build the payload to send to browser: L2 book + whale overlays."""
    # Process bids
    bids = []
    for level in l2_book.get("bids", [])[:max_levels]:
        px, sz, n = level.get("px", "0"), level.get("sz", "0"), level.get("n", 0)
        # Find tracked wallets with orders at this price
        whales_here = []
        for wallet, orders in wallet_orders.items():
            for o in orders:
                if o.get("limitPx") == px and o.get("side") == "B":
                    whales_here.append({
                        "wallet": wallet,
                        "sz": o.get("sz", "0"),
                        "oid": o.get("oid"),
                    })
        whales_here.sort(key=lambda x: float(x["sz"]), reverse=True)
        bids.append({"px": px, "sz": sz, "n": n, "whales": whales_here})

    # Process asks
    asks = []
    for level in l2_book.get("asks", [])[:max_levels]:
        px, sz, n = level.get("px", "0"), level.get("sz", "0"), level.get("n", 0)
        whales_here = []
        for wallet, orders in wallet_orders.items():
            for o in orders:
                if o.get("limitPx") == px and o.get("side") == "A":
                    whales_here.append({
                        "wallet": wallet,
                        "sz": o.get("sz", "0"),
                        "oid": o.get("oid"),
                    })
        whales_here.sort(key=lambda x: float(x["sz"]), reverse=True)
        asks.append({"px": px, "sz": sz, "n": n, "whales": whales_here})

    # Top traders by volume
    top_traders = sorted(
        wallet_stats.items(),
        key=lambda x: x[1]["volume"],
        reverse=True,
    )[:20]

    # Whales: top traders by volume, excluding banned
    # Auto-threshold: wallets with volume > median * 5 (or top 30 if not enough data)
    all_vols = sorted(
        [(w, s) for w, s in wallet_stats.items() if w not in banned_wallets],
        key=lambda x: x[1]["volume"],
        reverse=True,
    )
    if all_vols:
        median_vol = all_vols[len(all_vols) // 2][1]["volume"] if len(all_vols) > 2 else 0
        threshold = max(median_vol * 5, 0.001)  # min threshold to avoid noise
        whales = [
            (w, s) for w, s in all_vols
            if s["volume"] >= threshold
        ][:50]
    else:
        whales = []

    return {
        "coin": current_coin,
        "bids": bids,
        "asks": asks,
        "ts": int(time.time() * 1000),
        "tracked_wallets": list(tracked_wallets),
        "banned_wallets": list(banned_wallets),
        "recent_trades": trade_log[-50:],
        "top_traders": [
            {
                "wallet": w,
                "volume": round(s["volume"], 2),
                "count": s["count"],
                "bias": "BUY" if s["side_bias"]["buy"] > s["side_bias"]["sell"] else "SELL",
            }
            for w, s in top_traders
        ],
        "whales": [
            {
                "wallet": w,
                "volume": round(s["volume"], 2),
                "count": s["count"],
                "bias": "BUY" if s["side_bias"]["buy"] > s["side_bias"]["sell"] else "SELL",
                "last_seen": s["last_seen"],
            }
            for w, s in whales
        ],
    }


# ---------------------------------------------------------------------------
# Hyperliquid WebSocket — L2 Book
# ---------------------------------------------------------------------------

async def ws_l2_book(coin: str):
    """Subscribe to L2 book updates."""
    global l2_book
    while True:
        try:
            async with websockets.connect(HL_WS, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "l2Book", "coin": coin},
                }))
                print(f"[L2] Subscribed to {coin}")

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("channel") == "l2Book":
                        data = msg.get("data", {})
                        levels = data.get("levels", [])
                        if len(levels) >= 2:
                            l2_book = {
                                "bids": [{"px": e["px"], "sz": e["sz"], "n": e["n"]}
                                         for e in levels[0]],
                                "asks": [{"px": e["px"], "sz": e["sz"], "n": e["n"]}
                                         for e in levels[1]],
                            }
                            await broadcast_book()

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[L2] Error: {e}. Reconnecting in 3s...")
            await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# Hyperliquid WebSocket — Trades
# ---------------------------------------------------------------------------

async def ws_trades(coin: str):
    """Subscribe to trade feed — tracks active wallets."""
    while True:
        try:
            async with websockets.connect(HL_WS, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": coin},
                }))
                print(f"[TRADES] Subscribed to {coin}")

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("channel") == "trades":
                        trades = msg.get("data", [])
                        for t in trades:
                            trade_entry = {
                                "px": t.get("px"),
                                "sz": t.get("sz"),
                                "side": t.get("side"),
                                "time": t.get("time"),
                                "hash": t.get("hash", ""),
                                "users": t.get("users", []),
                            }
                            trade_log.append(trade_entry)
                            # Keep last 500 trades
                            if len(trade_log) > 500:
                                del trade_log[:100]

                            # Track wallet stats from users field if present
                            for user_addr in t.get("users", []):
                                stats = wallet_stats[user_addr]
                                stats["volume"] += float(t.get("sz", 0))
                                stats["count"] += 1
                                stats["last_seen"] = int(time.time() * 1000)
                                side = t.get("side", "").upper()
                                if side in ("B", "BUY"):
                                    stats["side_bias"]["buy"] += 1
                                elif side in ("A", "SELL"):
                                    stats["side_bias"]["sell"] += 1

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[TRADES] Error: {e}. Reconnecting in 3s...")
            await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# Whale polling — query open orders for tracked wallets
# ---------------------------------------------------------------------------

async def poll_whale_orders():
    """Periodically query open orders for all tracked wallets."""
    while True:
        try:
            if tracked_wallets:
                async with httpx.AsyncClient() as client:
                    for wallet in list(tracked_wallets):
                        try:
                            resp = await client.post(
                                f"{HL_API}/info",
                                json={"type": "openOrders", "user": wallet},
                                timeout=5,
                            )
                            if resp.status_code == 200:
                                all_orders = resp.json()
                                # Filter for current coin
                                coin_orders = [
                                    o for o in all_orders
                                    if o.get("coin") == current_coin
                                ]
                                wallet_orders[wallet] = coin_orders
                        except Exception as e:
                            print(f"[WHALE] Error querying {wallet[:10]}...: {e}")

                await broadcast_book()

            await asyncio.sleep(2)  # Poll every 2s

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[WHALE] Poll error: {e}")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Broadcast to browsers
# ---------------------------------------------------------------------------

last_broadcast = 0.0


async def broadcast_book():
    global last_broadcast
    now = time.time() * 1000
    if now - last_broadcast < 250:
        return
    last_broadcast = now

    if not browser_clients:
        return

    payload = json.dumps({"type": "book", "data": build_book_payload()})
    dead = set()
    for client in browser_clients:
        try:
            await client.send_text(payload)
        except Exception:
            dead.add(client)
    browser_clients.difference_update(dead)


# ---------------------------------------------------------------------------
# WebSocket endpoint for browser
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    browser_clients.add(ws)
    print(f"[WS] Browser connected ({len(browser_clients)} total)")

    # Send current state
    await ws.send_json({"type": "book", "data": build_book_payload()})

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "switch_coin":
                new_coin = data.get("coin", "BTC").upper()
                await switch_coin(new_coin)

            elif msg_type == "track_wallet":
                wallet = data.get("wallet", "").strip().lower()
                if wallet.startswith("0x") and len(wallet) == 42:
                    tracked_wallets.add(wallet)
                    save_state()
                    await ws.send_json({"type": "info", "msg": f"Tracking {wallet[:10]}..."})

            elif msg_type == "untrack_wallet":
                wallet = data.get("wallet", "").strip().lower()
                tracked_wallets.discard(wallet)
                wallet_orders.pop(wallet, None)
                save_state()
                await ws.send_json({"type": "info", "msg": f"Untracked {wallet[:10]}..."})

            elif msg_type == "auto_track_top":
                # Auto-track top N traders by volume
                n = data.get("n", 5)
                top = sorted(wallet_stats.items(), key=lambda x: x[1]["volume"], reverse=True)[:n]
                for addr, _ in top:
                    if addr.startswith("0x"):
                        tracked_wallets.add(addr)
                save_state()
                await ws.send_json({"type": "info", "msg": f"Auto-tracking top {n} traders"})

            elif msg_type == "ban_wallet":
                wallet = data.get("wallet", "").strip().lower()
                if wallet.startswith("0x") and len(wallet) == 42:
                    banned_wallets.add(wallet)
                    save_state()
                    await ws.send_json({"type": "info", "msg": f"Banned {wallet[:10]}..."})

            elif msg_type == "unban_wallet":
                wallet = data.get("wallet", "").strip().lower()
                banned_wallets.discard(wallet)
                save_state()
                await ws.send_json({"type": "info", "msg": f"Unbanned {wallet[:10]}..."})

    except WebSocketDisconnect:
        pass
    finally:
        browser_clients.discard(ws)


async def switch_coin(coin: str):
    """Switch coin — restart HL subscriptions."""
    global hl_task, trade_task, current_coin, l2_book, trade_log
    current_coin = coin
    l2_book = {"bids": [], "asks": []}
    trade_log.clear()
    wallet_orders.clear()

    for task in [hl_task, trade_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    hl_task = asyncio.create_task(ws_l2_book(coin))
    trade_task = asyncio.create_task(ws_trades(coin))

    # Broadcast empty book immediately so UI refreshes
    await broadcast_book()


# ---------------------------------------------------------------------------
# REST: coin list
# ---------------------------------------------------------------------------

coins_cache: list[str] = []
coins_cache_ts: float = 0


@app.get("/api/coins")
async def get_coins():
    global coins_cache, coins_cache_ts
    now = time.time()
    if not coins_cache or now - coins_cache_ts > 300:  # refresh every 5min
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{HL_API}/info",
                    json={"type": "meta"},
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    coins_cache = [u["name"] for u in data.get("universe", [])]
                    coins_cache_ts = now
        except Exception as e:
            print(f"[API] Error fetching coins: {e}")
    return {"coins": coins_cache}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    global hl_task, trade_task, whale_poll_task
    load_state()
    hl_task = asyncio.create_task(ws_l2_book(current_coin))
    trade_task = asyncio.create_task(ws_trades(current_coin))
    whale_poll_task = asyncio.create_task(poll_whale_orders())


# Serve static
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run("server:app", host=host, port=port, reload=False)
