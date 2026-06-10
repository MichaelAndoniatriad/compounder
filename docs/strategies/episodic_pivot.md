# Episodic Pivot Strategy

System reference for AI portfolio manager. Read fully before executing any catalyst trade. This document is the single source of truth for Episodic Pivot (EP) trades. If a rule here conflicts with general trading heuristics, this document wins.

## 1. Purpose and thesis

The Episodic Pivot captures the post catalyst drift in stocks that experience a fundamental change in their business outlook. The edge comes from two structural inefficiencies:

1. Post Earnings Announcement Drift (PEAD). Investors underreact to surprising news. Information takes days to weeks to fully price in.
2. Institutional accumulation. Large funds cannot buy their full position in one day. They build over multiple sessions, providing sustained demand.

The strategy is a swing trade lasting from 2 days to 8 weeks. It is not a day trade. It is not a long term hold. It does not predict catalysts. It reacts to confirmed ones.

Expected outcome with disciplined execution: win rate 35 to 50 percent, average winner 3 to 5 times average loser, annualised return 15 to 40 percent at moderate position sizing. Real Sharpe 0.8 to 1.5. Do not expect 100 percent winners or 10x trades on every entry.

## 2. Universe

Acceptable instruments:
- US listed common stock on NYSE, Nasdaq, Amex
- Market cap between 100 million USD and 50 billion USD
- Average daily dollar volume above 5 million USD over prior 20 sessions
- Price above 5 USD per share

Excluded:
- OTC, pink sheets, sub 5 USD stocks (manipulation risk)
- SPACs pre merger
- Chinese reverse merger stocks (CCM) unless dual listed and audited
- Stocks under SEC investigation or with going concern warnings
- ETFs, ADRs of micro caps, leveraged products

## 3. Qualifying catalysts

A catalyst qualifies for an Episodic Pivot only if it changes the fundamental outlook of the business. Noise does not qualify.

Tier 1 catalysts (highest priority, take first):
- Earnings beat of 10 percent or more on EPS AND revenue, plus raised forward guidance
- Guidance raise outside of earnings (mid quarter or pre announcement)
- FDA approval or positive Phase 3 readout for a material drug
- Large customer contract win (named, material, disclosed dollar figure)
- Strategic pivot validated by named anchor partner (e.g. Microsoft, Apple, major retailer)
- Activist investor stake of 5 percent or more with named campaign
- Buyout offer at premium (long acquirer or target depending on spread)

Tier 2 catalysts (acceptable but require stronger confirmation):
- Earnings beat without guidance raise
- Analyst upgrade by top tier firm (GS, MS, JPM) with double digit price target increase
- Insider cluster buy: 3 or more insiders buying open market within 30 days
- Sector tailwind shift confirmed by multiple stocks gapping on same theme

Disqualified events (do not trade as EP):
- Stock buyback announcements
- Dividend increases
- Share split announcements
- Rumours, unconfirmed reports, anonymous sources
- Short squeeze without underlying business change
- Crypto or blockchain pivot announcements (low signal, high reversal rate)
- Reverse stock splits
- Earnings that beat by single digits with no guidance change

## 4. Entry criteria

All conditions must be met. No exceptions.

### 4.1 Gap condition
- Stock gaps up 10 percent or more at the open on the catalyst day
- Gap measured as: (today open / yesterday close) minus 1
- For mid day catalysts (intraday news), use the price spike of 10 percent or more from pre catalyst price

### 4.2 Volume condition
- First 60 minutes of trading must exceed 100 percent of the stock's average full day volume over prior 20 sessions
- This confirms institutional participation, not retail noise

### 4.3 Price action condition
- Stock must hold above the gap up level for at least 30 minutes after open
- Stock must not fill more than 50 percent of the gap during the first 60 minutes
- A stock that gaps up 15 percent then trades back to 7 percent above prior close is a failed pivot. Skip.

### 4.4 Trend context
- 50 day moving average should be flat or rising. Avoid stocks in established downtrends unless the gap is more than 20 percent (game changer override).
- No earnings or major catalyst expected in next 10 sessions (avoid re entering a new event window)

### 4.5 Entry trigger
Primary entry: Opening Range Breakout (ORB)
- Define opening range as the high and low of the first 5 minutes of trading
- Enter when price breaks above the 5 minute opening range high on volume
- Confirmation: the breakout candle must close above the opening range high

Alternative entry: First pullback
- If ORB missed or extended too far, wait for first pullback to VWAP or 9 EMA on hourly chart
- Enter on reversal candle (hammer, bullish engulfing) on increased volume
- Only valid within first 5 sessions after catalyst

