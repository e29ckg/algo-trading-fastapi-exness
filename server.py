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

# 🌟 แผนผังแปลภาษา Timeframe ให้บอทเข้าใจ
TF_MAP = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1
}

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
market_status = {}

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
    # 🌟 สร้างฐานข้อมูลพร้อมคอลัมน์ timeframe
    c.execute('''CREATE TABLE IF NOT EXISTS portfolio
                 (symbol TEXT PRIMARY KEY, risk REAL, tp INTEGER, sl INTEGER, trailing INTEGER, strategy TEXT, timeframe TEXT DEFAULT 'M5')''')
    
    # 🌟 อัปเกรดฐานข้อมูลเก่าให้รองรับ timeframe (ถ้าเคยมีไฟล์ .db เก่าอยู่แล้ว)
    try:
        c.execute("ALTER TABLE portfolio ADD COLUMN timeframe TEXT DEFAULT 'M5'")
    except:
        pass
        
    conn.commit()
    conn.close()

def get_portfolio():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM portfolio")
    rows = c.fetchall()
    conn.close()
    
    portfolio = {}
    for row in rows:
        data = dict(row)
        # ป้องกัน error กรณีดึงข้อมูลเก่าที่ยังไม่มี Timeframe
        if 'timeframe' not in data or not data['timeframe']:
            data['timeframe'] = 'M5'
        portfolio[row['symbol']] = data
    return portfolio

def auto_calculate_settings(symbol):
    if not mt5.initialize(): return None
    mt5.symbol_select(symbol, True) 
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
    
    adx_val = round(last.get('ADX_14', 0), 2) if not pd.isna(last.get('ADX_14')) else 0
    rsi_val = round(last.get('RSI_14', 0), 2) if not pd.isna(last.get('RSI_14')) else 0
    price_val = round(last['close'], 5)

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
        
        elif actual_strat == "Scalping_Fast":
            df.ta.ema(length=5, append=True)
            df.ta.ema(length=13, append=True)
            df.ta.rsi(length=14, append=True)
            
            if not pd.isna(last.get('EMA_13')) and not pd.isna(last.get('RSI_14')):
                if prev['EMA_5'] <= prev['EMA_13'] and last['EMA_5'] > last['EMA_13'] and last['RSI_14'] > 50: 
                    signal = "BUY"
                elif prev['EMA_5'] >= prev['EMA_13'] and last['EMA_5'] < last['EMA_13'] and last['RSI_14'] < 50: 
                    signal = "SELL"   

    except Exception: pass
    
    market_status[symbol] = {
        "price": price_val, "adx": adx_val, "rsi": rsi_val, "signal": signal, "strat": actual_strat
    }
    
    return signal

def bot_loop():
    global bot_running
    mt5.initialize()
    add_log("🟢 [Backend] บอทเริ่มทำงานแล้ว! ระบบพร้อมลุยตลาด")
    
    while bot_running:
        # add_log("-" * 40)
        # add_log("🤖 [เริ่มรอบสแกนตลาดใหม่]")
        portfolio = get_portfolio()
        
        for symbol, settings in portfolio.items():
            if not bot_running: break
            
            # 🌟 แปลงชื่อ Timeframe ที่ตั้งไว้ เป็นรหัสให้ MT5
            tf_str = settings.get("timeframe", "M5")
            tf_mt5 = TF_MAP.get(tf_str, mt5.TIMEFRAME_M5)
            
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
                
                # 🌟 ส่ง Timeframe ที่ตั้งค่าไว้เข้าไปเช็ค
                get_signal(symbol, tf_mt5, settings["strategy"])
                continue 
            
            # --- 2. สแกนหาสัญญาณเข้าเทรด ---
            # 🌟 ส่ง Timeframe ที่ตั้งค่าไว้เข้าไปเช็ค
            signal = get_signal(symbol, tf_mt5, settings["strategy"])
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
                
                add_log(f"   💸 {symbol} ({tf_str}): เจอสัญญาณ {signal}! ยิงออร์เดอร์ {lot} Lot...")
                res = mt5.order_send(req)
                
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    add_log(f"   🎉 สำเร็จ! รหัสออร์เดอร์: {res.order}")
                    send_telegram(f"✅ [{symbol}] เปิดออร์เดอร์สำเร็จ!\nTF: {tf_str}\nสัญญาณ: {signal} ({settings['strategy']})\nLot: {lot}\nราคาเข้า: {price:.5f}")
                else:
                    add_log(f"   ❌ ล้มเหลว! Error Code: {res.retcode if res else 'Unknown'}")
                
        # add_log("⏳ รอ 60 วินาที เพื่อตรวจสอบรอบถัดไป...")
        # 🌟 ปรับให้บอทพักหายใจแค่ 5 วินาที (ลูป 5 รอบ รอบละ 1 วินาที)
        for _ in range(5):
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

