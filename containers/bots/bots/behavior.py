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
from . import safety, spend
from .memory import ConversationMemory
from .provider import ProviderError, SimulatedProvider

logger = logging.getLogger('bots.behavior')

_PRUNE_INTERVAL = 60.0
_POST_SEND_PACE = (1.0, 2.5)
_MESSAGE_CONTEXT_LIMIT = 300
# Houdini applies a 1 s cooldown on u#sa / u#se; stay above that between repeats.
# Houdini u#sa cooldown is 1 s; stay above that and re-anchor so dance never drifts.
_STATIONARY_ACTION_PACE = (3.0, 4.5)


class BotBehavior:
    """Per-bot reply pipeline plus the walking loop."""

    def __init__(self, session, bot, policy, behavior, metrics, fallback_replies):
        self.session = session
        self.bot = bot
        self.policy = policy
        self.behavior = behavior
        self.metrics = metrics
        self.fallback_replies = list(bot.fallbacks or fallback_replies)
        self.output_filter = getattr(session, 'output_filter', None) or safety.OutputFilter()
        self.life = None
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
            'No pidas edad, ubicacion, redes ni datos personales. '
            'Si te preguntan en serio si eres un bot o IA, admite que eres IA '
            'sin salir de tu personaje. '
            f'El chat corta los mensajes en {max_len} caracteres: se conciso '
            'para no perder el final de tu frase. '
            'Solo contesta lo que te pregunta tu interlocutor actual sin repetir '
            'frases anteriores.'
        )

    # ---------------------------------------------------------------- startup
    async def start(self):
        self.stopped = False
        self.tasks = [
            asyncio.create_task(self._reply_worker(), name=f'reply-{self.bot.username}'),
            asyncio.create_task(self._prune_loop(), name=f'prune-{self.bot.username}'),
        ]
        if self.bot.card is not None:
            from .life import Life
            self.life = Life(self.session)
            self.tasks.insert(0, asyncio.create_task(
                self.life.run(), name=f'life-{self.bot.username}'))
        elif not self.bot.stationary:
            self.tasks.insert(0, asyncio.create_task(
                self._walk_loop(), name=f'move-{self.bot.username}'))
        elif self.bot.repeat_action is not None or self.bot.repeat_emote is not None:
            self.tasks.insert(0, asyncio.create_task(
                self._stationary_loop(), name=f'move-{self.bot.username}'))

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
        if getattr(session, 'director', None) is not None:
            await session.director.on_chat(session, sender_id, message)
            return
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
        if await self._music(sender_id, message):
            return
        user_content = self._format_user_message(sender_id, message)
        messages = ([{'role': 'system', 'content': self.system_prompt}]
                    + self.memory.context(sender_id)
                    + [{'role': 'user', 'content': user_content}])
        priority = spend.P2 if self._is_bot_speaker(sender_id) else spend.P0
        source = 'local' if isinstance(self.session.provider, SimulatedProvider) else 'llm'
        try:
            reply, _usage = await self.session.provider.complete(messages, priority=priority)
            if source == 'local' and self.bot.card is not None:
                reply = random.choice(self.fallback_replies)
        except ProviderError as error:
            logger.info('%s provider failure (%s); fallback local reply',
                        self.bot.username, error.kind)
            await self.metrics.fallback_used(self.bot.index)
            reply = random.choice(self.fallback_replies)
            source = 'local'
        sent, replaced = await self._safe_line(reply)
        if replaced:
            source = 'local'
        if not sent:
            await self.metrics.skipped(self.bot.index, metrics_mod.SKIP_EMPTY_REPLY)
            return
        await self.session.send_chat(self.session.player_id, sent)
        await self.metrics.chat_sent(self.bot.index)
        await self.metrics.reply(priority, source)
        self.memory.add(sender_id, user_content, sent)
        # A short human-like gap before taking the next queued mention.
        await asyncio.sleep(random.uniform(*_POST_SEND_PACE))

    async def _music(self, sender_id, message, valid=None):
        """«Cobra, pon despacito»: the speaker's own order on the speaker's own Spotify.

        True when the puffle host handled it (the bot then says the fixed result and
        the model never sees the order). Bots never send orders for other bots.
        """
        music = getattr(self.session, 'music', None)
        if music is None or not self.bot.music or self._is_bot_speaker(sender_id):
            return False
        command = text_mod.strip_mention(message, self.bot.mention_names)
        try:
            answer = await music.handle(sender_id, command)
        except Exception as error:  # music down: answer normally instead
            logger.info('%s music unavailable (%s)', self.bot.username, type(error).__name__)
            return False
        if answer is None:
            return False
        if valid is not None and not valid():
            return True
        sent, _replaced = await self._safe_line(answer)
        if sent:
            await self.session.send_chat(self.session.player_id, sent)
            await self.metrics.chat_sent(self.bot.index)
            await self.metrics.reply(spend.P0, 'local')
            await asyncio.sleep(random.uniform(*_POST_SEND_PACE))
        return True

    async def _safe_line(self, reply):
        """Filter after sanitizing; a fallback is checked by the same rules."""
        sent = text_mod.sanitize(reply, self.behavior.max_message_length)
        reason = self.output_filter.check(sent)
        if reason is None:
            return sent, False
        await self.metrics.output_filtered(reason)
        await self.metrics.fallback_used(self.bot.index)
        safe = [text_mod.sanitize(line, self.behavior.max_message_length)
                for line in self.fallback_replies]
        safe = [line for line in safe if line and self.output_filter.check(line) is None]
        return (random.choice(safe) if safe else ''), True

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

    async def _stationary_loop(self):
        """Hold the spawn tile; optionally repeat a dance emote or action."""
        try:
            await asyncio.sleep(random.uniform(self.behavior.first_move_delay_min,
                                               self.behavior.first_move_delay_max))
            while not self.stopped:
                try:
                    if self.bot.repeat_action is not None:
                        await self.session.send_action(self.bot.repeat_action)
                    elif self.bot.repeat_emote is not None:
                        await self.session.send_emote(self.bot.repeat_emote)
                    else:
                        await asyncio.sleep(_STATIONARY_ACTION_PACE[1])
                        continue
                except as2.ServerError as error:
                    logger.debug('%s stationary action skipped (server %s)',
                                 self.bot.username, error.code)
                except as2.Disconnected:
                    return
                await asyncio.sleep(random.uniform(*_STATIONARY_ACTION_PACE))
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
