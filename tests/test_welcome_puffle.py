import asyncio
import importlib.util
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    'lab_welcome_puffle', ROOT / 'containers/runtime/lab_welcome_puffle.py')
welcome = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = welcome
_spec.loader.exec_module(welcome)


class MemoryWelcomeStore:
    def __init__(self):
        self.puffles = {}
        self.items = {}
        self.welcome_by_penguin = {}
        self.welcome_by_instance = {}
        self._next_id = 1
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def atomic(self):
        async with self._lock:
            snapshot = (
                dict(self.puffles),
                dict(self.items),
                dict(self.welcome_by_penguin),
                dict(self.welcome_by_instance),
                self._next_id,
            )
            try:
                yield
            except Exception:
                puffles, items, by_penguin, by_instance, next_id = snapshot
                self.puffles = dict(puffles)
                self.items = dict(items)
                self.welcome_by_penguin = dict(by_penguin)
                self.welcome_by_instance = dict(by_instance)
                self._next_id = next_id
                raise

    async def welcome_instance_id(self, penguin_id):
        return self.welcome_by_penguin.get(int(penguin_id))

    async def is_welcome_instance(self, puffle_instance_id):
        return int(puffle_instance_id) in self.welcome_by_instance

    async def insert_owned_puffle(self, penguin_id, puffle_id, name, hat, backyard):
        instance_id = self._next_id
        self._next_id += 1
        self.puffles[instance_id] = {
            'id': instance_id,
            'penguin_id': int(penguin_id),
            'puffle_id': int(puffle_id),
            'name': name,
            'hat': int(hat) if hat is not None else None,
            'backyard': bool(backyard),
        }
        return instance_id

    async def claim_welcome(self, penguin_id, puffle_instance_id):
        penguin_id = int(penguin_id)
        puffle_instance_id = int(puffle_instance_id)
        if penguin_id in self.welcome_by_penguin or puffle_instance_id in self.welcome_by_instance:
            raise welcome.WelcomeAlreadyGranted
        self.welcome_by_penguin[penguin_id] = puffle_instance_id
        self.welcome_by_instance[puffle_instance_id] = penguin_id

    async def ensure_puffle_item(self, penguin_id, item_id, quantity=1):
        self.items.setdefault((int(penguin_id), int(item_id)), int(quantity))


class NameTests(unittest.TestCase):
    def test_empty_or_missing_name_defaults_to_dubstep(self):
        self.assertEqual(welcome.normalize_welcome_puffle_name(None), 'Dubstep')
        self.assertEqual(welcome.normalize_welcome_puffle_name(''), 'Dubstep')
        self.assertEqual(welcome.normalize_welcome_puffle_name('  '), 'Dubstep')

    def test_custom_name_is_title_cased(self):
        self.assertEqual(welcome.normalize_welcome_puffle_name('luna'), 'Luna')
        self.assertEqual(welcome.normalize_welcome_puffle_name('Dubstep'), 'Dubstep')

    def test_invalid_names_are_rejected(self):
        self.assertIsNone(welcome.normalize_welcome_puffle_name('bad name'))
        self.assertIsNone(welcome.normalize_welcome_puffle_name('Dubstep1'))
        self.assertIsNone(welcome.normalize_welcome_puffle_name('x' * 13))


class UniqueGrantTests(unittest.IsolatedAsyncioTestCase):
    async def test_grant_creates_one_black_puffle_with_headphones(self):
        store = MemoryWelcomeStore()
        instance_id = await welcome.grant_welcome_puffle(store, 41)
        pet = store.puffles[instance_id]
        self.assertEqual(pet['puffle_id'], welcome.BLACK_PUFFLE_TYPE_ID)
        self.assertEqual(pet['name'], 'Dubstep')
        self.assertEqual(pet['hat'], welcome.HEADPHONES_ITEM_ID)
        self.assertFalse(pet['backyard'])
        self.assertEqual(store.items[(41, welcome.HEADPHONES_ITEM_ID)], 1)
        self.assertEqual(store.welcome_by_penguin[41], instance_id)

    async def test_grant_uses_the_chosen_name(self):
        store = MemoryWelcomeStore()
        instance_id = await welcome.grant_welcome_puffle(store, 42, name='luna')
        self.assertEqual(store.puffles[instance_id]['name'], 'Luna')
        self.assertTrue(await welcome.is_protected_puffle(store, instance_id))

    async def test_retry_does_not_create_a_second_instance(self):
        store = MemoryWelcomeStore()
        first = await welcome.grant_welcome_puffle(store, 7)
        second = await welcome.grant_welcome_puffle(store, 7)
        self.assertEqual(first, second)
        self.assertEqual(len(store.puffles), 1)
        self.assertEqual(len(store.welcome_by_penguin), 1)

    async def test_concurrent_grants_keep_a_single_row(self):
        store = MemoryWelcomeStore()
        first, second = await asyncio.gather(
            welcome.grant_welcome_puffle(store, 9),
            welcome.grant_welcome_puffle(store, 9),
        )
        self.assertEqual(first, second)
        self.assertEqual(len(store.puffles), 1)

    async def test_failed_claim_rolls_back_the_extra_puffle(self):
        store = MemoryWelcomeStore()

        async def occupied(penguin_id, puffle_instance_id):
            raise welcome.WelcomeAlreadyGranted

        store.claim_welcome = occupied
        with self.assertRaises(welcome.WelcomeAlreadyGranted):
            await welcome.grant_welcome_puffle(store, 3)
        self.assertEqual(store.puffles, {})
        self.assertEqual(store.welcome_by_penguin, {})


class ProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_protection_follows_the_instance_not_hat_or_name(self):
        store = MemoryWelcomeStore()
        welcome_id = await welcome.grant_welcome_puffle(store, 12)
        store.puffles[welcome_id]['hat'] = 0
        store.puffles[welcome_id]['name'] = 'Renamed'
        other = await store.insert_owned_puffle(
            penguin_id=12,
            puffle_id=welcome.BLACK_PUFFLE_TYPE_ID,
            name='Dubstep',
            hat=welcome.HEADPHONES_ITEM_ID,
            backyard=False,
        )
        self.assertTrue(await welcome.is_protected_puffle(store, welcome_id))
        self.assertFalse(await welcome.is_protected_puffle(store, other))

    async def test_preexisting_puffle_is_not_protected(self):
        store = MemoryWelcomeStore()
        adopted = await store.insert_owned_puffle(
            penguin_id=4, puffle_id=2, name='Dubstep', hat=53, backyard=False)
        self.assertFalse(await welcome.is_protected_puffle(store, adopted))


class UniqueViolationTests(unittest.TestCase):
    def test_asyncpg_unique_violation_name(self):
        class UniqueViolationError(Exception):
            sqlstate = '23505'

        self.assertTrue(welcome.is_unique_violation(UniqueViolationError()))
        self.assertFalse(welcome.is_unique_violation(RuntimeError('nope')))


if __name__ == '__main__':
    unittest.main()
