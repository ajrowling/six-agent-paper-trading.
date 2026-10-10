# Six-Agent Paper Trading Team v2

## What this version does
- Responsive Streamlit web dashboard for phone/computer.
- Automatic crypto-only paper scanner for Binance-listed USDT pairs.
- Background loop while the app process is running.
- One open position maximum.
- Maximum 1% paper balance risk per position.
- Required stop and target; no leverage.
- Simulated stop/target monitoring and manual paper close.
- SQLite audit log plus CSV export.
- Emergency pause control.

## Very important: XAUUSD
This build does **not** automatically trade or scan gold yet. Crypto market data from Binance cannot be used as a gold price. Gold is deliberately excluded until you select a real market-data source (for example, an MT5 demo terminal adapter or a quote API). No gold price is fabricated.

## Run on your computer
Requires Python 3.10 or newer.

```bash
python -m venv .venv
```

Windows:
```powershell
.venv\Scripts\activate
```

macOS/Linux:
```bash
source .venv/bin/activate
```

Then:
```bash
pip install -r requirements.txt
streamlit run app.py
```

Open the local URL printed by Streamlit. On a phone, use the same Wi-Fi and open the computer's local IP with port 8501, if your firewall permits it.

## Web deployment
1. Create a GitHub repository and upload `app.py`, `requirements.txt`, and `README.md`.
2. Deploy the repository on Streamlit Community Cloud using `app.py` as the entry point.
3. Free hosting can sleep or restart. The automatic loop only runs while the app process is alive. For reliable continuous scans, use an always-on host or VPS; this may cost money.
4. Local SQLite persistence on free hosts may not survive restarts/redeployments. Download CSV backups frequently or add a persistent database.

## Configure
- Starting balance: default $1,000 paper.
- Risk per trade: default 0.5%, hard cap 1%.
- Scan interval: default 60 seconds, minimum 15 seconds.
- Default symbols: BTCUSDT, ETHUSDT, DOGEUSDT, SOLUSDT.
- Automation is off until you explicitly enable it and save settings.

## Safety design
- No broker APIs, exchange API keys, wallet support, or real order endpoints.
- Does not import ccxt or use any trading execution library.
- Paper trade calculations are approximate and not a guarantee of real-world results.

## Known limitations
- Crypto-only automatic execution in this version; XAUUSD requires a separate data adapter.
- Rules are deterministic heuristics, not six independent LLMs.
- Polling may miss fast moves; fills assume stop/target price and don't model gaps.
- Estimated round-trip fee is set to 0.1% of entry+exit notional; this may differ from actual venues.
- No strategy has been shown to be profitable. Test 50–100+ paper trades before drawing conclusions.
