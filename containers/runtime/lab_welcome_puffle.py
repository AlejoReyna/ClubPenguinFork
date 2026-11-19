"""Welcome Dubstep Puffle: one protected instance per new account.

HPHONES1 is the historical Headphones redeem (puffle_item.id = 53). Delivery
grants and equips that item on a black puffle (puffle_id = 2) named Dubstep.
Identification lives in penguin_welcome_puffle so protection follows the
instance after hat, name or walking changes.
"""

BLACK_PUFFLE_TYPE_ID = 2
HEADPHONES_ITEM_ID = 53
WELCOME_PUFFLE_NAME = 'Dubstep'


def normalize_welcome_puffle_name(raw):
    """Return a valid puffle name. Empty/None becomes Dubstep; invalid input is None."""
    if raw is None:
        return WELCOME_PUFFLE_NAME
    name = str(raw).strip()
    if not name:
        return WELCOME_PUFFLE_NAME
    if len(name) > 12 or not name.isalpha():
        return None
    return name.title()


class WelcomeAlreadyGranted(Exception):
    """Unique penguin_id on penguin_welcome_puffle rejected a second gift."""


async def grant_welcome_puffle(store, penguin_id, name=None):
    """Create at most one welcome puffle for penguin_id. Idempotent under retry."""
    existing = await store.welcome_instance_id(penguin_id)
    if existing is not None:
        return existing
    puffle_name = normalize_welcome_puffle_name(name) or WELCOME_PUFFLE_NAME
    try:
        async with store.atomic():
            instance_id = await store.insert_owned_puffle(
                penguin_id=penguin_id,
                puffle_id=BLACK_PUFFLE_TYPE_ID,
                name=puffle_name,
                hat=HEADPHONES_ITEM_ID,
                backyard=False,
            )
            await store.claim_welcome(penguin_id, instance_id)
            await store.ensure_puffle_item(penguin_id, HEADPHONES_ITEM_ID, quantity=1)
            return instance_id
    except WelcomeAlreadyGranted:
        existing = await store.welcome_instance_id(penguin_id)
        if existing is None:
            raise
        return existing


async def is_protected_puffle(store, puffle_instance_id):
    return await store.is_welcome_instance(puffle_instance_id)


async def grant_for_penguin(db, penguin_id, name=None):
    return await grant_welcome_puffle(GinoWelcomeStore(db), penguin_id, name=name)


async def is_protected_puffle_id(db, puffle_instance_id):
    return await is_protected_puffle(GinoWelcomeStore(db), puffle_instance_id)


def is_unique_violation(exc):
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        sqlstate = getattr(current, 'sqlstate', None) or getattr(current, 'pgcode', None)
        if name == 'UniqueViolationError':
            return True
        if name == 'IntegrityError' and sqlstate in (None, '23505'):
            return True
        current = getattr(current, '__cause__', None) or getattr(current, 'orig', None)
    return False


class GinoWelcomeStore:
    """Postgres adapter used by Dash (delivery) and Houdini (protection)."""

    def __init__(self, db):
        self.db = db

    def atomic(self):
        return self.db.transaction()

    async def welcome_instance_id(self, penguin_id):
        value = await self._scalar(
            'SELECT puffle_instance_id FROM penguin_welcome_puffle WHERE penguin_id = :penguin_id',
            penguin_id=int(penguin_id),
        )
        return int(value) if value is not None else None

    async def is_welcome_instance(self, puffle_instance_id):
        value = await self._scalar(
            'SELECT 1 FROM penguin_welcome_puffle WHERE puffle_instance_id = :puffle_instance_id',
            puffle_instance_id=int(puffle_instance_id),
        )
        return value is not None

    async def insert_owned_puffle(self, penguin_id, puffle_id, name, hat, backyard):
        value = await self._scalar(
            'INSERT INTO penguin_puffle (penguin_id, puffle_id, name, hat, backyard) '
            'VALUES (:penguin_id, :puffle_id, :name, :hat, :backyard) RETURNING id',
            penguin_id=int(penguin_id),
            puffle_id=int(puffle_id),
            name=name,
            hat=int(hat),
            backyard=bool(backyard),
        )
        return int(value)

    async def claim_welcome(self, penguin_id, puffle_instance_id):
        try:
            await self._status(
                'INSERT INTO penguin_welcome_puffle (penguin_id, puffle_instance_id) '
                'VALUES (:penguin_id, :puffle_instance_id)',
                penguin_id=int(penguin_id),
                puffle_instance_id=int(puffle_instance_id),
            )
        except Exception as exc:
            if is_unique_violation(exc):
                raise WelcomeAlreadyGranted from exc
            raise

    async def ensure_puffle_item(self, penguin_id, item_id, quantity=1):
        await self._status(
            'INSERT INTO penguin_puffle_item (penguin_id, item_id, quantity) '
            'VALUES (:penguin_id, :item_id, :quantity) '
            'ON CONFLICT (penguin_id, item_id) DO NOTHING',
            penguin_id=int(penguin_id),
            item_id=int(item_id),
            quantity=int(quantity),
        )

    async def _scalar(self, sql, **params):
        return await self.db.scalar(self.db.text(sql).bindparams(**params))

    async def _status(self, sql, **params):
        return await self.db.status(self.db.text(sql).bindparams(**params))
