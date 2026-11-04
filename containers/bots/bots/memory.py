"""Short-term conversation memory, isolated per speaker.

One conversation per human: an interlocutor never sees the context of another
conversation, so private threads stay separate. Keys are numeric player ids
provided by the protocol, never free text. Entries expire after a TTL and the
buffer is bounded; :meth:`prune` drops expired conversations.
"""
import time

_TIMESTAMP_KEY = '_ts'


def _now():
    return time.monotonic()


class ConversationMemory:
    """Bounded per-speaker chat history in provider message format."""

    def __init__(self, max_turns=8, ttl=600.0, clock=_now, max_speakers=64):
        if max_turns < 1:
            raise ValueError('max_turns must be at least 1')
        if ttl <= 0:
            raise ValueError('ttl must be positive')
        self.max_turns = max_turns
        self.ttl = ttl
        self._clock = clock
        self.max_speakers = max_speakers
        # speaker_id -> list[dict(role, content, _ts)] alternating
        # user/assistant messages.
        self._turns = {}

    def _trim(self, history):
        # One conversational turn is a user remark plus the assistant reply.
        while len(history) > self.max_turns * 2:
            del history[0]
        return history

    def _record(self, history, role, content):
        history.append({'role': role, 'content': str(content),
                        _TIMESTAMP_KEY: self._clock()})

    def add(self, speaker_id, user_content, assistant_content=None):
        """Store one user remark and, when answered, the assistant reply."""
        speaker_id = int(speaker_id)
        self.prune()
        if speaker_id not in self._turns and len(self._turns) >= self.max_speakers:
            oldest = min(self._turns, key=lambda k: self._turns[k][-1][_TIMESTAMP_KEY])
            self.forget(oldest)
        history = self._turns.setdefault(speaker_id, [])
        self._record(history, 'user', user_content)
        if assistant_content:
            self._record(history, 'assistant', assistant_content)
        self._trim(history)

    def context(self, speaker_id):
        """Copy of this speaker's recent turns; empty for unknown speakers."""
        self.prune()
        history = self._turns.get(int(speaker_id))
        if not history:
            return []
        return [{'role': turn['role'], 'content': turn['content']}
                for turn in history]

    def forget(self, speaker_id):
        """Drop the conversation with one speaker."""
        self._turns.pop(int(speaker_id), None)

    def prune(self):
        """Remove conversations without activity inside the TTL."""
        cutoff = self._clock() - self.ttl
        stale = [key for key, history in self._turns.items()
                 if all(turn[_TIMESTAMP_KEY] < cutoff for turn in history)]
        for key in stale:
            del self._turns[key]
        return len(stale)

    def __len__(self):
        return len(self._turns)

    def __contains__(self, speaker_id):
        return int(speaker_id) in self._turns
