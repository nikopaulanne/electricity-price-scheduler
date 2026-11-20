"""Constants shared by the Home Assistant integration and its pure core."""

DOMAIN = "electricity_price_scheduler"

# The optimizer is deliberately bounded for low-powered Home Assistant hosts.
DEFAULT_MAX_PERIODS = 6
MIN_MAX_PERIODS = 1
MAX_UI_MAX_PERIODS = 6
MAX_OPTIMIZER_PERIODS = 24

DEFAULT_MIN_CUTOFF_MINUTES = 30
DEFAULT_MAX_CUTOFF_MINUTES = 300
MAX_CUTOFF_MINUTES = 360
CUTOFF_STEP_MINUTES = 15
SLOT_MINUTES = 15
MAX_SLOTS = 200

MAX_CUTOFF_ENTITY_UNIT_HOURS = "h"
MAX_CUTOFF_ENTITY_UNIT_MINUTES = "min"
MIN_PRICE_DIFF_ENTITY_UNIT_CURRENCY_PER_KWH = "currency_per_kwh"
MIN_PRICE_DIFF_ENTITY_UNIT_SUBUNIT_PER_KWH = "subunit_per_kwh"

WARNING_MAX_CUTOFF_HELPER_UNAVAILABLE = "max_cutoff_helper_unavailable"
WARNING_MAX_CUTOFF_HELPER_INVALID = "max_cutoff_helper_invalid"
WARNING_MIN_PRICE_DIFF_HELPER_UNAVAILABLE = "min_price_diff_helper_unavailable"
WARNING_MIN_PRICE_DIFF_HELPER_INVALID = "min_price_diff_helper_invalid"

ACTIVE_WINDOW_STORE_VERSION = 1

DEFAULT_CURRENCY = "EUR"
DEFAULT_MIN_PRICE_DIFF_PER_KWH = 0.03
DEFAULT_PREHEAT_FACTOR = 1.5
DEFAULT_RECOVERY_FACTOR = 1.2
DEFAULT_MULTIPLIER_MODE = "fixed"

# Source units accepted by core.normalize_slots. Values are converted to the
# selected currency per kWh; no currency conversion is performed.
UNIT_CURRENCY_PER_KWH = "currency/kWh"
UNIT_CURRENCY_PER_MWH = "currency/MWh"
UNIT_CENT_PER_KWH = "cent/kWh"
UNIT_SNT_PER_KWH = "snt/kWh"

MULTIPLIER_MODE_FIXED = "fixed"
MULTIPLIER_MODE_OUTDOOR_TEMPERATURE = "outdoor_temperature"
