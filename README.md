# 🤖 Nifty Options Paper Trading Bot

**Dhan API | EMA + BB + RSI + ADX + VWAP Strategy | Railway/Render Ready**

---

## 📁 Files Overview

| File | Purpose |
|------|---------|
| `bot.py` | Main trading bot — runs 24/7 |
| `report.py` | Run anytime to see performance |
| `requirements.txt` | Python packages needed |
| `Procfile` | Tells Railway/Render how to start the bot |
| `.env.example` | Template for your API credentials |
| `.gitignore` | Protects secrets from being uploaded |

---

## ⚙️ Strategy Parameters (from Backtest v6)

| Parameter | Value |
|-----------|-------|
| Capital | ₹1,00,000 |
| Units | 5 × ₹20,000 |
| Candle | 15-min |
| Entry Window | 9:30 AM – 2:30 PM IST |
| Hard Close | 3:10 PM IST |
| EMA | 9 / 21 / 50 |
| Bollinger Bands | 20 period, 2 std |
| RSI | 14 period |
| ADX Min | 20 |
| Stop Loss | -40% of premium |
| Take Profit | +100% of premium |
| Trailing | After +60%, lock 75% of peak |
| Max Lots | 2 per trade |

---

## 🚀 DEPLOY TO RAILWAY (Recommended — Free)

### Step 1: Push to GitHub
```bash
git init
git add .
git commit -m "Initial bot"
git remote add origin https://github.com/YOUR_USERNAME/nifty-bot.git
git push -u origin main
```

### Step 2: Deploy on Railway
1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **New Project → Deploy from GitHub Repo**
3. Select your repo
4. Go to **Variables** tab and add:
   ```
   DHAN_CLIENT_ID = your_client_id
   DHAN_ACCESS_TOKEN = your_token
   ```
5. Railway auto-detects Python and uses the `Procfile`
6. Bot starts automatically ✅

### Step 3: Add Persistent Storage (CRITICAL)
Railway resets the filesystem on redeploy. To keep trade history:
1. In Railway dashboard → **Add Plugin → Volume**
2. Mount it at `/app` (where trading.db and bot_state.json will live)
3. This ensures your data persists across restarts

---

## 🚀 DEPLOY TO RENDER (Alternative)

1. Go to [render.com](https://render.com) → New → **Background Worker**
2. Connect your GitHub repo
3. Set:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
4. Add environment variables in the **Environment** tab
5. Add a **Disk** (under Advanced) mounted at `/opt/render/project/src` to persist data

---

## 📊 How to Check Performance

Since the bot runs on a server, view performance two ways:

### Option 1: Download DB and run report locally
```bash
# Download trading.db from Railway/Render dashboard
python report.py
```

### Option 2: View logs in Railway/Render dashboard
- Every trade entry/exit is logged with P&L
- Check the **Logs** tab in the dashboard

---

## 💾 What Gets Saved (Persistent)

| File | Contains |
|------|---------|
| `trading.db` | All trades, signals, daily summaries |
| `bot_state.json` | Current capital per unit, open positions |
| `logs/bot.log` | Full activity log |

The bot **never resets capital** — it always loads from the last saved state.

---

## 🔄 If Bot Crashes / Restarts

The bot automatically:
1. Loads capital from `bot_state.json`
2. Loads any open positions
3. Continues from where it left off

**No data is lost on restart.**

---

## 📈 Performance Report Columns

| Column | Meaning |
|--------|---------|
| Win Rate | % of profitable trades |
| Profit Factor | Total wins ÷ Total losses (>1 is good) |
| Max Drawdown | Worst equity dip % |
| Avg Win / Avg Loss | Size of wins vs losses |

---

## ⚠️ Important Notes

1. **This is PAPER trading** — no real orders are placed
2. Access Token expires — update `DHAN_ACCESS_TOKEN` in Railway variables when it does
3. Download `api-scrip-master.csv` from Dhan and upload to your repo for accurate contract selection
4. Bot sleeps automatically on weekends and after market hours

---

## 🛠️ Troubleshooting

| Problem | Fix |
|---------|-----|
| "Not enough candle data" | API token may have expired — refresh it |
| Bot shows 0 trades | Check logs — ADX may be below 20, or all units in cooldown |
| Capital same every restart | Make sure `bot_state.json` is on persistent storage |
| Railway worker keeps restarting | Check Logs tab for error, likely a missing package |
