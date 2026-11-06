"""Output filter for model text, applied after ``text.sanitize``.

Why it exists: Houdini's chat filter (handlers/play/message.py) can *ban* the
sender, and the bot pod has no database access to read its rules. A model
sentence that trips a ``ban`` rule would ban the bot account. So the same words
are checked here first, and a rejected sentence is replaced by a local line.
Houdini matches whole whitespace-separated lowercase tokens; this filter is
stricter on purpose (accent- and punctuation-insensitive, phrases allowed).

It also stops what a language model must never publish in a children's game:
links and domains, e-mail addresses and handles, and long digit runs (phone
numbers, ids, codes). The blocklist file is optional; the checks that need no
data are always on.
"""
import logging
import re
import unicodedata
from pathlib import Path

logger = logging.getLogger('bots.safety')

# Bounded reasons (metric labels).
REASON_URL = 'url'
REASON_EMAIL = 'email'
REASON_DIGITS = 'digits'
REASON_BLOCKLIST = 'blocklist'
REASONS = (REASON_URL, REASON_EMAIL, REASON_DIGITS, REASON_BLOCKLIST)

# A digit run this long (separators between digits ignored) is not small talk.
DEFAULT_MAX_DIGITS = 6

_SCHEME_RE = re.compile(r'(?:\b[a-z][a-z0-9+.-]{1,12}://|\bwww\s*\.)', re.IGNORECASE)
# label.tld with no space: "ejemplo.com", "mi-sitio.gg/abc". Over-blocks "hola.como"
# (a missing space after a full stop), which is the cheap direction to be wrong.
_DOMAIN_RE = re.compile(r'(?<![\w@])[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]{1,63})*\.[a-z]{2,24}(?![\w])',
                        re.IGNORECASE)
# "ejemplo punto com", "ejemplo dot com", "ejemplo [.] com"
_SPOKEN_DOMAIN_RE = re.compile(
    r'\b(?:punto|dot)\s+(?:com|net|org|es|mx|ar|cl|co|io|gg|tv|me|ly|xyz|app|dev|info|club|online|site)\b'
    r'|\[\s*\.\s*\]|\(\s*(?:punto|dot|\.)\s*\)', re.IGNORECASE)
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+', re.IGNORECASE)
# "@usuario" handles and spoken forms: "usuario arroba gmail", "usuario [at] x"
_HANDLE_RE = re.compile(r'(?<!\w)@\w{2,}|\b(?:arroba)\b|\[\s*at\s*\]|\(\s*at\s*\)', re.IGNORECASE)
_DIGIT_RUN_RE = re.compile(r'\d(?:[\s().+-]?\d)*')
_WORD_RE = re.compile(r'[^\W_]+', re.UNICODE)


def _fold(value):
    """Lowercase, accent-free text for comparisons."""
    decomposed = unicodedata.normalize('NFKD', str(value).casefold())
    return ''.join(c for c in decomposed if not unicodedata.combining(c))


def load_blocklist(path):
    """Words/phrases from a text file, one per line; blank lines and ``#`` comments ignored.

    Returns a tuple of folded entries. A missing file is an error: an operator who
    configured a blocklist expects it to be enforced, never silently skipped.
    """
    entries = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            entries.append(' '.join(_fold(line).split()))
    return tuple(dict.fromkeys(entry for entry in entries if entry))


class OutputFilter:
    """Decides whether a sentence may be published as is."""

    def __init__(self, blocklist=(), max_digits=DEFAULT_MAX_DIGITS):
        if int(max_digits) < 2:
            raise ValueError('max_digits must be at least 2')
        self.max_digits = int(max_digits)
        words, phrases = set(), []
        for entry in blocklist:
            folded = ' '.join(_fold(entry).split())
            if not folded:
                continue
            if ' ' in folded:
                phrases.append(folded)
            else:
                words.add(folded)
        self.words = frozenset(words)
        self.phrases = tuple(phrases)

    @classmethod
    def from_file(cls, path, **kwargs):
        return cls(load_blocklist(path), **kwargs)

    def __len__(self):
        return len(self.words) + len(self.phrases)

    def check(self, text):
        """The bounded reason a sentence is refused, or None when it may go out."""
        text = str(text)
        # E-mail first: an address contains a domain, but it is the address that matters.
        if _EMAIL_RE.search(text) or _HANDLE_RE.search(text):
            return REASON_EMAIL
        if _SCHEME_RE.search(text) or _SPOKEN_DOMAIN_RE.search(text) or _DOMAIN_RE.search(text):
            return REASON_URL
        for run in _DIGIT_RUN_RE.findall(text):
            if sum(c.isdigit() for c in run) >= self.max_digits:
                return REASON_DIGITS
        if self._blocked(text):
            return REASON_BLOCKLIST
        return None

    def _blocked(self, text):
        if not self.words and not self.phrases:
            return False
        folded = _fold(text)
        tokens = set(folded.split()) | set(_WORD_RE.findall(folded))
        if self.words & tokens:
            return True
        # Phrases are matched on word boundaries over the whitespace-collapsed text.
        joined = ' ' + ' '.join(_WORD_RE.findall(folded)) + ' '
        return any(' ' + ' '.join(_WORD_RE.findall(phrase)) + ' ' in joined for phrase in self.phrases)


def check_local_line(text, max_length, output_filter=None):
    """Problem with an operator-written chat line, or None. Used when cards load."""
    line = str(text)
    if not line.strip():
        return 'is empty'
    if len(line) > max_length:
        return f'is longer than {max_length} characters'
    if any(ord(c) > 126 or ord(c) < 32 for c in line):
        return 'must be plain ASCII (the client font and the existing local lines are ASCII)'
    if any(c in line for c in '%|'):
        return 'contains a protocol delimiter'
    reason = (output_filter or OutputFilter()).check(line)
    if reason:
        return f'would be refused by the output filter ({reason})'
    return None