No entry after session 5 post catalyst. The edge decays sharply.

## 5. Position sizing

Risk based sizing only. Never size by dollar amount or share count.

### 5.1 Risk per trade
- Maximum 1 percent of total portfolio equity at risk per trade
- Risk equals entry price minus stop price, multiplied by share count
- Example: 100,000 portfolio, 1 percent risk equals 1,000 USD risk. Entry 50, stop 47, risk per share 3. Share count: 1000 / 3 equals 333 shares. Position value: 16,650 USD.

### 5.2 Concentration limits
- Maximum 5 concurrent EP positions
- Maximum 20 percent of portfolio in EP sleeve at any time
- Maximum 1 position per sector at a time (avoid correlated bets)

### 5.3 Pyramiding (adding to winners)
- Only add to a position after the first add-back is profitable
- First add allowed at +5R (5 times initial risk)
- Add size: 50 percent of original position
- Move stop on original position to breakeven before adding
- Maximum 2 adds per position

## 6. Stop loss rules

Hard stop required on every position. No mental stops.

### 6.1 Initial stop placement
Primary: Low of the breakout candle (5 minute or hourly depending on entry)
Alternative: Low of the catalyst day, whichever is tighter while still wider than 2 percent

Minimum stop distance: 2 percent below entry (avoid noise stops)
Maximum stop distance: 8 percent below entry (if wider, position size too small to be worth taking)

### 6.2 Stop movement
- After +2R profit: move stop to entry price (breakeven)
- After +4R profit: move stop to +1R (lock in 1R profit)
- After +6R profit: trail stop using prior session low or 10 day moving average, whichever is closer
- Never lower a stop. Only raise it.

### 6.3 Time stop
- If position is flat (between minus 0.5R and plus 0.5R) after 10 sessions, exit at market on session 11 open
- Money sitting in dead trades is opportunity cost

## 7. Exit rules

### 7.1 Stop hit
Exit immediately at market when stop level is touched. No averaging down. No widening stops.

### 7.2 Trend break exit
Exit when:
- Stock closes below 10 day moving average on volume
- Stock closes below the 20 day moving average for 2 consecutive sessions
- Stock breaks the trend line drawn from catalyst day low to most recent swing low

### 7.3 Profit target exit (partial)
- Scale out 33 percent at +4R
- Scale out 33 percent at +8R
- Trail final 33 percent with the rules in section 6.2

### 7.4 Catalyst invalidation
Exit immediately at market if:
- Company issues clarifying statement that walks back the original catalyst
- News emerges that contradicts the catalyst (FDA reverses, contract cancelled, fraud accusations)
- SEC, DOJ, or regulator opens an investigation

### 7.5 Next earnings approach
If the position is still open within 3 sessions of next earnings report:
- If unrealised gain is less than +4R: exit before earnings (do not hold binary risk)
- If unrealised gain is more than +4R: optional hold for earnings if conviction is high. Default exit two thirds, hold one third.

## 8. Daily workflow

### 8.1 Pre market (every trading day)
1. Scan for stocks gapping up 10 percent or more in pre market
2. Identify the catalyst for each gapper from news feed
3. Classify catalyst as Tier 1, Tier 2, or Disqualified per section 3
4. Verify universe criteria from section 2
5. Build watchlist of qualified candidates (typically 0 to 5 names per day)

### 8.2 At open
1. Mark the 5 minute opening range high and low for each watchlist name
2. Set alerts at opening range high
3. Confirm volume condition is on track (first hour pacing above 100 percent of 20 day average)

### 8.3 During session
1. On ORB trigger plus volume confirmation, enter at market
2. Set hard stop immediately after fill
3. Log entry in trade journal (see section 10)
4. Do not chase if price moves more than 3 percent above breakout level without entry. Wait for pullback or skip.

### 8.4 End of session
1. Review all open EP positions against section 6 stop movement rules
2. Adjust stops upward only
3. Note any catalyst follow up news that affects open positions

### 8.5 Weekend
1. Review all closed trades from the week
2. Tag winners and losers by catalyst type, entry quality, exit reason
3. Look for pattern: which catalyst types worked, which timing worked, which sectors worked
4. Adjust forward bias only after 50 trades minimum

## 9. Disqualifiers and skip conditions

Do not take the trade even if all entry criteria are met if:
- Overall market (SPY) is down more than 2 percent on the session
- VIX is above 35 (regime too volatile for swing setups)
- Stock has been pumped on social media in prior 5 sessions without a real catalyst
- Float is below 5 million shares (manipulation risk, ignore section 2 minimum)
- Short interest is above 30 percent of float (squeeze dynamic, not EP dynamic)
- Stock is up more than 50 percent in the prior 10 sessions (extended, mean reversion risk)
- Catalyst is a re statement, restatement of prior numbers, or accounting correction

