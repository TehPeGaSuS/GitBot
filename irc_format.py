"""IRC formatting helpers — colors, bold, etc."""

BOLD      = "\x02"
COLOR     = "\x03"
RESET     = "\x0F"

# IRC color codes
WHITE      = 0
BLACK      = 1
BLUE       = 2
GREEN      = 3
RED        = 4
BROWN      = 5
PURPLE     = 6
ORANGE     = 7
YELLOW     = 8
LIGHTGREEN = 9
CYAN       = 10
LIGHTCYAN  = 11
LIGHTBLUE  = 12
PINK       = 13
GREY       = 14
LIGHTGREY  = 15

# Semantic aliases used by webhook formatters
COLOR_BRANCH   = ORANGE
COLOR_REPO     = GREY
COLOR_POSITIVE = GREEN
COLOR_NEGATIVE = RED
COLOR_NEUTRAL  = LIGHTGREY
COLOR_ID       = PINK


def color(s: str, fg: int) -> str:
    return f"{COLOR}{fg:02d}{s}{COLOR}"


def bold(s: str) -> str:
    return f"{BOLD}{s}{BOLD}"
