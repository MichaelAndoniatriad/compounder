"""User-defined portfolio mandate injected into agent prompts.

The portfolio runs two strategy sleeves:
  - core: long-term hold + growth (3-5yr). Governed by INVESTOR_POLICY_FULL.
  - catalyst: short-term, event-driven tactical trades. Governed by CATALYST_POLICY_FULL.

Edit the relevant block to change exit rules, framework, and checklist for the
agents that import it. Use ``policy_for_strategy()`` to select the right block.
"""

# Shorter block for early-stage analysts (market/news/sentiment) so prompts stay focused.
INVESTOR_POLICY_ANALYST_SUPPLEMENT = """
## Desk mandate (context for your report)

Downstream agents apply: **5+ year** growth horizon; staged entry (**half** intended size, add over **2–4 weeks**); **≤5%** of portfolio per new position; **average up, not down**; pre-earnings trim if **≥+15%** before print (sell half, hold half through); at **2×** entry sell half; **thesis break** (2–3 predefined metrics) → full exit within **48 hours** when any metric breaks on earnings or material news; **−30%** from entry with thesis intact → one review at **next scheduled earnings**; **−40%** from entry → **full exit**, no exceptions.

Call out catalysts (earnings, guidance), drawdown vs a plausible entry, red-flag patterns (deteriorating growth, cash flow vs revenue, heavy dilution), and anything that would fail the desk's pre-buy checklist.
""".strip()

# Short block for analysts when researching a catalyst-sleeve (short-term, event-driven) name.
CATALYST_ANALYST_SUPPLEMENT = """
## Desk mandate (context for your report — CATALYST trade)

This is a short-term, event-driven trade, not a long-term hold. The thesis is one specific dated catalyst
(earnings, FDA/PDUFA, launch, index inclusion, contract or legal decision, analyst day). Downstream agents
apply: single entry before the event; **-8%** hard stop; trailing stop once **+10%** (then **-8%** off peak);
**time stop** if the catalyst passes without the move; **30-day** max hold absent a catalyst date. No averaging,
no holding through the noise.

Focus your report on: what the catalyst is and its date, the setup into the event (positioning, expectations,
implied move), what would make the event a beat vs a miss, liquidity/tradability, and any near-term risk that
could break the trade before the catalyst. Long-term moat/valuation detail is secondary here.
""".strip()

INVESTOR_POLICY_FULL = """
## Exit Policy (Standardized — applies to all eToro positions)

Three triggers. Any one fires, act immediately.

**Trigger 1: Pre-earnings trim**
If a position is 15% or more above entry going into an earnings event, sell half before the print. Hold the other half through earnings. This rule is binding and applies regardless of conviction level.

**Trigger 2: Thesis break**
Every position must have 2 to 3 defined thesis-break metrics written in the portfolio Notes column. If any one of those metrics breaks on an earnings print or material news event, exit the full position within 48 hours. No deliberating. Before adding analysis to any position, confirm thesis-break metrics are written down first.

**Trigger 3: Double from entry**
When a position reaches 2x the entry price, sell half and let the remainder run. Capital is recovered. The rest is house money. No rule required to exit the remainder unless Trigger 1 or 2 fires.

**Drawdown floor (applies to all positions)**
If a position falls 30% from entry and the thesis is unchanged, one review window is allowed (next scheduled earnings). If a position falls 40% from entry for any reason, exit in full regardless of thesis. No exceptions. This rule exists because averaging down into losers is the most repeated mistake in this portfolio.

**Trigger 4: Crowded trade trim**
If a position is in the public LLM consensus top 10 AND has reached +30% from entry AND retail flow share of its 30 day ADV exceeds 25%, sell 25% of position regardless of conviction. Hold the remainder under existing rules. This rule exists because consensus names reverse faster than fundamentals and most of the asymmetric upside is captured by the +30% mark. Rule is binding, evaluated weekly. When the system mode is consensus_defensive, the +30% threshold drops to +15%.

After a Trigger 4 trim, the affected position is tagged with a 30 day sleeve rebalancing cool down. Sleeve allocation rebalancing skips this position during the cool down window. This prevents the rebalance from undoing the trim immediately.

---

# Stock Evaluation Framework — 10-Step Growth Process

## Step 1: Generate Ideas

Scan for companies encountered in daily life, industry trends, or emerging technologies. Use screeners (Finviz, TradingView, Yahoo Finance) filtered for: revenue growth >20% YoY, EPS growth >15% over 3 to 5 years, market cap >$1B, relative strength rating >80. Apply the Snap Test: if this company vanished overnight, would millions of people notice? If no, move on.

## Step 2: Confirm Market Leadership

Verify the company is top dog or clear first mover in its space. Confirm the industry has secular tailwinds (AI, electrification, cloud, ageing populations, fintech in emerging markets), not cyclical demand. Disqualify any company ranked third or fourth in a crowded market with no clear differentiation.

## Step 3: Evaluate the Competitive Moat

Identify which moat source applies: network effects, switching costs, intangible assets (patents, brands, licences), cost advantages, or efficient scale. Test pricing power: can the company raise prices 2 to 4% annually without losing customers? If not, moat is weak. Moat must be structural, not person-dependent.

## Step 4: Run the Numbers

Pull data from SEC filings, Yahoo Finance, or TIKR.

- Revenue: growing 20%+ YoY consistently over 3 to 5 years? Accelerating or decelerating?
- Earnings: EPS compounding 15%+ annually? Quarterly EPS up 25%+ vs same quarter last year?
- PEG ratio: below 1.0 ideal, below 1.5 acceptable, above 2.0 needs a compelling reason.
- Margins: gross margins above 50% (above 70% for SaaS)? Operating margins improving over time?
- ROIC: above 15%? Exceeding cost of capital? ROIC below WACC means growth destroys value.
- FCF: positive and growing? If negative, credible path to positive within 2 to 3 years?
- For SaaS/subscription: NRR above 110%, LTV:CAC above 3:1, Rule of 40 met.

## Step 5: Assess Management Quality

Research CEO and leadership. Is the company founder led? Does the CEO own meaningful personal stock? Does management communicate transparently in bad quarters? Is R&D above industry average? Check Glassdoor. Has there been unexplained C suite turnover (especially CFO)? Fisher's filter: any doubt on integrity, pass.

## Step 6: Size the Opportunity (TAM)

Top down: industry report total market narrowed to the actual served segment. Bottom up: price per customer multiplied by total potential customers worldwide. A company with 2% of a $500B TAM has massive runway. A company with 40% of a $10B TAM is approaching saturation. Bonus: does the company actively expand its TAM into adjacent markets?

## Step 7: Check Red Flags

Disqualify if: revenue growth decelerating 2+ consecutive quarters, cash flow declining while revenue rises, SBC exceeding 15% of revenue, share count increasing 5%+ annually, insider selling at unusual scale, NRR below 100% in subscription businesses, rising CAC quarter on quarter, accounting policy changes or auditor switches, loss of competitive position, or acquisitions outside core competency.

## Step 8: Value the Stock

Quick check: PEG below 1.5. Forward P/E vs own 5-year average and sector peers. Run a reverse DCF: what growth rate is the market implying for the next 10 years? If it requires 30%+ sustained growth and the company grows at 20%, the stock is priced for perfection. For pre-profit companies: EV/Revenue vs peers, and model earnings at industry average margins on current revenue.

## Step 9: Build the Position

Start with half the intended allocation. Add the other half over 2 to 4 weeks as the stock confirms the thesis. Never invest more than 5% of portfolio in a single new position. Average up, not down. Rising price after purchase confirms the thesis. Falling price means wait for clarity.

## Step 10: Hold and Monitor

Minimum holding period: 5 years. Review quarterly earnings but only act on fundamental changes, not price movements.

Hold through: market corrections, temporary earnings misses if thesis is intact, media panic.

Sell only when: thesis is broken (market share loss, structural revenue decline, management integrity failure), company is acquired for cash, significantly better opportunity exists and capital is needed, or concentration risk exceeds sleep number.

Never sell because: price dropped, price doubled, a TV pundit said to, to lock in gains, or the overall market is falling.

---

## Pre-Buy Checklist

Before buying any growth stock, confirm all of the following:

- Top dog or first mover in a growing industry
- At least one durable competitive moat source identified
- Revenue growth >20% YoY sustained over 3+ years
- Earnings growth >15% annually (or clear path to profitability)
- PEG ratio <1.5 (or reverse DCF shows reasonable implied growth)
- ROIC >15% and exceeding cost of capital
- Strong or improving margins
- Founder led or management with significant skin in the game
- TAM large enough to support 5 to 10x current revenue
- No red flags from the disqualifier scan
- You can explain the business and why it wins in two sentences

If all boxes cannot be checked, either do more research or move to the next idea.
""".strip()


