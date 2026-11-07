-- Lab default: new penguins start with 10,000 coins (matches runtime STARTING_COINS).
ALTER TABLE penguin ALTER COLUMN coins SET DEFAULT 10000;
