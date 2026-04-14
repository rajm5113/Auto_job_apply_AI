import sqlite3

conn = sqlite3.connect("data/jobs.db")

# Reset manual_review → scored so applier retries with fixed code
n = conn.execute(
    "UPDATE jobs SET status='scored', fail_reason=NULL WHERE status='manual_review'"
).rowcount
conn.commit()

rows = conn.execute(
    "SELECT job_title, company, score FROM jobs WHERE status='scored' ORDER BY score DESC"
).fetchall()

print(f"Reset {n} jobs to 'scored'. Will retry on next run:")
for r in rows:
    print(f"  [{r[2]:.2f}]  {r[0]} @ {r[1]}")

conn.close()
