"""Message sanitation and mention matching.

Piece of care: everything a bot says goes through the same XT line as player
chat, ``%xt%s%m#sm%-1%<id>%<message>%``. Houdini also treats messages that
start with a command prefix as server commands. Generated text must never be
able to break framing or inject protocol data.
"""
import re
import unicodedata

# Delimiters of the wire protocol and separators of the compiled player
# string (id|nickname|...). A generated message must not contain them.
_FORBIDDEN_CHARS = '%|\x00'
_COMMAND_PREFIXES = '!?.'
_WHITESPACE_RE = re.compile(r'\s+')
# Printable subset check: allow anything except control characters and DEL.
_NONPRINT_RE = re.compile(r'[\x00-\x1f\x7f\x9f]+')


def sanitize(text, max_length=54):
    """Return a chat-safe string or '' when nothing meaningful survives.

    - removes protocol delimiters (% | NUL) instead of escaping them, so no
      extra field can ever be parsed out of the payload;
    - replaces control characters with spaces;
    - drops leading command prefixes so replies cannot invoke Houdini
      commands (! ? .);
    - collapses whitespace and truncates to the AS2 chat box length.
    """
    if not isinstance(text, str):
        text = str(text)
    cleaned = ''.join(' ' if unicodedata.category(c).startswith('C') else c for c in text)
    cleaned = cleaned.translate({ord(c): None for c in _FORBIDDEN_CHARS})
    # A reply must not start with a word lookalike of a command prefix.
    cleaned = cleaned.lstrip()
    while cleaned and cleaned[0] in _COMMAND_PREFIXES:
        cleaned = cleaned[1:].lstrip()
    cleaned = _WHITESPACE_RE.sub(' ', cleaned).strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].strip()
    return cleaned


def mentions(message, names):
    """True when any of ``names`` (case-insensitive) appears in the message."""
    if not message:
        return False
    def normalize(value):
        return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                       if not unicodedata.combining(c))
    lowered = normalize(message)
    return any(name and re.search(r'(?<!\w)' + re.escape(normalize(name)) + r'(?!\w)', lowered)
               for name in names)
