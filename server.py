import os
import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
import time
import threading
import sqlite3
import requests
from datetime import datetime
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn
from collections import deque
from dotenv import load_dotenv

# ==========================================
# 🔐 1. โหลดการตั้งค่าลับจากไฟล์ .env
# ==========================================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_NAME = "bot_settings.db"

# ==========================================
# ⚙️ 2. ระบบพื้นฐาน (Logs & Database)
# ==========================================
bot_logs = deque(maxlen=100)
bot_thread = None
bot_running = False
market_status = {} # 🌟 [เพิ่มใหม่] ตัวแปรเก็บค่า Indicator ส่งให้หน้าเว็บ

def add_log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    bot_logs.append(log_entry)

def send_telegram(message):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "ใส่_TOKEN_ของคุณตรงนี้_ไม่ต้องมีเครื่องหมายคำพูด": return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try: requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except: pass

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS portfolio
                 (symbol TEXT PRIMARY KEY, risk REAL, tp INTEGER, sl INTEGER, trailing INTEGER, strategy TEXT)''')
    conn.commit()
    conn.close()

def get_portfolio():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM portfolio")
    rows = c.fetchall()
    conn.close()
    return {row['symbol']: dict(row) for row in rows}

def auto_calculate_settings(symbol):
    if not mt5.initialize(): return None
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None: return None
    spread = sym_info.spread if sym_info.spread > 0 else 200
    sl_calculated = max(int(spread * 5), 100)
    return {"risk": 1.0, "tp": int(sl_calculated * 2), "sl": sl_calculated, "trailing": int(sl_calculated * 0.5), "strategy": "AUTO_DETECT"}

# ==========================================
# 🧠 3. สมองของบอท (Trading Logic & Strategies)
# ==========================================
def calculate_lot(symbol, sl_points, risk_percent):
    account = mt5.account_info()
    sym_info = mt5.symbol_info(symbol)
    if not account or not sym_info or sl_points == 0: return 0.01
    loss_value = sl_points * sym_info.trade_tick_value
    if loss_value == 0: return sym_info.volume_min
    lot = round((account.balance * (risk_percent / 100)) / loss_value / sym_info.volume_step) * sym_info.volume_step
    return round(max(sym_info.volume_min, min(lot, sym_info.volume_max)), 2)

def get_signal(symbol, timeframe, strategy):
    global market_status
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, 250)
    if rates is None or len(rates) < 200: return "WAIT"
    df = pd.DataFrame(rates)
    
    # 🌟 [เพิ่มใหม่] คำนวณ Indicator ทั้งหมดเตรียมไว้โชว์บนเว็บ
    df.ta.adx(length=14, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.ema(length=10, append=True)
    df.ta.ema(length=50, append=True)
    df.ta.macd(append=True)
    df.ta.ema(length=200, append=True)
    df.ta.bbands(length=20, std=2, append=True)
    df.ta.donchian(lower_length=20, upper_length=20, append=True)
    df.ta.stoch(append=True)
    
    last, prev = df.iloc[-2], df.iloc[-3]
    
    # ดึงค่ามาเตรียมโชว์
    adx_val = round(last.get('ADX_14', 0), 2) if not pd.isna(last.get('ADX_14')) else 0
    rsi_val = round(last.get('RSI_14', 0), 2) if not pd.isna(last.get('RSI_14')) else 0
    price_val = round(last['close'], 5)

    # เช็คระบบ AUTO_DETECT
    actual_strat = strategy
    if strategy == "AUTO_DETECT":
        actual_strat = "Trend_ADX_EMA" if adx_val >= 25 else "Bollinger_Bands"
            
    signal = "WAIT"
    try:
        if actual_strat == "Trend_ADX_EMA":
            if adx_val >= 25:
                if prev['EMA_10'] <= prev['EMA_50'] and last['EMA_10'] > last['EMA_50']: signal = "BUY"
                elif prev['EMA_10'] >= prev['EMA_50'] and last['EMA_10'] < last['EMA_50']: signal = "SELL"
                
        elif actual_strat == "Bollinger_Bands":
            if not pd.isna(last.get('BBL_20_2.0')):
                if last['close'] < last['BBL_20_2.0']: signal = "BUY"
                elif last['close'] > last['BBU_20_2.0']: signal = "SELL"
                
        elif actual_strat == "MACD_EMA200":
            if not pd.isna(last.get('EMA_200')) and not pd.isna(last.get('MACD_12_26_9')):
                if last['close'] > last['EMA_200'] and prev['MACD_12_26_9'] <= prev['MACDs_12_26_9'] and last['MACD_12_26_9'] > last['MACDs_12_26_9']: signal = "BUY"
                elif last['close'] < last['EMA_200'] and prev['MACD_12_26_9'] >= prev['MACDs_12_26_9'] and last['MACD_12_26_9'] < last['MACDs_12_26_9']: signal = "SELL"
                
        elif actual_strat == "Donchian_Breakout":
            if not pd.isna(last.get('DCU_20_20')):
                if last['close'] > prev['DCU_20_20']: signal = "BUY"
                elif last['close'] < prev['DCL_20_20']: signal = "SELL"
                
        elif actual_strat == "Stoch_RSI":
            if not pd.isna(last.get('STOCHk_14_3_3')) and not pd.isna(last.get('RSI_14')):
                if last['RSI_14'] < 30 and prev['STOCHk_14_3_3'] <= prev['STOCHd_14_3_3'] and last['STOCHk_14_3_3'] > last['STOCHd_14_3_3'] and last['STOCHk_14_3_3'] < 20: signal = "BUY"
                elif last['RSI_14'] > 70 and prev['STOCHk_14_3_3'] >= prev['STOCHd_14_3_3'] and last['STOCHk_14_3_3'] < last['STOCHd_14_3_3'] and last['STOCHk_14_3_3'] > 80: signal = "SELL"
    except Exception: pass
    
    # 🌟 [เพิ่มใหม่] เก็บค่าลง Global Variable เพื่อให้ API ดึงไปโชว์
    market_status[symbol] = {
        "price": price_val,
        "adx": adx_val,
        "rsi": rsi_val,
        "signal": signal,
        "strat": actual_strat
    }
    
    return signal

def bot_loop():
    global bot_running
    mt5.initialize()
    add_log("🟢 [Backend] บอทเริ่มทำงานแล้ว! ระบบพร้อมลุยตลาด")
    
    while bot_running:
        add_log("-" * 40)
        add_log("🤖 [เริ่มรอบสแกนตลาดใหม่]")
        portfolio = get_portfolio()
        
        for symbol, settings in portfolio.items():
            if not bot_running: break
            
            # --- 1. จัดการออร์เดอร์ค้าง & Trailing Stop ---
            positions = mt5.positions_get(symbol=symbol)
            if positions:
                point = mt5.symbol_info(symbol).point
                for pos in positions:
                    current_price = mt5.symbol_info_tick(symbol).bid if pos.type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(symbol).ask
                    if pos.type == mt5.ORDER_TYPE_BUY:
                        new_sl = current_price - (settings["trailing"] * point)
                        if current_price - pos.price_open > (settings["trailing"] * point) and (pos.sl < new_sl or pos.sl == 0):
                            mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "sl": new_sl, "tp": pos.tp})
                            add_log(f"🛡️ {symbol}: ขยับ SL ป้องกันกำไรไม้ BUY ไปที่ {new_sl:.5f}")
                    elif pos.type == mt5.ORDER_TYPE_SELL:
                        new_sl = current_price + (settings["trailing"] * point)
                        if pos.price_open - current_price > (settings["trailing"] * point) and (pos.sl > new_sl or pos.sl == 0):
                            mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "sl": new_sl, "tp": pos.tp})
                            add_log(f"🛡️ {symbol}: ขยับ SL ป้องกันกำไรไม้ SELL ไปที่ {new_sl:.5f}")
                
                # แม้จะมีออร์เดอร์อยู่ ก็เรียก get_signal เพื่อให้หน้าเว็บดึงค่าอัปเดตไปโชว์ได้
                get_signal(symbol, mt5.TIMEFRAME_M15, settings["strategy"]) 
                continue 
            
            # --- 2. สแกนหาสัญญาณเข้าเทรด ---
            signal = get_signal(symbol, mt5.TIMEFRAME_M15, settings["strategy"])
            if signal in ["BUY", "SELL"]:
                lot = calculate_lot(symbol, settings["sl"], settings["risk"])
                tick = mt5.symbol_info_tick(symbol)
                point = mt5.symbol_info(symbol).point
                
                if signal == "BUY":
                    price = tick.ask
                    sl, tp = price - (settings["sl"] * point), price + (settings["tp"] * point)
                    type_order = mt5.ORDER_TYPE_BUY
                else:
                    price = tick.bid
                    sl, tp = price + (settings["sl"] * point), price - (settings["tp"] * point)
                    type_order = mt5.ORDER_TYPE_SELL
                
                req = {
                    "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(lot),
                    "type": type_order, "price": price, "sl": sl, "tp": tp, "deviation": 20,
                    "magic": 123456, "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC
                }
                
                add_log(f"   💸 {symbol}: เจอสัญญาณ {signal}! ยิงออร์เดอร์ {lot} Lot...")
                res = mt5.order_send(req)
                
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    add_log(f"   🎉 สำเร็จ! รหัสออร์เดอร์: {res.order}")
                    send_telegram(f"✅ [{symbol}] เปิดออร์เดอร์สำเร็จ!\nสัญญาณ: {signal} ({settings['strategy']})\nLot: {lot}\nราคาเข้า: {price:.5f}")
                else:
                    add_log(f"   ❌ ล้มเหลว! Error Code: {res.retcode if res else 'Unknown'}")
                
        add_log("⏳ รอ 60 วินาที เพื่อตรวจสอบรอบถัดไป...")
        for _ in range(30):
            if not bot_running: break
            time.sleep(2)
            
    add_log("🛑 [Backend] บอทหยุดทำงานอย่างปลอดภัยแล้ว!")

# ==========================================
# 🌐 4. FastAPI (Backend API & Web Server)
# ==========================================
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🌟 ดึงรหัสผ่านจาก .env (ถ้าไม่ได้ตั้งไว้ จะใช้ admin1234 เป็นค่าเริ่มต้น)
BOT_PASSWORD = os.getenv("BOT_PASSWORD", "admin1234")
API_TOKEN = f"secret_token_{BOT_PASSWORD}" # สร้างกุญแจเสมือน

# 🛡️ ระบบตรวจสอบกุญแจ (ยามเฝ้าประตู API)
def verify_token(authorization: str = Header(None)):
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized - กรุณาเข้าสู่ระบบ")

class LoginData(BaseModel):
    password: str

class UpdateSetting(BaseModel):
    strategy: str
    risk: float
    tp: int
    sl: int
    trailing: int

class AddSymbol(BaseModel):
    symbol: str

@app.get("/")
def serve_frontend():
    if os.path.exists("index.html"): return FileResponse("index.html")
    else: return {"error": "ไม่พบไฟล์ index.html ในโฟลเดอร์เดียวกัน"}

# 🌟 API สำหรับเช็ครหัสผ่าน
@app.post("/api/login")
def login(data: LoginData):
    if data.password == BOT_PASSWORD:
        return {"status": "success", "token": API_TOKEN}
    raise HTTPException(status_code=401, detail="รหัสผ่านไม่ถูกต้อง")

# 🔒 ใส่ Depends(verify_token) เพื่อล็อคประตูทุก API!
@app.get("/api/status", dependencies=[Depends(verify_token)])
def get_status():
    account_data = {"balance": 0.0, "equity": 0.0, "profit": 0.0}
    positions_data = {}
    
    if mt5.initialize():
        acc = mt5.account_info()
        if acc:
            account_data = {"balance": round(acc.balance, 2), "equity": round(acc.equity, 2), "profit": round(acc.profit, 2)}
        
        positions = mt5.positions_get()
        if positions:
            for pos in positions:
                if pos.symbol not in positions_data:
                    positions_data[pos.symbol] = {"profit": 0.0, "volume": 0.0, "type": "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"}
                positions_data[pos.symbol]["profit"] += pos.profit
                positions_data[pos.symbol]["volume"] += pos.volume

    return {
        "bot_running": bot_running, 
        "portfolio": get_portfolio(), 
        "strategies": ["AUTO_DETECT", "Trend_ADX_EMA", "Bollinger_Bands", "MACD_EMA200", "Donchian_Breakout", "Stoch_RSI"],
        "market_status": market_status,
        "account_info": account_data,
        "positions_data": positions_data
    }

@app.get("/api/logs", dependencies=[Depends(verify_token)])
def get_logs():
    return {"logs": list(bot_logs)}

@app.post("/api/toggle", dependencies=[Depends(verify_token)])
def toggle_bot():
    global bot_running, bot_thread
    if bot_running:
        bot_running = False
        add_log("⏳ กำลังสั่งหยุดบอท...")
    else:
        bot_running = True
        bot_thread = threading.Thread(target=bot_loop)
        bot_thread.start()
    return {"status": "success", "bot_running": bot_running}

@app.post("/api/portfolio", dependencies=[Depends(verify_token)])
def add_symbol(data: AddSymbol):
    symbol = data.symbol.strip().upper()
    settings = auto_calculate_settings(symbol)
    if settings is None: raise HTTPException(status_code=400, detail="ไม่พบคู่เงินนี้ใน MT5")
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO portfolio VALUES (?, ?, ?, ?, ?, ?)",
              (symbol, settings["risk"], settings["tp"], settings["sl"], settings["trailing"], settings["strategy"]))
    conn.commit()
    conn.close()
    add_log(f"➕ เพิ่มคู่เงิน {symbol} สำเร็จ!")
    return {"status": "success"}

@app.put("/api/portfolio/{symbol}", dependencies=[Depends(verify_token)])
def update_symbol(symbol: str, data: UpdateSetting):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE portfolio SET strategy=?, risk=?, tp=?, sl=?, trailing=? WHERE symbol=?", 
              (data.strategy, data.risk, data.tp, data.sl, data.trailing, symbol))
    conn.commit()
    conn.close()
    add_log(f"💾 อัปเดตการตั้งค่าคู่เงิน {symbol} สำเร็จ!")
    return {"status": "success"}

@app.delete("/api/portfolio/{symbol}", dependencies=[Depends(verify_token)])
def delete_symbol(symbol: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM portfolio WHERE symbol=?", (symbol,))
    conn.commit()
    conn.close()
    add_log(f"🗑️ ลบคู่เงิน {symbol} ออกจากพอร์ตแล้ว")
    return {"status": "success"}

@app.post("/api/close/{symbol}", dependencies=[Depends(verify_token)])
def close_order(symbol: str):
    positions = mt5.positions_get(symbol=symbol)
    if not positions: return {"status": "error", "message": "ไม่มีออร์เดอร์เปิดอยู่"}
    success_count = 0
    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        type_order = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if type_order == mt5.ORDER_TYPE_SELL else tick.ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": pos.volume, "type": type_order,
            "position": pos.ticket, "price": price, "deviation": 20, "magic": 123456,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC
        }
        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE: success_count += 1
            
    if success_count > 0:
        add_log(f"🛑 [Web] กดปุ่มปิดออร์เดอร์ {symbol} สำเร็จ ({success_count} ไม้)")
        send_telegram(f"🛑 ปิดออร์เดอร์ {symbol} ด้วยมือ (Manual Close)")
        return {"status": "success"}
    return {"status": "error", "message": "โบรกเกอร์ปฏิเสธคำสั่งปิด"}

if __name__ == "__main__":
    init_db()
    print("🚀 เริ่มรัน Backend API และ Web Server ที่ http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)