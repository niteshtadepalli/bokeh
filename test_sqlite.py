import sqlite3

conn = sqlite3.connect(':memory:')
c = conn.cursor()

c.execute("CREATE TABLE findings (finding_id TEXT PRIMARY KEY, title TEXT)")
c.execute("INSERT INTO findings VALUES ('f1', 'old')")

c.execute("ATTACH DATABASE ':memory:' AS worker")
c.execute("CREATE TABLE worker.findings (finding_id TEXT PRIMARY KEY, title TEXT)")
c.execute("INSERT INTO worker.findings VALUES ('f1', 'new')")

c.execute("""
UPDATE main.findings 
SET title = (SELECT title FROM worker.findings WHERE finding_id = main.findings.finding_id)
WHERE finding_id IN (SELECT finding_id FROM worker.findings)
""")

c.execute("SELECT * FROM main.findings")
print(c.fetchall())
