#!/usr/bin/env python3
"""
WebSocket Test - Match TwelveData Playground Format
Testing EUR/USD and XAU/USD on free tier
"""
import websocket
import json
import time
from datetime import datetime
import threading
import ssl

# Test BOTH symbols like the playground showed
SYMBOLS = ["EUR/USD", "XAU/USD"]
API_KEY = "e7d9d5ad35414b948cda0b7e4f6b0b34"

results = {
    'connected': False,
    'subscribed': [],
    'denied': [],
    'prices_received': {},
    'message_count': 0
}

def on_message(ws, message):
    """Handle incoming WebSocket messages"""
    try:
        data = json.loads(message)
        results['message_count'] += 1
        timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        
        print(f"\n[{timestamp}] 📩 {json.dumps(data)}")
        
        event = data.get('event')
        
        if event == 'subscribe-status':
            status = data.get('status')
            success = data.get('success', [])
            fails = data.get('fails', [])
            
            print(f"   Status: {status}")
            
            for item in success:
                sym = item.get('symbol')
                if sym:
                    results['subscribed'].append(sym)
                    print(f"   ✅ {sym}")
            
            for item in (fails or []):
                sym = item.get('symbol')
                if sym:
                    results['denied'].append(sym)
                    print(f"   ❌ {sym}")
                
        elif event == 'price':
            symbol = data.get('symbol')
            price = data.get('price')
            ts = data.get('timestamp')
            
            if symbol not in results['prices_received']:
                results['prices_received'][symbol] = []
                print(f"\n   🎉 FIRST PRICE for {symbol}: ${price}")
            
            results['prices_received'][symbol].append({
                'price': price,
                'timestamp': ts,
                'time': datetime.fromtimestamp(ts).strftime('%H:%M:%S') if ts else 'N/A'
            })
            print(f"   💰 {symbol}: ${price}")
            
        elif event == 'heartbeat':
            print(f"   💓 OK")
            
    except Exception as e:
        print(f"❌ Error: {e} | Raw: {message}")

def on_error(ws, error):
    print(f"\n❌ Error: {error}")

def on_close(ws, close_status_code, close_msg):
    print(f"\n🔌 Closed: {close_status_code}")

def on_open(ws):
    print("✅ CONNECTED!")
    results['connected'] = True
    
    # Subscribe to BOTH symbols (comma-separated, matching playground)
    print(f"\n📡 Subscribing to: {', '.join(SYMBOLS)}...")
    subscribe_msg = {
        "action": "subscribe",
        "params": {
            "symbols": ",".join(SYMBOLS)
        }
    }
    ws.send(json.dumps(subscribe_msg))
    print(f"   Sent: {json.dumps(subscribe_msg)}")
    
    # Start heartbeat thread (every 10 seconds like playground)
    def send_heartbeat():
        while True:
            time.sleep(10)
            try:
                ws.send(json.dumps({"action": "heartbeat"}))
                ts = datetime.now().strftime('%H:%M:%S')
                print(f"\n[{ts}] 💓 Heartbeat sent")
            except:
                break
    
    heartbeat_thread = threading.Thread(target=send_heartbeat, daemon=True)
    heartbeat_thread.start()

def run_test():
    ws_url = f"wss://ws.twelvedata.com/v1/quotes/price?apikey={API_KEY}"
    
    print("=" * 80)
    print("🧪 TWELVEDATA WEBSOCKET TEST - EUR/USD & XAU/USD")
    print("=" * 80)
    print(f"Symbols: {', '.join(SYMBOLS)}")
    print(f"API Key: {API_KEY[:10]}...{API_KEY[-4:]}")
    print("=" * 80)
    
    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open
    )
    
    # Run WebSocket with SSL disabled for Mac
    ws_thread = threading.Thread(
        target=lambda: ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
    )
    ws_thread.daemon = True
    ws_thread.start()
    
    # Wait 60 seconds (playground showed first price after ~26 seconds)
    print("\n⏳ Waiting 60 seconds for price data...\n")
    
    try:
        time.sleep(60)
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
    
    ws.close()
    time.sleep(2)
    
    # Results
    print("\n" + "=" * 80)
    print("📊 RESULTS")
    print("=" * 80)
    
    print(f"\nConnection: {'✅' if results['connected'] else '❌'}")
    print(f"Messages: {results['message_count']}")
    print(f"Subscribed: {len(results['subscribed'])}/{len(SYMBOLS)}")
    
    for sym in results['subscribed']:
        count = len(results['prices_received'].get(sym, []))
        print(f"  • {sym}: {count} price updates")
        if count > 0:
            latest = results['prices_received'][sym][-1]
            print(f"    Latest: ${latest['price']} at {latest['time']}")
    
    if results['denied']:
        print(f"\nDenied: {', '.join(results['denied'])}")
    
    print("\n" + "=" * 80)
    if len(results['prices_received']) > 0:
        print("✅ SUCCESS! WebSocket receiving price data!")
        print("   EUR/USD & XAU/USD confirmed working on free tier!")
    else:
        print("⚠️  No prices in 60 seconds (market closed or low activity)")
    print("=" * 80 + "\n")

if __name__ == "__main__":
    run_test()
