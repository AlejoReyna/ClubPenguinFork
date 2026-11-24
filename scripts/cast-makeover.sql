-- Run after provisioning with psql -v ON_ERROR_STOP=1.
-- Reconnect the bot sessions after commit to reload their outfits.
BEGIN;
SET LOCAL lock_timeout = '5s';
LOCK TABLE penguin IN SHARE ROW EXCLUSIVE MODE;

CREATE TEMP TABLE cast_outfit (
    username text PRIMARY KEY,
    nickname text NOT NULL,
    color integer NOT NULL,
    head integer,
    face integer,
    neck integer,
    body integer,
    hand integer,
    feet integer
) ON COMMIT DROP;

INSERT INTO cast_outfit (username, nickname, color, head, face, neck, body, hand, feet) VALUES
    ('baristaia', 'Juanito', 9, 402, NULL, 3242, 262, 5212, 351),
    ('djia', 'Cadence', 10, 1032, 117, 3011, NULL, 1034, 1033),
    ('pizzeroia', 'Viterbos', 5, 424, NULL, 176, 263, NULL, 358),
    ('avalancha', 'Avalancha', 5, 1511, 136, 175, 4757, 5047, 6142),
    ('bolazo', 'Bolazo', 6, 438, 133, 179, 213, 342, 365),
    ('copito', 'Copito', 12, 483, NULL, 191, 4285, 5011, 366),
    ('nevado', 'Nevado', 16, 1509, 136, 194, 785, 344, 361),
    ('ventisca', 'Ventisca', 15, 418, NULL, 195, 219, 5032, 365),
    ('lolaritmo', 'Lola Ritmo', 3, 1538, 2032, 184, 4641, 5428, 378),
    ('mambo', 'Mambo', 8, 652, 2010, 3210, 241, 5016, 6008),
    ('titogroove', 'Tito Groove', 1, 1164, 107, NULL, 4009, 5079, 357),
    ('nube', 'Nube', 17, 484, 114, 175, 238, 5551, 379),
    ('pixel', 'Pixel', 13, 481, 103, 3031, 204, 5420, 6002),
    ('pacopregon', 'Paco Pregon', 6, 422, NULL, 317, 209, 328, 351),
    ('mimi', 'Mimi', 10, 498, NULL, 3002, 4063, 751, 380),
    ('bruma', 'Bruma', 14, 21116, 114, 3001, 248, 718, 376),
    ('sombra', 'Sombra', 4, 1582, 104, 303, 4033, NULL, 352),
    ('tormenta', 'Tormenta', 5, 1321, 133, NULL, 4447, 5246, 352),
    ('peperina', 'Peperina', 13, 437, NULL, NULL, 4413, NULL, 377),
    ('bruno', 'Bruno', 4, 403, NULL, 199, 275, 347, 372),
    ('divaperla', 'Diva Perla', 3, 412, 118, 181, 252, 5135, 359),
    ('dontelon', 'Don Telon', 8, 1555, 119, 174, 294, 5031, 358),
    ('kiki', 'Kiki', 7, 1799, 113, 307, 226, 5007, 352),
    ('salitre', 'Salitre', 11, 446, 152, 161, 235, 321, 373),
    ('capigol', 'Capi Gol', 2, 1170, NULL, 194, 4088, 732, 385),
    ('rayo', 'Rayo', 5, 1321, 109, 3002, 4005, 5548, 360);

CREATE TEMP TABLE cast_outfit_items ON COMMIT DROP AS
SELECT outfit.username, slot.item_id, slot.item_type
FROM cast_outfit AS outfit
CROSS JOIN LATERAL (VALUES (outfit.color, 1), (outfit.head, 2), (outfit.face, 3),
                           (outfit.neck, 4), (outfit.body, 5), (outfit.hand, 6),
                           (outfit.feet, 7)) AS slot(item_id, item_type)
WHERE slot.item_id IS NOT NULL;

DO $$
BEGIN
    IF (SELECT count(*) FROM cast_outfit JOIN penguin USING (username, nickname)
        WHERE active AND approval_es AND NOT moderator AND NOT stealth_moderator AND NOT permaban) <> 26 THEN
        RAISE EXCEPTION 'Expected all 26 active, Spanish-approved cast identities';
    END IF;
    IF EXISTS (SELECT 1 FROM cast_outfit_items AS slot LEFT JOIN item
               ON (item.id, item.type) = (slot.item_id, slot.item_type)
               WHERE item.id IS NULL) THEN
        RAISE EXCEPTION 'A cast clothing item is missing or has the wrong type';
    END IF;
END $$;

INSERT INTO penguin_item (penguin_id, item_id)
SELECT penguin.id, slot.item_id
FROM cast_outfit_items AS slot JOIN penguin USING (username)
ON CONFLICT (penguin_id, item_id) DO NOTHING;

UPDATE penguin AS p SET
    color = outfit.color, head = outfit.head, face = outfit.face, neck = outfit.neck,
    body = outfit.body, hand = outfit.hand, feet = outfit.feet
FROM cast_outfit AS outfit
WHERE p.username = outfit.username;

COMMIT;
