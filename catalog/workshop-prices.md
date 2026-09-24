# Early Workshop price estimates

`workshop-prices.v1.json` contains early coin prices, with a source URL per
upgrade and a pinned TowerSmith revision. The source data is attributed to
AngryBrit/tower-smith under CC BY-NC-SA 4.0. This compilation selects exact
integer prices; rounded K/M entries and battle cash prices are excluded.
Index L is the cost of buying level L to L+1.

The reroll planner saves every readable Workshop row in an account-bound
`workshop-prices.json` file under the worker runtime directory. A unique
catalog-price match provides an **inferred** level. It is not a verified account
level: tutorial purchases and discounts can change the starting point. A
nonmatching observed price remains useful for affordability but cannot project
subsequent prices. Known discount changes and uncertain purchase outcomes
invalidate affected observations. Catalog revision changes discard the cache.

Only confirmed purchases occurring after a price observation advance its
estimate. Wallet estimates combine an observed balance with later completed
runs and ledger changes, using event timestamps to avoid counting delayed
database writes twice. Unknown spending or run rewards require reconciliation.
The buyer always checks the live row price and wallet before tapping.

Fresh rerolls take at most one cheap Workshop level per starter row (Damage,
Attack Speed, Health, Defense unlock, Defense Absolute), each capped at 75 coins.
Expensive existing levels are skipped. The strategy then retains its 350-coin
utility target and 400-coin ceiling. In battle, small survival targets precede
economy; unaffordable starters allow affordable alternatives. These are bounded
opening priorities, not a guarantee of reaching a particular wave.
