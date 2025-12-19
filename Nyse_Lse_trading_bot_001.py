import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import requests
import time
from datetime import datetime, timedelta
import os
import sys
from typing import Tuple, Optional, List, Dict, Any

# === IMPORT CREDENTIALS & SETTINGS ===
try:
    import credentials as creds
except ImportError:
    print("❌ ERROR: 'credentials.py' not found. Please create it with your login details.")
    sys.exit()

# === Configuration ===
# --- MetaTrader 5 Connection ---
MT5_PATH = "C:\\Program Files\\MetaTrader 5 IC Markets Global\\terminal64.exe"

# === GLOBAL SETTINGS ===
COUNTDOWN_TIMER_SECONDS = 300  # Time between analysis cycles
TIMEFRAME = mt5.TIMEFRAME_H4
TIMEFRAME_STR = "H4" # Text label for Telegram

# === ATR Settings (Dynamic SL/TP) ===
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5       # Stop Loss = 1.5 x ATR
ATR_TP_MULTIPLIER = 2.0       # Take Profit = 2.0 x ATR
ATR_PROXIMITY_MULTIPLIER = 0.5 

# === Time Schedule Settings (LSE Hours) ===
TRADING_START_TIME = "08:05"  
TRADING_END_TIME = "20:55"    
SERVER_TIMEZONE_OFFSET_HOURS = 2  

# === FILE PATHS (Dynamic) ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SYMBOL_CONFIG_FILENAME = "5k_lse_nyse_symbol_config_gemini_v3.csv"

# === Global Variables ===
BOT_START_TIME = None
LAST_STATUS_UPDATE = None
analysis_cycles = 0
ALL_SYMBOLS_CONFIG = {}  
SYMBOL_STATES = {}

# === Telegram Functions ===
def send_telegram_message(message: str) -> None:
    """Sends a message to the main Telegram bot (status/alerts)."""
    url = f"https://api.telegram.org/bot{creds.MAIN_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={'chat_id': creds.MAIN_CHAT_ID, 'text': message})
    except Exception as e:
        print(f"Main Telegram Bot Error: {e}")

def send_trade_execution_message(message: str) -> None:
    """Sends detailed trade execution messages to the trade Telegram bot."""
    url = f"https://api.telegram.org/bot{creds.TRADE_BOT_TOKEN}/sendMessage"
    try:
        payload = {
            'chat_id': creds.TRADE_CHAT_ID,
            'text': message,
            'parse_mode': 'Markdown'
        }
        requests.post(url, data=payload, timeout=5)
    except Exception as e:
        print(f"Trade Execution Bot Error: {e}")

# === Load Configuration Files ===
def load_symbol_config() -> Dict[str, Dict[str, Any]]:
    """Loads the symbol configuration from the CSV in the local folder."""
    csv_file_path = os.path.join(SCRIPT_DIR, SYMBOL_CONFIG_FILENAME)
    print(f"📁 Loading Symbol Config from: {csv_file_path}")
    
    if not os.path.exists(csv_file_path):
        error_msg = f"❌ Config file not found at: {csv_file_path}. Bot stopped."
        print(error_msg)
        send_telegram_message(error_msg)
        return {}
    
    try:
        df = pd.read_csv(csv_file_path)
        symbols_config = {}
        
        for index, row in df.iterrows():
            symbol = row['instrument']
            
            # Apply Account Lot Multiplier from CREDENTIALS file
            base_volume = float(row['base_volume']) * creds.ACCOUNT_LOT_MULTIPLIER
            
            daily_bias = str(row['DAILY_BIAS']).upper().strip()
            
            symbols_config[symbol] = {
                'base_volume': base_volume,
                'daily_bias': daily_bias
            }
            
            # Initialize symbol state if not exists
            if symbol not in SYMBOL_STATES:
                SYMBOL_STATES[symbol] = {
                    'trades_executed': 0,
                    'consecutive_losses': 0,
                    'last_trade_close_time': None,
                    'last_skipped_reason': None
                }
            
            print(f"✅ Loaded {symbol}: Bias={daily_bias}, Base Lot={base_volume:.2f}")
        
        print(f"📊 Total symbols loaded: {len(symbols_config)}")
        return symbols_config
        
    except Exception as e:
        msg = f"❌ Error loading symbol config: {e}"
        print(msg)
        send_telegram_message(msg)
        return {}

# === Time Check Function ===
def is_trading_active() -> bool:
    """Checks if current time is within the allowed trading window."""
    try:
        current_time = datetime.now().time()
        start_time = datetime.strptime(TRADING_START_TIME, "%H:%M").time()
        end_time = datetime.strptime(TRADING_END_TIME, "%H:%M").time()
        return start_time <= current_time <= end_time
    except ValueError as e:
        print(f"❌ Time config error: {e}")
        return False