## 10. Trade journal (mandatory)

Log every trade in a structured format. Without logs there is no improvement.

Required fields per trade:
- Ticker
- Catalyst type and tier
- Catalyst description (one line)
- Entry date and time
- Entry price
- Stop price
- Position size in shares and USD
- Risk in USD
- Exit date and time
- Exit price
- P and L in USD and R multiple
- Exit reason (stop, trend break, profit target, time stop, catalyst invalidation, earnings approach)
- Post trade note: what went right or wrong, what to adjust

Review journal every 25 trades minimum. Compute by catalyst tier:
- Win rate
- Average winner in R
- Average loser in R
- Expectancy: (win rate times avg win) minus (loss rate times avg loss)
- Largest drawdown
- Time in trade

If expectancy is negative after 50 trades, stop trading and review system. Do not double down.

## 11. Risk management at portfolio level

### 11.1 Drawdown halts
- If EP sleeve loses 5 percent from peak: reduce position size by 50 percent for next 10 trades
- If EP sleeve loses 10 percent from peak: halt all new trades for 5 sessions, review journal for systematic errors
- If EP sleeve loses 15 percent from peak: full stop. Do not resume without strategy review.

### 11.2 Correlation check
- Before entering a new position, verify no existing position in same sector or theme
- If two EP candidates qualify in the same theme on the same day, take the stronger one only (higher gap, more volume, Tier 1 over Tier 2)

### 11.3 Macro overrides
- During FOMC week: do not enter new EP positions in the 24 hours before the announcement
- During major geopolitical event (war declaration, central bank emergency action): halt new entries until 2 sessions of stable price action

## 12. What this strategy is not

Common misuse cases. Avoid these.

This is not a day trade strategy. Holding period is days to weeks, not hours.

This is not a buy the dip strategy. You buy strength, not weakness.

This is not a prediction strategy. You do not guess earnings, you react to confirmed beats.

This is not a value strategy. Fundamentals matter only to confirm the catalyst is real. Valuation does not gate entry.

This is not a meme stock strategy. Float, retail attention, and short interest are disqualifiers when extreme.

This is not a hedge. Positions are long only by default. Pair trades and shorts are out of scope for this document.

## 13. Edge case decision tree

If multiple Tier 1 catalysts qualify on the same day:
1. Take the highest gap percentage first
2. If tied, take the highest dollar volume
3. If still tied, take the smaller market cap (more drift potential)

If catalyst day price action is choppy (no clean ORB):
1. Wait for first pullback entry per section 4.5
2. If no pullback by session 3, skip

If stop is hit but stock reverses same day:
1. Do not re enter. The thesis was wrong on this entry. New setup required (new catalyst or new gap day).

If position gaps down through stop overnight:
1. Exit at market open. Do not wait for bounce. Accept the slip.
2. Log as a gap loss. These are normal. Account for them in expectancy.

If a Tier 1 catalyst occurs but the stock fails to gap (already priced in):
1. Skip. The drift edge requires a fresh gap. No gap, no setup.

## 14. Intraday clock (ET)

All times in US Eastern. The AI manager should adapt if regular session hours shift (half day, holiday close). Use judgment within the spirit of each window.

### 14.1 Pre market scan (04:00 to 09:25)
- 04:00 to 06:00: initial scan for overnight news, earnings released before open, foreign market reactions
- 06:00 to 08:00: second pass once pre market liquidity builds. Confirm gap levels are holding on real volume not thin prints.
- 08:00 to 09:25: finalise watchlist. Classify catalysts. Set alerts at expected ORB levels.
- Do not commit to entries based on pre market alone. Pre market liquidity is too thin for reliable price discovery.

### 14.2 Opening range (09:30 to 09:35)
- First 5 minutes: do not enter. Observe the range only. Mark high and low.
- High frequency reversals are common in this window. Patience is the edge.

### 14.3 Primary entry window (09:35 to 10:30)
- ORB breakouts most reliable here
- Volume confirmation strongest in this hour (often 40 to 60 percent of full day volume prints in first hour)
- If no breakout by 10:30, move to first pullback playbook

### 14.4 Secondary entry window (10:30 to 11:30)
- First pullback entries acceptable
- Watch for VWAP holds and 9 EMA bounces on the 5 minute chart
- Volume should still be elevated vs prior session pace

