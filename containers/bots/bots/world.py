"""Shared in-process world model: who is in each room, humans versus bots, positions.

Every bot session feeds it with the frames it already receives (``jr``/``grs``
snapshots, ``ap``, ``rp``, ``sp``). A bot only hears its own room, so the model
only knows rooms where at least one of our sessions is. Several bots in one room
receive the same broadcasts, hence every update here is idempotent. When the last
bot leaves a room its state is dropped: nobody is listening, so nothing is known.

A "bot" is one of this process's own sessions (registered by player id). Everyone
else in a room is a human as far as the service is concerned; the lab exporter's
``clubpenguin_human_players_estimated`` stays the operator's number for the world.

Nothing here talks to the network or sleeps, and it holds no chat text.
"""
import time

# Player-string layout (Houdini PenguinStringCompiler):
# id|nickname|approval|color|head|face|neck|body|hand|feet|flag|photo|x|y|frame|...
_X, _Y = 12, 13


class Member:
    __slots__ = ('id', 'nickname', 'x', 'y', 'seen')

    def __init__(self, player_id, nickname, x, y, seen):
        self.id, self.nickname, self.x, self.y, self.seen = player_id, nickname, x, y, seen


def parse_record(record):
    """``(id, nickname, x, y)`` from a player string, or None when it is not one.

    ``x``/``y`` are None when the string is short or the numbers are not integers.
    """
    if not isinstance(record, str) or '|' not in record:
        return None
    fields = record.split('|')
    if len(fields) < 2 or not fields[0].isdigit():
        return None

    def coordinate(index):
        try:
            return int(fields[index])
        except (IndexError, ValueError):
            return None

    return int(fields[0]), fields[1], coordinate(_X), coordinate(_Y)


class World:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._bot_ids = set()   # player ids of this process's own sessions
        self._bot_room = {}     # bot player id -> room id it is in now
        self._rooms = {}        # room id -> {player id: Member}
        self._conversations = {}  # own bot id -> <=64 numeric human ids / expiry

    # ------------------------------------------------------------- bots
    def register_bot(self, player_id):
        self._bot_ids.add(int(player_id))

    def is_bot(self, player_id):
        return int(player_id) in self._bot_ids

    def bot_entered(self, bot_id, room_id, records):
        """A bot is now in ``room_id``; ``records`` is that room's full roster (jr/grs)."""
        bot_id, room_id = int(bot_id), int(room_id)
        previous = self._bot_room.get(bot_id)
        self._bot_room[bot_id] = room_id
        if previous is not None and previous != room_id:
            self.member_removed(previous, bot_id)
            self._drop_if_unobserved(previous)
        self.snapshot(room_id, records)

    def bot_left(self, bot_id):
        """A session ended (drop or shutdown): it no longer observes anything."""
        room_id = self._bot_room.pop(int(bot_id), None)
        if room_id is not None:
            self.member_removed(room_id, bot_id)
            self._drop_if_unobserved(room_id)

    def room_of(self, bot_id):
        return self._bot_room.get(int(bot_id))

    def human_room(self, player_id):
        if self.is_bot(player_id):
            return None
        found = [room for room, members in self._rooms.items() if player_id in members]
        return found[0] if len(found) == 1 else None  # ambiguous/unknown is not a travel instruction

    def talked(self, bot_id, human_id, seconds=180):
        if bot_id in self._bot_ids:
            now = self._clock()
            active = {key: expiry for key, expiry in self._conversations.get(bot_id, {}).items() if expiry > now}
            if human_id not in active and len(active) >= 64:
                active.pop(min(active, key=active.get))
            active[human_id] = now + seconds
            self._conversations[bot_id] = active

    def conversation_locked(self, bot_id, requester=None):
        return any(self._clock() < expiry and human != requester
                   for human, expiry in self._conversations.get(bot_id, {}).items())

    def _drop_if_unobserved(self, room_id):
        if not any(room == room_id for room in self._bot_room.values()):
            self._rooms.pop(room_id, None)

    # ----------------------------------------------------------- frames
    def snapshot(self, room_id, records):
        """Replace a room's roster with an authoritative list of player strings."""
        now = self._clock()
        members = {}
        for record in records:
            parsed = parse_record(record)
            if parsed is not None:
                members[parsed[0]] = Member(parsed[0], parsed[1], parsed[2], parsed[3], now)
        self._rooms[int(room_id)] = members

    def member_added(self, room_id, record):
        parsed = parse_record(record)
        if parsed is not None and int(room_id) in self._rooms:
            self._rooms[int(room_id)][parsed[0]] = Member(parsed[0], parsed[1], parsed[2], parsed[3],
                                                          self._clock())

    def member_removed(self, room_id, player_id):
        members = self._rooms.get(int(room_id))
        if members is not None:
            members.pop(int(player_id), None)

    def member_moved(self, room_id, player_id, x, y):
        member = self._rooms.get(int(room_id), {}).get(int(player_id))
        if member is not None:
            try:
                member.x, member.y, member.seen = int(x), int(y), self._clock()
            except (TypeError, ValueError):
                pass

    # ---------------------------------------------------------- queries
    def members(self, room_id):
        return list(self._rooms.get(int(room_id), {}).values())

    def humans_in(self, room_id):
        return [m for m in self.members(room_id) if m.id not in self._bot_ids]

    def bots_in(self, room_id):
        return [m for m in self.members(room_id) if m.id in self._bot_ids]

    def has_humans(self, room_id):
        return bool(self.humans_in(room_id))

    def bot_counts(self):
        """``{room id: bots currently there}`` over the rooms where a bot session is."""
        counts = {}
        for room in self._bot_room.values():
            counts[room] = counts.get(room, 0) + 1
        return counts

    def nearest_human(self, room_id, x, y):
        """The human closest to ``(x, y)`` whose position is known, or None."""
        best, best_distance = None, None
        for human in self.humans_in(room_id):
            if human.x is None or human.y is None:
                continue
            distance = (human.x - x) ** 2 + (human.y - y) ** 2
            if best_distance is None or distance < best_distance:
                best, best_distance = human, distance
        return best