# === Status Update Function ===
def send_status_update():
    """Sends hourly status update with bot statistics and balance."""
    global BOT_START_TIME, LAST_STATUS_UPDATE, analysis_cycles
    
    current_time = datetime.now()
    
    # Calculate uptime
    uptime = current_time - BOT_START_TIME
    hours, remainder = divmod(uptime.total_seconds(), 3600)
    minutes, _ = divmod(remainder, 60)
    
    account_info = mt5.account_info()
    balance = account_info.balance if account_info else 0.0
    balance_str = f"{balance:,.0f}"

    # UPDATED MESSAGE FORMAT HERE
    status_message = (
        f"🤖 **UK100_bot** 🤖\n"
        f"📉 Timeframe: {TIMEFRAME_STR}\n"
        f"💵 Balance: ${balance_str}\n"
        f"⚡ Multiplier: {creds.ACCOUNT_LOT_MULTIPLIER}x\n"
        f"⏰ Uptime: {int(hours)}h {int(minutes)}m\n"
        f"🔄 Cycles: {analysis_cycles}\n"
        f"────────────────────\n"
    )
    
    # Add simple symbol summary
    active_count = 0
    sleeping_count = 0
    
    for symbol, state in SYMBOL_STATES.items():
        if state['last_skipped_reason'] and "Cooldown" in str(state['last_skipped_reason']):
             active_count += 1 
        elif not is_trading_active():
             sleeping_count += 1
        else:
             active_count += 1

    status_message += f"✅ Active/Cooling: {active_count}\n💤 Sleeping: {sleeping_count}"
    
    print(f"\n📢 Sending status update...")
    send_telegram_message(status_message)
    LAST_STATUS_UPDATE = current_time

def check_status_update():
    global LAST_STATUS_UPDATE
    current_time = datetime.now()
    if LAST_STATUS_UPDATE is None:
        send_status_update()
        return
    if (current_time - LAST_STATUS_UPDATE).total_seconds() >= 3600:
        send_status_update()

# === Trading Rules & Logic ===
def is_enough_time_since_last_trade(symbol: str) -> Tuple[bool, float]:
    """Checks for 10 min cooldown after trade close."""
    state = SYMBOL_STATES[symbol]
    
    if state['last_trade_close_time'] is None:
        # Initial check of history if we haven't traded yet this session
        try:
            history = mt5.history_deals_get(datetime.now() - timedelta(days=1), datetime.now())
            if history:
                symbol_trades = [d for d in history if d.symbol == symbol and d.entry == 1]
                if symbol_trades:
                    symbol_trades.sort(key=lambda x: x.time_msc, reverse=True)
                    latest = symbol_trades[0]
                    state['last_trade_close_time'] = datetime.fromtimestamp(latest.time_msc/1000) - timedelta(hours=SERVER_TIMEZONE_OFFSET_HOURS)
        except: pass

    if state['last_trade_close_time']:
        diff = (datetime.now() - state['last_trade_close_time']).total_seconds() / 60
        if diff < 10: return True, diff
    return False, 0.0

def calculate_sl_tp(price: float, trade_type: str, atr_value: float) -> Tuple[float, float]:
    sl_dist = atr_value * ATR_SL_MULTIPLIER
    tp_dist = atr_value * ATR_TP_MULTIPLIER
    
    if trade_type == "BUY":
        sl = price - sl_dist
        tp = price + tp_dist
    else: # SELL
        sl = price + sl_dist
        tp = price - tp_dist
        
    return round(sl, 5), round(tp, 5)

def execute_trade(symbol: str, trade_type: str, price: float, volume: float, sl: float, tp: float, atr_used: float):
    state = SYMBOL_STATES[symbol]
    
    info = mt5.symbol_info(symbol)
    filling = mt5.ORDER_FILLING_FOK
    if info:
        if (info.filling_mode & 2) != 0: filling = mt5.ORDER_FILLING_IOC
        elif (info.filling_mode & 1) != 0: filling = mt5.ORDER_FILLING_FOK

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if trade_type == "BUY" else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": 1001,
        "comment": "Bot ATR V5",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling
    }

    res = mt5.order_send(request)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        state['trades_executed'] += 1
        print(f"✅ {symbol} {trade_type} Executed!")
        msg = (f"✅ **{symbol} {trade_type} EXECUTION**\n"
               f"Price: {price}\nVol: {volume}\n"
               f"ATR: {atr_used:.4f}\n"
               f"SL: {sl}\nTP: {tp}")
        send_trade_execution_message(msg)
    else:
        err = res.comment if res else "Unknown"
        print(f"❌ {symbol} Exec Failed: {err} (Retcode: {res.retcode if res else 'None'})")

