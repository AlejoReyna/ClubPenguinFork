-- Local dev: Cadence on the DJ booth platform (no shirt — pink body + scarf/accessories).
BEGIN;
UPDATE penguin SET nickname = 'CadenceMascot', active = false, character = NULL
WHERE username = 'cadence' AND id <> (SELECT id FROM penguin WHERE username = 'djia');

INSERT INTO penguin_item (penguin_id, item_id)
SELECT p.id, outfit.item_id
FROM penguin AS p
JOIN (VALUES
    ('djia', 1032), ('djia', 1033), ('djia', 1034), ('djia', 3011)
) AS outfit(username, item_id) ON outfit.username = p.username
ON CONFLICT (penguin_id, item_id) DO NOTHING;

UPDATE penguin SET
    nickname = 'Cadence',
    color = 10,
    head = 1032,
    face = NULL,
    neck = 3011,
    body = NULL,
    hand = 1034,
    feet = 1033,
    character = NULL
WHERE username = 'djia';
COMMIT;
