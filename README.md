# bybit_btc_short_basket_bot

Short-basket bot on Bybit perpetuals: run a broad basket of BTC-correlated alts short against a BTC hedge, harvesting relative-weakness drift and funding.

## Concept

- Universe: liquid Bybit USDT perps with high beta to BTC.
- Signal: rank by trailing relative strength; short bottom quantile, hedge with proportional long BTC.
- Rebalance: on a fixed schedule; positions reduce-only outside of rebalance windows.
- Risk: max concurrent notional cap, MM% ceiling, per-name stop-loss.

## Layout

- `bot.py` — main loop.
- `basket.py` — universe selection + ranking.
- `hedge.py` — BTC leg sizing.
- `risk.py` — caps and stop logic.

## Run

```bash
pip install -r requirements.txt
python bot.py
```

## Status

Prototype. Not currently in production rotation.