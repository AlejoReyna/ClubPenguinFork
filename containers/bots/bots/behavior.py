"""What a bot does once online: walking with local logic and chat replies.

Movement never touches the model: targets are random inside a per-bot area,
paced several seconds apart, exactly like a player clicking the floor (the
server then animates the walk). Replies are the only place the provider is
used; the packet reader keeps running while a reply is generated.
"""
import asyncio
import logging
import random

from . import as2
from . import metrics as metrics_mod
from . import text as text_mod
from .memory import ConversationMemory
from .provider import ProviderError

logger = logging.getLogger('bots.behavior')

_PRUNE_INTERVAL = 60.0
_POST_SEND_PACE = (1.0, 2.5)
_MESSAGE_CONTEXT_LIMIT = 300


class BotBehavior:
    """Per-bot reply pipeline plus the walking loop."""

    def __init__(self, session, bot, policy, behavior, metrics, fallback_replies):
        self.session = session
        self.bot = bot
        self.policy = policy
        self.behavior = behavior
        self.metrics = metrics
        self.fallback_replies = list(fallback_replies)
        self.memory = ConversationMemory(max_turns=policy.memory_turns,
                                         ttl=policy.memory_ttl)
        self.queue = asyncio.Queue(maxsize=policy.queue_size)
        self.tasks = []
        self.stopped = True
        # Peer bot nicknames injected by the manager: roster lookups decide
        # whether a speaker is another bot before any mention logic runs.
        self.peer_bot_names = frozenset()
        self._bot_exchange = {}  # peer_id -> consecutive bot-directed exchanges
        self.system_prompt = self._build_prompt()

    # ----------------------------------------------------------------- prompt
    def _build_prompt(self):
        max_len = self.behavior.max_message_length
        personality = self.bot.personality.strip() \
            or 'Eres un vecino simpatico de la isla, siempre dispuesto a ayudar.'
        return (
            f'Eres {self.bot.nickname}, un pinguino de Club Penguin en el mundo '
            f'Glaciar. Lo que sabes y haces: {personality} '
            'Respondes SIEMPRE en una sola frase corta de conversacion casual, '
            'de maximo 25 palabras. '
            'No inventes poderes que no tienes, no pidas contrasenas, no envies '
            'links y no uses comandos ni emojis. '
            f'El chat corta los mensajes en {max_len} caracteres: se conciso '
            'para no perder el final de tu frase. '
            'Solo contesta lo que te pregunta tu interlocutor actual sin repetir '
            'frases anteriores.'
        )

    # ---------------------------------------------------------------- startup
    async def start(self):
        self.stopped = False
        self.tasks = [
            asyncio.create_task(self._walk_loop(), name=f'walk-{self.bot.username}'),
            asyncio.create_task(self._reply_worker(), name=f'reply-{self.bot.username}'),
            asyncio.create_task(self._prune_loop(), name=f'prune-{self.bot.username}'),
        ]

    async def stop(self):
        self.stopped = True
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._drain_queue()
        self._bot_exchange.clear()

    def _drain_queue(self):
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    # ---------------------------------------------------------- incoming chat
    async def on_chat(self, sender_id, message):
        """Classify one room message; enqueue a reply only when appropriate."""
        session = self.session
        if sender_id == session.player_id:
            await self.metrics.skipped(self.bot.index, metrics_mod.SKIP_SELF)
            return

        # A human message resets any bot-to-bot thread we might be parting of.
        if self._is_bot_speaker(sender_id):
            if self._bot_speaker_exhausted(sender_id):
                await self.metrics.skipped(self.bot.index, metrics_mod.SKIP_BOT)
                return
        else:
            self._bot_exchange.clear()

        if not text_mod.mentions(message, self.bot.mention_names):
            await self.metrics.skipped(self.bot.index, metrics_mod.SKIP_NOT_MENTIONED)
            return

        allowed, reason = session.gate.try_reserve(self.bot.index, sender_id)
        if not allowed:
            await self.metrics.skipped(self.bot.index, reason)
            return

        try:
            self.queue.put_nowait((sender_id, message))
        except asyncio.QueueFull:
            await self.metrics.skipped(self.bot.index, metrics_mod.SKIP_QUEUE_FULL)

    def _is_bot_speaker(self, sender_id):
        if sender_id in getattr(self.session, 'peer_bot_ids', lambda: set())():
            return True
        nickname = self.session.roster.get(sender_id)
        if nickname is None:
            return False
        return nickname.casefold() in {name.casefold() for name in self.peer_bot_names}

    def _bot_speaker_exhausted(self, sender_id):
        """Bot-to-bot replies exist but are opt-in and strictly bounded."""
        if not self.behavior.respond_to_bots:
            return True
        count = self._bot_exchange.get(sender_id, 0)
        if count >= self.behavior.bot_exchange_limit:
            return True
        if not self.session.bot_gap.try_acquire(self.bot.index):
            return True
        self._bot_exchange[sender_id] = count + 1
        return False

    # ----------------------------------------------------------- reply worker
    async def _reply_worker(self):
        """Serial provider pipeline per bot; readers never wait on it."""
        try:
            while not self.stopped:
                try:
                    sender_id, message = await self.queue.get()
                    await self._reply(sender_id, message)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.error('bot %s reply pipeline: %s', self.bot.index, type(error).__name__)
        except asyncio.CancelledError:
            raise

    async def _reply(self, sender_id, message):
        """Generate and publish one answer for ``message`` from ``sender_id``."""
        user_content = self._format_user_message(sender_id, message)
        messages = ([{'role': 'system', 'content': self.system_prompt}]
                    + self.memory.context(sender_id)
                    + [{'role': 'user', 'content': user_content}])
        try:
            reply, _usage = await self.session.provider.complete(messages)
        except ProviderError as error:
            logger.info('%s provider failure (%s); fallback local reply',
                        self.bot.username, error.kind)
            await self.metrics.fallback_used(self.bot.index)
            reply = random.choice(self.fallback_replies)
        sent = text_mod.sanitize(reply, self.behavior.max_message_length)
        if not sent:
            await self.metrics.skipped(self.bot.index, metrics_mod.SKIP_EMPTY_REPLY)
            return
        await self.session.send_chat(self.session.player_id, sent)
        await self.metrics.chat_sent(self.bot.index)
        self.memory.add(sender_id, user_content, sent)
        # A short human-like gap before taking the next queued mention.
        await asyncio.sleep(random.uniform(*_POST_SEND_PACE))

    def _format_user_message(self, sender_id, message):
        nickname = self.session.roster.get(sender_id)
        speaker = nickname or 'un jugador'
        trimmed = str(message)[:_MESSAGE_CONTEXT_LIMIT]
        return f'{speaker} dice: {trimmed}'

    # ------------------------------------------------------------- movement
    async def _walk_loop(self):
        """Random walk inside the configured rectangle; no teleport bursts."""
        try:
            await asyncio.sleep(random.uniform(self.behavior.first_move_delay_min,
                                               self.behavior.first_move_delay_max))
            while not self.stopped:
                min_x, max_x, min_y, max_y = self.bot.area
                try:
                    await self.session.send_move(random.randint(min_x, max_x),
                                                 random.randint(min_y, max_y))
                except as2.Disconnected:
                    return
                await asyncio.sleep(random.uniform(self.behavior.walk_min_delay,
                                                   self.behavior.walk_max_delay))
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------- maintenance
    async def _prune_loop(self):
        """Periodic memory hygiene; expired conversations are freed."""
        try:
            while True:
                await asyncio.sleep(_PRUNE_INTERVAL)
                forgotten = self.memory.prune()
                if forgotten:
                    logger.debug('%s released %d stale conversation(s)',
                                 self.bot.username, forgotten)
        except asyncio.CancelledError:
            raise
