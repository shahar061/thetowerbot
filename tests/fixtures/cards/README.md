# Cards reader evidence

The immutable full captures remain at `tests/fixtures/menu_cards.png`,
`tests/fixtures/menu_cards_stocked.png`, and `tests/fixtures/menu_cards_compact_*.png`;
recorded OCR has the corresponding basename in `tests/fixtures/ocr/`.
`tests/card_fixtures.py` loads them from that existing root. They are not synthetic
and are not copied into this directory, avoiding a second divergent corpus.

All captures are 1080 × 2400, English. The compact profile now supports bounded x1/assignment controls as described below;
older profiles remain observation-only. Captures themselves do not authorize input.

| Layout | Measured evidence | Supported observation |
| --- | --- | --- |
| `cards.empty.1080x2400.en` | CARDS `(30,246,174,45)`, ACTIVE `(436,504,208,52)`, count `(502,555,77,45)` | Capacity 1, exact empty equipped set; anonymous inventory locks do not identify unowned cards |
| `cards.stocked.1080x2400.en` | CARDS `(30,111,174,45)`, ACTIVE `(438,493,205,45)`, count `(480,540,120,41)` | Capacity 14, partial populated inventory, incomplete equipment |
| `cards.compact.1080x2400.en` | CARDS `(30,111,174,45)`, ACTIVE `(436,371,208,48)`, count `0/1` `(503,420,75,42)` or `1/1` `(506,420,68,42)` | Capacity 1, exact empty equipment at 0/1, complete one-card ACTIVE identity at 1/1, Damage ownership when its complete tile is visible |

Compact captures come from the same account in game 29.0.3 on BlueStacks Air,
using full-resolution ADB PNG screenshots. `compact_manifest.json` records
source filenames, capture timestamps, and exact PNG/OCR SHA-256 hashes. Images
are unedited copies; OCR was recorded using `tools/record_ocr.py`.
The five states are empty inventory, claimed Damage, Damage equipped, empty
equipment restored, and a claimed duplicate Damage. The manually authorized
capture session used two x1 draws (40 gems total, 304 → 264) and restored empty
equipment. It did not exercise the dashboard or Cards runtime.

The compact INVENTORY heading is `(376,844,327,45)`, with tile origins x34,291,
548,805 and y933,1284,1635. Tile dimensions and pitch match the stocked grid.
Damage's label is `(94,947,121,34)`, and visible counters `1/3` or `2/3` are near
`(133,1241,42,27)`. Only ownership is read from these tiles: non-max stars and
counters remain unknown. The visible compact check does not match the existing
stocked template at its acceptance threshold. Complete equipment identity instead
requires the full one-card ACTIVE strip, its named tile and count1/1. Anonymous lock tiles do not identify unowned cards.

Discovery requires one complete title/ACTIVE/count profile. Mixing anchors
from different profiles is unsupported; compact inventory labels are read only
under that profile's measured INVENTORY heading.

The stocked INVENTORY heading is `(374,965,328,43)`. Complete tile origins
are x34,291,548,805 and y1054,1405,1756: column pitch257 and row pitch351.
The measured tile width is about240 and height299. Label recognition uses the
whole label band, joins split `Enemy` + `Balance` boxes in x order, and refuses
uncertain or duplicate identities. A bright top border must accompany the OCR.
The fourth row starts around2107 and is clipped by navigation near2230; its
labels are deliberately not read. The active strip also shows only four of
fourteen equipped identities, so it does not establish a complete set.

Twelve complete tiles identify Damage, Attack Speed, Health, Health Regen,
Range, Cash, Coins, Slow Aura, Critical Chance, Enemy Balance, Extra Defense,
and Fortress. A single trusted `Max` label below its own tile maps to the
separately sourced normal maximum in `card_catalog`. Magenta/white star symbols
are not numeric-level calibration. Non-max copy counters are unsupported.

Runtime templates are exact unedited BGR crops from `menu_cards_stocked.png`:

- `templates/cards/equipped_check.png`: x231,y1326,width60,height56.
- `templates/cards/battle_lock.png`: x301,y1814,width49,height61.

Template correlation ≥.95 in a tile-relative region supports positive presence
only. Eight recorded inventory checks are detected. The Enemy Balance lock is
observed alongside its check: ownership is `owned`, `equipped=True`, and
`battle_locked=True`. An absent or changed marker produces `None`, never false.
The item confidence is the conservative minimum of accepted OCR confidence and
positive template match scores; it is a recognition score, not a probability.
Each known field gets provenance including frame digest, layout and timestamp.

The task-local capture corpus now also contains authentic x1 new/duplicate
reward sequences, scroll attempts, and one-card equip/unequip before/after
states. The settled COMMON reward receipts and one-card ACTIVE strip now calibrate
x1 receipt/claim and assignment reconciliation. `action_manifest.json` links
the exact new/duplicate reward, animation, home and detail-overlay fixtures to
their source captures and SHA-256 hashes. `tests/test_cards_recorded_runtime.py`
uses these full frames and OCR through the production adapter, HTTP, runtime,
journal and ledger with a recording device transport. It is an offline replay,
not a live dashboard/runtime canary. x10, slot purchase/result, level-up and
different-card replacement remain unrecorded. Scroll completeness and non-max
levels/copies remain unsupported. Pure merge tests use explicit model observations to verify overlap,
level-up copy reset, historical field preservation and scope boundaries. OCR and
pixel mutations only verify refusal/unknown behavior. Repeated or duplicate
synthetic reward labels always return unsupported (`None`), never empty rewards.
Collection completeness is never inferred from these fixtures; the empty ACTIVE
band and calibrated one-card compact ACTIVE strip support complete equipment.

Historical projections may retain same-account card fields and equipped tuples
across visits and scope changes. Their original field/equipment evidence remains
attached; only matching per-field scope/visit can be current. Collection coverage
never refreshes missing fields, and retained equipment sets are incomplete until
newly observed. Equal-time conflicts preserve prior proven values and provenance.