### 14.5 Lunch lull (11:30 to 13:30)
- Do not initiate new EP positions
- Volume drops, false breakouts increase
- Manage existing positions only. Move stops per section 6.2 if levels are hit.

### 14.6 Afternoon (13:30 to 15:00)
- New entries acceptable only if a fresh catalyst hits intraday (mid day news, analyst upgrade)
- For existing positions: this is the trend continuation window. Hold winners.

### 14.7 Power hour (15:00 to 16:00)
- Institutional positioning window
- Strong close above the day's mid range is a positive signal for holding overnight
- Close below VWAP on volume is a warning. Consider tightening stops.
- Do not initiate new positions in the final 15 minutes (15:45 to 16:00)

### 14.8 After hours (16:00 to 20:00)
- Monitor for follow up catalysts (earnings released after close, contract announcements, FDA letters)
- Do not adjust stops based on after hours prints. Liquidity is unreliable.
- If catalyst invalidation occurs after hours (section 7.4): exit on the next session open at market, do not attempt after hours exit unless the stock is highly liquid in extended hours.

### 14.9 Overnight
- All EP positions are held with regular session stops. After hours stops are not honoured by most brokers reliably.
- If overnight news materially changes thesis, decision is made at next session open per section 7.4

## 15. Multi session decision checkpoints

The catalyst day is session 1. Session count uses regular trading sessions (skip weekends and holidays). Each checkpoint defines what the AI manager must verify and decide.

### 15.1 Session 1 (catalyst day)
- Decision: enter or skip
- Verify: all entry criteria in section 4
- If entered: set hard stop within 60 seconds of fill, log trade

### 15.2 Session 2 (day after catalyst)
- Decision: hold or exit early
- Verify: stock holds above session 1 low. If breached intraday on volume, exit at market.
- Verify: no overnight news invalidates thesis (section 7.4)
- Acceptable: consolidation between session 1 high and session 1 mid range
- Warning: gap down opening, especially below session 1 VWAP

### 15.3 Session 3
- Decision: first add eligibility check
- Verify: position is at +2R or better, stop has been raised to breakeven per section 6.2
- If position is flat or losing: hold, do not add, do not exit unless stop hit
- Volume should still be elevated vs pre catalyst baseline (at least 1.5x 20 day average)

### 15.4 Session 5
- Decision: close of new entry window
- No new EP entries on this stock after session 5 close. Drift edge degrades.
- For existing positions: confirm position is at +1R or better. If flat after 5 sessions, the setup has likely failed. Tighten stop to entry level.

### 15.5 Session 10
- Decision: time stop check
- If position is between minus 0.5R and plus 0.5R: exit at session 11 open per section 6.3
- If position is profitable: continue managing per section 6.2 and 7

### 15.6 Session 20
- Decision: transition to longer trail
- Switch trailing stop reference from prior session low to 20 day moving average
- Most of the PEAD drift edge has been captured by this point. Remaining hold is trend continuation, not catalyst drift.

### 15.7 Session 40
- Decision: thesis reassessment
- The original catalyst is now stale. Continued hold is based on price action only, not the original thesis.
- Tighten stop to 10 day moving average
- Acceptable to fully exit even without stop hit if higher quality EP setups are available elsewhere

### 15.8 Session 60 and beyond
- Outside the documented PEAD drift window
- Position should already be closed or trailed to a tight stop
- If still open: treat as a momentum trend trade, not an EP trade. Different rule set applies (out of scope for this document).

### 15.9 Weekend rules
- Default: hold positions through the weekend with regular stops
- Exception: exit before weekend close if any of the following
  - Position is at +6R or better and weekend has elevated risk (FOMC Monday, geopolitical event pending, earnings season open Monday in a related name)
  - Position has shown 2 consecutive sessions of declining volume and flat price action
  - VIX has spiked above 30 during the week and closed above 28 on Friday

## 16. Version control

This document version: 1.1
Last updated: 2026-06-03
Strategy authority: Pradeep Bonde (original Episodic Pivot framework), refined by Kristjan Kullamägi for swing trading application
Academic basis: Post Earnings Announcement Drift literature (Bernard and Thomas 1989 onwards), insider cluster buying research (Cohen Malloy Pomorski 2012)

Changes to this document must be logged at the bottom with date, change, and reason. Strategy parameters should only be changed after a minimum 50 trade sample contradicts the current rule.

Change log:
- v1.0 2026-06-03: Initial document
- v1.1 2026-06-03: Added Section 14 (Intraday clock ET) and Section 15 (Multi session decision checkpoints). Renumbered version control to Section 16.