# === Analysis Engine ===
def run_symbol_analysis(symbol: str):
    config = ALL_SYMBOLS_CONFIG[symbol]
    state = SYMBOL_STATES[symbol]

    # --- 1. Permission Checks ---
    direction = config['daily_bias']
    if direction == "NONE":
        state['last_skipped_reason'] = "Daily Bias NONE"
        return

    # --- 2. Cooldown Check ---
    skip, mins = is_enough_time_since_last_trade(symbol)
    if skip:
        state['last_skipped_reason'] = f"Cooldown: {10-mins:.1f}m left"
        return

    # --- 3. Technical Analysis ---
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, 100)
    if rates is None or len(rates) < 50: 
        return
        
    df = pd.DataFrame(rates)
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    
    # ATR
    df['h-l'] = df['high'] - df['low']
    df['h-pc'] = abs(df['high'] - df['close'].shift(1))
    df['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    df['atr'] = df['tr'].rolling(ATR_PERIOD).mean()
    
    # BB
    df['ma'] = df['close'].rolling(20).mean()
    df['std'] = df['close'].rolling(20).std()
    df['upper'] = df['ma'] + (2 * df['std'])
    df['lower'] = df['ma'] - (2 * df['std'])
    
    # RSI
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1/14, min_periods=14).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, min_periods=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    last = df.iloc[-1]
    atr_value = last['atr']
    signal = "NONE"
    
    # === STRATEGY ===
    if last['rsi'] < 35 and last['low'] <= last['lower']:
        signal = "BUY"
    elif last['rsi'] > 65 and last['high'] >= last['upper']:
        signal = "SELL"
        
    if signal != "NONE":
        print(f"📊 {symbol} | RSI: {last['rsi']:.1f} | ATR: {atr_value:.4f} | Signal: {signal}")

    # --- 4. Execution ---
    positions = mt5.positions_get(symbol=symbol)
    if positions:
        state['last_skipped_reason'] = "Position Open"
        return

    tick = mt5.symbol_info_tick(symbol)
    if not tick: return

    if signal != "NONE":
        vol = config['base_volume']
        price = tick.ask if signal == "BUY" else tick.bid
        sl, tp = calculate_sl_tp(price, signal, atr_value)
        
        if signal == "BUY" and direction in ["BUY", "BOTH"]:
            execute_trade(symbol, "BUY", price, vol, sl, tp, atr_value)
        elif signal == "SELL" and direction in ["SELL", "BOTH"]:
            execute_trade(symbol, "SELL", price, vol, sl, tp, atr_value)

# === Main Loop ===
if __name__ == "__main__":
    if not mt5.initialize(path=MT5_PATH):
        print("❌ MT5 Init Failed: check the path in the script.")
        sys.exit()
    if not mt5.login(creds.MT5_LOGIN, creds.MT5_PASSWORD, creds.MT5_SERVER):
        print("❌ MT5 Login Failed: check credentials.py.")
        mt5.shutdown()
        sys.exit()

    ALL_SYMBOLS_CONFIG = load_symbol_config()
    BOT_START_TIME = datetime.now()
    
    msg = (f"🤖 **UK100_bot Started**\n"
           f"⚡ Lot Multiplier: {creds.ACCOUNT_LOT_MULTIPLIER}x\n"
           f"⏰ Window: {TRADING_START_TIME}-{TRADING_END_TIME}\n"
           f"📊 Symbols: {len(ALL_SYMBOLS_CONFIG)}")
    send_telegram_message(msg)
    
    try:
        while True:
            now_str = datetime.now().strftime('%H:%M:%S')
            
            if is_trading_active():
                print(f"\n🔄 Cycle: {now_str} [ACTIVE]")
                for symbol in ALL_SYMBOLS_CONFIG:
                    run_symbol_analysis(symbol)
            else:
                print(f"\r💤 Sleep Mode (Outside {TRADING_START_TIME}-{TRADING_END_TIME}): {now_str}", end="")
            
            analysis_cycles += 1
            check_status_update()
            time.sleep(COUNTDOWN_TIMER_SECONDS)
            
    except KeyboardInterrupt:
        print("\n🛑 Bot Stopped Manually")
        send_telegram_message("🛑 Bot Stopped Manually")
        mt5.shutdown()
    except Exception as e:
        print(f"\n🚨 Crash: {e}")
        send_telegram_message(f"🚨 Bot Crashed: {e}")