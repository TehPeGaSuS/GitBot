"""
IRC formatting utilities.
Colour codes follow the mIRC / IRC standard.
"""

# ----------------------------------------------------------------- colour codes

RESET  = "\x0f"
BOLD   = "\x02"
ITALIC = "\x1d"
UNDER  = "\x1f"
COLOR  = "\x03"

# Named colours (mIRC numbers)
WHITE       = "00"
BLACK       = "01"
BLUE        = "02"
GREEN       = "03"
RED         = "04"
BROWN       = "05"
PURPLE      = "06"
ORANGE      = "07"
YELLOW      = "08"
LIGHTGREEN  = "09"
TEAL        = "10"
LIGHTCYAN   = "11"
LIGHTBLUE   = "12"
PINK        = "13"
DARKGREY    = "14"
LIGHTGREY   = "15"

# Aliases used by webhook formatters
COLOR_BRANCH   = ORANGE
COLOR_REPO     = DARKGREY
COLOR_POSITIVE = GREEN
COLOR_NEUTRAL  = LIGHTGREY
COLOR_NEGATIVE = RED
COLOR_ID       = PINK

# zero-width non-joiner – used to prevent highlights
ZWNJ = "\u200c"


def color(text: str, code: str) -> str:
    """Wrap *text* in IRC colour *code*, then reset."""
    return f"{COLOR}{code}{text}{COLOR}"


def bold(text: str) -> str:
    return f"{BOLD}{text}{BOLD}"


def reset(text: str) -> str:
    return f"{text}{RESET}"


def strip_html(text: str) -> str:
    """Very basic HTML tag removal."""
    import re
    return re.sub(r"<[^>]+>", "", text)


def strip_formatting(text: str) -> str:
    """Remove all IRC formatting control characters from *text*.

    Strips: colour (\x03 fg,bg), bold (\x02), italic (\x1d),
            underline (\x1f), strikethrough (\x1e), monospace (\x11),
            reverse (\x16), and reset (\x0f).
    """
    import re
    # \x03 may be followed by up to two comma-separated numbers (fg,bg)
    text = re.sub(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?", "", text)
    text = re.sub(r"[\x02\x0f\x11\x16\x1d\x1e\x1f]", "", text)
    return text


def prevent_highlight(nicknames: list, text: str) -> str:
    """Insert ZWNJ after the first char of every nick found in *text*.

    The ZWNJ is inserted on every occurrence — including inside URLs and
    repo paths — because WeeChat (and most IRC clients) trigger highlights
    on any substring match regardless of word boundaries. The ZWNJ is a
    zero-width character that is invisible to URL openers (stripped before
    the URL is opened) but breaks the highlight match.
    """
    import re
    for nick in nicknames:
        if len(nick) < 2:
            continue
        pattern = re.compile(re.escape(nick), re.IGNORECASE)
        text = pattern.sub(
            lambda m: m.group(0)[0] + ZWNJ + m.group(0)[1:], text)
    return text
