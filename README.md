# Compounder

An autonomous LLM portfolio manager that paper-trades an Alpaca account. The system runs two concurrent sleeves: a core compounders sleeve holding quality growth names for multi-week holds, and an event-driven catalyst sleeve that enters on earnings surprises and exits within days. Deterministic risk rails (position limits, drawdown stops, concentration caps) constrain every LLM decision before it reaches the executor. A measurement loop scores each recommendation once outcome data is available and feeds learned rules back into the next PM cycle.

## What it is

Compounder orchestrates a multi-agent LLM graph (analysts → researchers → trader → risk team → portfolio manager) to produce actionable trade decisions, then routes those decisions through paper execution on Alpaca. The system is designed to run unattended on a VPS with all state persisted to `~/.tradingagents/`.

## Architecture

Scheduled LaunchAgents (or server cron) fire weekly and event-driven scans. Scanners surface candidate tickers into a priority queue. PM cycles pull from the queue, run the full LLM graph, and emit trade proposals. A paper executor sends approved orders to Alpaca. A watchdog monitors positions and fires alerts via Telegram. See `docs/` for detailed runbooks.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # then fill in values
```

Required environment variables:

- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` — Alpaca paper trading keys
- `ALPHA_VANTAGE_API_KEY` — news, fundamentals, and price data
- `TRADINGAGENTS_ANALYSIS_TELEGRAM_BOT_TOKEN` / `TRADINGAGENTS_ANALYSIS_TELEGRAM_CHAT_ID` — Telegram alert delivery
- At least one LLM provider key: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, `OPENROUTER_API_KEY`, or a hosted Ollama endpoint via `OLLAMA_BASE_URL`

See `.env.example` for the full list of optional overrides.

## Running tests

```bash
cd ~/workspace/trading-agents
set -a && source .env && set +a
.venv/bin/python -m pytest tests/ -q
```

## License

Apache-2.0; originally derived from the TradingAgents open-source framework.
