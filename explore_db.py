import sqlite3, json

conn = sqlite3.connect(r'C:\Users\admin\.local\share\opencode\opencode.db')
conn.row_factory = sqlite3.Row

# Check part table
print("=== part table columns ===")
cur = conn.execute('PRAGMA table_info(part)')
for row in cur:
    print(f"  {dict(row)}")

# Get a user message and its parts
print("\n=== user message + parts example ===")
user_msg = conn.execute("SELECT * FROM message m WHERE json_extract(m.data, '$.role')='user' LIMIT 1").fetchone()
if user_msg:
    print(f"Message id: {user_msg['id']}")
    print(f"Session id: {user_msg['session_id']}")
    d = json.loads(user_msg['data'])
    print(f"Data keys: {list(d.keys())}")
    
    # Get parts for this message
    parts = conn.execute("SELECT * FROM part WHERE message_id=? ORDER BY id", (user_msg['id'],)).fetchall()
    print(f"Parts count: {len(parts)}")
    for p in parts:
        pd = json.loads(p['data'])
        print(f"  Part id={p['id'][:30]} type={pd.get('type')} keys={list(pd.keys())}")
        if pd.get('type') == 'text':
            print(f"    text: {pd.get('text','')[:200]}")
        elif pd.get('type') == 'file':
            print(f"    filename: {pd.get('filename','')}")

# Also check session_message structure more broadly  
print("\n=== session_message data samples (all types) ===")
rows = conn.execute("SELECT DISTINCT type FROM session_message").fetchall()
print(f"Types: {[r['type'] for r in rows]}")

# Get one of each type
for r in rows:
    sm = conn.execute("SELECT * FROM session_message WHERE type=? LIMIT 1", (r['type'],)).fetchone()
    if sm:
        d = json.loads(sm['data'])
        print(f"\n  type={sm['type']}: keys={list(d.keys())}")
        if 'text' in d:
            print(f"    text: {str(d['text'])[:150]}")

# Count sessions with user messages (from legacy message table)
print("\n=== session stats ===")
rows = conn.execute("""
    SELECT 
        (SELECT COUNT(*) FROM session) as total_sessions,
        (SELECT COUNT(DISTINCT session_id) FROM message WHERE json_extract(data, '$.role')='user') as sessions_with_user_msgs,
        (SELECT COUNT(*) FROM message WHERE json_extract(data, '$.role')='user') as total_user_msgs
""").fetchone()
print(f"Total sessions: {rows['total_sessions']}")
print(f"Sessions with user msgs: {rows['sessions_with_user_msgs']}")
print(f"Total user msgs: {rows['total_user_msgs']}")

conn.close()
