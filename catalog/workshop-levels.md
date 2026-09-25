# Workshop level ladders

`workshop-levels.v1.json` holds every levelled Workshop upgrade's full ladder:
`values[L]` is the value the Workshop row displays at level L (level 0 is the
base), and `next_coins[L]` is the coin cost of buying L to L+1 (null at max).
The source data is attributed to AngryBrit/tower-smith under CC BY-NC-SA 4.0,
pinned to the revision recorded in the file. Regenerate with
`python -m tools.build_workshop_levels`. The prices agree with every row of the
Fandom wiki's Workshop cost tables and with `workshop-prices.v1.json`.

`workshop_levels.py` infers an account's level by finding the rungs whose value
rounds to the displayed read. A coarse read ("12K") yields a level range; a
value off the ladder is reported as unmatched rather than snapped to a level.
These levels are inferences from stat reads, not levels read from the game.