BOT_PASSWORD = os.getenv("BOT_PASSWORD", "admin1234")
API_TOKEN = f"secret_token_{BOT_PASSWORD}" 

def verify_token(authorization: str = Header(None)):
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized - กรุณาเข้าสู่ระบบ")

class LoginData(BaseModel):
    password: str

# 🌟 เพิ่ม timeframe ลงใน Model ส่งข้อมูล
class UpdateSetting(BaseModel):
    strategy: str
    risk: float
    tp: int
    sl: int
    trailing: int
    timeframe: str 

class AddSymbol(BaseModel):
    symbol: str

@app.get("/")
def serve_frontend():
    if os.path.exists("index.html"): return FileResponse("index.html")
    else: return {"error": "ไม่พบไฟล์ index.html ในโฟลเดอร์เดียวกัน"}

@app.post("/api/login")
def login(data: LoginData):
    if data.password == BOT_PASSWORD:
        return {"status": "success", "token": API_TOKEN}
    raise HTTPException(status_code=401, detail="รหัสผ่านไม่ถูกต้อง")

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
        "strategies": ["AUTO_DETECT", "Scalping_Fast", "Trend_ADX_EMA", "Bollinger_Bands", "MACD_EMA200", "Donchian_Breakout", "Stoch_RSI"],
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
    symbol = data.symbol.strip() 
    settings = auto_calculate_settings(symbol)
    
    if settings is None: 
        add_log(f"⚠️ บังคับเพิ่มคู่เงิน {symbol} (โหมด Manual)")
        settings = {"risk": 1.0, "tp": 2000, "sl": 3000, "trailing": 500, "strategy": "Scalping_Fast"}
    
    # 🌟 ค่าเริ่มต้น Timeframe ตอนเพิ่มคู่เงิน
    tf = "M5" 
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO portfolio (symbol, risk, tp, sl, trailing, strategy, timeframe) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (symbol, settings["risk"], settings["tp"], settings["sl"], settings["trailing"], settings["strategy"], tf))
    conn.commit()
    conn.close()
    
    add_log(f"➕ เพิ่มคู่เงิน {symbol} ลงระบบสำเร็จ!")
    return {"status": "success"}

@app.put("/api/portfolio/{symbol}", dependencies=[Depends(verify_token)])
def update_symbol(symbol: str, data: UpdateSetting):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # 🌟 บันทึก Timeframe ลงฐานข้อมูล
    c.execute("UPDATE portfolio SET strategy=?, risk=?, tp=?, sl=?, trailing=?, timeframe=? WHERE symbol=?", 
              (data.strategy, data.risk, data.tp, data.sl, data.trailing, data.timeframe, symbol))
    conn.commit()
    conn.close()
    add_log(f"💾 อัปเดตการตั้งค่า {symbol} สำเร็จ! (TF: {data.timeframe})")
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
    # 🌟 เปลี่ยน Host เป็น 0.0.0.0 เพื่อให้เข้าจากมือถือ/คอมเครื่องอื่นผ่าน IP ได้!
    print("🚀 เริ่มรัน Backend API และ Web Server ที่ http://0.0.0.0:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)