CATALYST_POLICY_FULL = """
## Catalyst Sleeve Policy (short-term, event-driven — NOT a long-term hold)

This sleeve trades a specific, dated catalyst. The catalyst IS the thesis. There is no
5-year horizon, no averaging up, no "hold through the noise". Risk is managed tightly
because the only edge is correctly reading one event.

**Entry**
- Every catalyst trade must name the catalyst and its expected date (earnings, FDA/PDUFA,
  product launch, index inclusion, contract decision, legal ruling, analyst day).
- Enter as a single position before the event. Do NOT stage in over weeks.
- Size is smaller than a core position; multiple concurrent catalyst trades are expected.
- If you cannot state the catalyst, the date, and what move you expect, do not enter.

**Exit (any one fires, act immediately)**

Trigger 1: Hard stop loss
Exit if the position falls 8% from entry. This is tight on purpose — a catalyst trade
that is down before the event is usually wrong on timing or thesis.

Trigger 2: Trailing stop
Once the position is up 10% from entry, a trailing stop arms. Exit if it then falls 8%
from its peak. This lets a winning catalyst move run while locking in the gain. There is
no fixed profit target — the trailing stop captures the upside.

Trigger 3: Time stop
If the catalyst date passes and the expected move did not happen (position not up ~5%+),
exit within 3 days regardless of P/L. A catalyst that fired without moving the stock is a
dead thesis. If no catalyst date was set, close the trade after 30 days maximum.

**Crowded trade catalyst rule**
If a catalyst position is in the public LLM consensus top 20 AND the trailing stop has
not armed (still below +10%), tighten the hard stop from -8% to -5%. Crowded catalyst
trades fail faster.

**Hard rules**
- Never let a catalyst trade quietly become a long-term hold. If you want to keep it as a
  core position after the catalyst, that is a NEW decision with core entry rules and a new
  entry price — not a way to dodge the time stop.
- Never widen the stop to avoid being stopped out.
- The sleeve is locked at entry. A losing catalyst trade is not reclassified as core.
""".strip()


def policy_for_strategy(strategy: str) -> str:
    """Return the mandated policy text for a strategy sleeve ('core' or 'catalyst')."""
    return CATALYST_POLICY_FULL if str(strategy).strip().lower() == "catalyst" else INVESTOR_POLICY_FULL


def analyst_supplement_for_strategy(strategy: str) -> str:
    """Return the analyst-facing mandate supplement for a strategy sleeve."""
    return (
        CATALYST_ANALYST_SUPPLEMENT
        if str(strategy).strip().lower() == "catalyst"
        else INVESTOR_POLICY_ANALYST_SUPPLEMENT
    )
