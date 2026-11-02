import sqlite3
db = sqlite3.connect("cards.sqlite")
cards = db.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
priced = db.execute("SELECT COUNT(DISTINCT product_id) FROM prices WHERE market IS NOT NULL").fetchone()[0]
date = db.execute("SELECT value FROM meta WHERE key = 'data_date'").fetchone()
sets = db.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
print("kártyák:", cards, "| szettek:", sets, "| piaci árral:", priced, "| dátum:", date[0] if date else None)
print()
for r in db.execute("""SELECT c.name, s.name, c.number, c.rarity
                       FROM cards c JOIN sets s ON s.set_id = c.set_id
                       WHERE c.name_norm = 'charizard' AND c.number_int = 4"""):
    print(r)
